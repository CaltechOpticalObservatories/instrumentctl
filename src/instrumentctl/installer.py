"""``install``: converge a host onto what this instrument needs.

Run as root, and safe to re-run. It creates the ops group and the daemon
account, enrols operators, makes the directories, and renders the unit, the
polkit rule, the sudoers drop-in and the two helper scripts from the same
``Product`` the rest of the CLI reads, so what ``doctor`` checks is what this
wrote.

Creating the virtualenv is not here. It has to happen before this runs, since
this code lives inside it.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
from importlib import resources
from pathlib import Path
from typing import List, Optional, Sequence

from . import instances as inst
from . import units
from .instances import Paths
from .output import paint
from .product import Product

BIN_DIR = Path("/usr/local/bin")
JOURNAL_GROUP = "systemd-journal"

# Setgid, so a file created by any ops-group member keeps the group rather than
# the creating user's own
CONFIG_MODE = 0o2775
LOG_MODE = 0o2750
SECRETS_MODE = 0o660
TOKEN_MODE = 0o640


def _say(action: str, what: str) -> None:
    print(f"  {paint(action, '32'):<22} {what}")


def _render(template: str, product: Product, paths: Paths) -> str:
    """Fill a template's ``@placeholder@`` values from the product."""
    values = {
        "name": product.name,
        "display_name": product.display_name,
        "user": product.user,
        "ops_group": product.ops_group,
        "env_prefix": product.env_prefix,
        "docs_url": product.docs_url,
        "etc": str(paths.etc),
        "venv": str(paths.venv),
        "repo": str(paths.repo),
        "logs": str(paths.logs),
    }
    text = resources.files(__package__).joinpath("templates", template).read_text(
        encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def _write(path: Path, content: str, mode: int) -> None:
    """Install a rendered file, reporting only when it changes."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        _say("unchanged", str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename, so a reader never sees half a file
    handle, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    _say("installed", str(path))


def _group_and_user(product: Product) -> None:
    """Create the ops group and the account the daemons run as."""
    try:
        grp.getgrnam(product.ops_group)
    except KeyError:
        subprocess.run(["groupadd", "--system", product.ops_group], check=True)
        _say("created group", product.ops_group)
    try:
        pwd.getpwnam(product.user)
        # The daemon account joins the ops group so it can read what operators write
        subprocess.run(["usermod", "-aG", product.ops_group, product.user], check=True)
    except KeyError:
        subprocess.run(["useradd", "--system", "--no-create-home",
                        "--shell", "/usr/sbin/nologin",
                        "-G", product.ops_group, product.user], check=True)
        _say("created user", product.user)


def _operators(product: Product, operators: Sequence[str]) -> None:
    """Enrol operators, which is what spares them a password on every call."""
    for operator in operators:
        try:
            pwd.getpwnam(operator)
        except KeyError:
            print(f"  {paint('skipped', '33'):<22} no such user '{operator}'")
            continue
        groups = [product.ops_group]
        try:
            grp.getgrnam(JOURNAL_GROUP)
            groups.append(JOURNAL_GROUP)
        except KeyError:
            pass
        subprocess.run(["usermod", "-aG", ",".join(groups), operator], check=True)
        _say("enrolled", f"{operator} in {', '.join(groups)} "
                         "(they must log out and back in)")


def _directories(product: Product, paths: Paths) -> None:
    for directory, mode in ((paths.etc, CONFIG_MODE),
                            (paths.instances, CONFIG_MODE),
                            (paths.logs, LOG_MODE)):
        directory.mkdir(parents=True, exist_ok=True)
        shutil.chown(directory, product.user, product.ops_group)
        directory.chmod(mode)
    _say("ready", f"{paths.etc}, {paths.instances}, {paths.logs}")


def _secrets_and_role(product: Product, paths: Paths, role: Optional[str]) -> None:
    """Seed secrets.env and the host role, neither overwritten once present."""
    if not paths.secrets.exists():
        paths.secrets.write_text(
            f"# Environment for every {product.name}@ instance. Keep credentials\n"
            f"# here rather than in a config file, which is committed.\n"
            f"# `{product.name} doctor` lists the ones this host needs.\n",
            encoding="utf-8")
        _say("created", str(paths.secrets))
    # root-owned and group-readable: requiring sudo for one file pushes people
    # into putting credentials in the instance files, which operators can read
    shutil.chown(paths.secrets, "root", product.ops_group)
    paths.secrets.chmod(SECRETS_MODE)

    host_file = paths.etc / "host"
    if not host_file.exists():
        host_file.write_text(f"{role or os.uname().nodename.split('.')[0]}\n",
                             encoding="utf-8")
        _say("recorded host", f"{host_file.read_text().strip()} in {host_file}")
    shutil.chown(host_file, "root", product.ops_group)
    host_file.chmod(0o644)


def _artefacts(product: Product, paths: Paths) -> None:
    """Render the unit, the polkit rule, the sudoers drop-in and the helpers."""
    _write(product.unit_template, _render("unit.service", product, paths), 0o644)
    product.polkit_rule.parent.mkdir(parents=True, exist_ok=True)
    product.polkit_rule.parent.chmod(0o750)
    _write(product.polkit_rule, _render("polkit.rules", product, paths), 0o644)
    _write(BIN_DIR / product.name, _render("front-door.sh", product, paths), 0o755)
    # Root runs this, so it must not live in the venv, which the daemon account
    # can write to
    _write(product.enable_helper, _render("enable-helper.sh", product, paths), 0o755)
    _sudoers(product, paths)


def _sudoers(product: Product, paths: Paths) -> None:
    """Install the drop-in only if visudo accepts it."""
    content = _render("sudoers", product, paths)
    handle, temporary = tempfile.mkstemp()
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
        if subprocess.run(["visudo", "-cqf", temporary], check=False).returncode != 0:
            print(f"  {paint('refused', '31'):<22} "
                  f"generated sudoers failed validation, not installing")
            return
    finally:
        Path(temporary).unlink(missing_ok=True)
    _write(product.sudoers, content, 0o440)
    shutil.chown(product.sudoers, "root", "root")


def _hand_to_operators(tree: Path, product: Product) -> None:
    """Give a tree to the daemon account, writable by the ops group.

    Operators already decide what the daemons run, through the configs they
    edit and the restarts they trigger, so letting them pull and hotfix the
    checkout grants nothing new. It spares a sudo for every edit and lets them
    import a driver to test it.
    """
    if not tree.is_dir():
        return
    for target in (tree, *tree.rglob("*")):
        if target.is_symlink():
            continue
        shutil.chown(target, product.user, product.ops_group)
        mode = stat.S_IMODE(target.stat().st_mode)
        # Setgid on directories so anything created later keeps the group,
        # rather than the creating operator's own
        target.chmod(mode | (0o2770 if target.is_dir() else 0o660))


def _git_safe_directory(repo: Path) -> None:
    """Let operators run git here despite the tree belonging to the daemon.

    Since 2.35.2 git refuses a repository whose owner is not the caller, which
    group permissions do not satisfy.
    """
    if not (repo / ".git").exists():
        return
    listed = subprocess.run(["git", "config", "--system", "--get-all",
                             "safe.directory"], check=False, capture_output=True,
                            text=True)
    if str(repo) in listed.stdout.split():
        return
    subprocess.run(["git", "config", "--system", "--add", "safe.directory",
                    str(repo)], check=False)
    _say("git trusts", str(repo))


def _git_shared_repository(repo: Path) -> None:
    """Have git leave what it writes group-writable.

    Otherwise each commit leaves objects only their author can write, and the
    next operator to commit is refused.
    """
    config = repo / ".git" / "config"
    if not config.is_file():
        return
    current = subprocess.run(["git", "config", "--file", str(config),
                              "core.sharedRepository"], check=False,
                             capture_output=True, text=True)
    if current.stdout.strip() == "group":
        return
    subprocess.run(["git", "config", "--file", str(config),
                    "core.sharedRepository", "group"], check=False)
    _say("git shares", f"{repo} with the ops group")


def _github_token(product: Product, paths: Paths) -> None:
    """Let operators, who push hotfixes with it, read the token, and no one else."""
    token = paths.github_token
    if not token.exists():
        print(f"  {paint('no token', '33'):<22} {token}; hotfix cannot push "
              "until one is put there (see the README)")
        return
    shutil.chown(token, "root", product.ops_group)
    token.chmod(TOKEN_MODE)


def install(paths: Paths, args: argparse.Namespace) -> int:
    """Converge this host onto what the instrument needs. Root only."""
    product = paths.product
    if os.geteuid() != 0:
        print(f"{product.name}: install must run as root (sudo)")
        return 1
    if not paths.repo_instances.is_dir():
        print(f"{product.name}: no {paths.repo_instances}; "
              f"is {paths.repo} the checkout?")
        return 1

    print(f"{product.name} install: {os.uname().nodename}")
    _group_and_user(product)
    _operators(product, args.operators)
    _directories(product, paths)
    _secrets_and_role(product, paths, args.host_role)
    _github_token(product, paths)
    _artefacts(product, paths)
    # Before the hand-over, which gives back the config git rewrites as root
    _git_shared_repository(paths.repo)
    for tree in (paths.venv, paths.repo):
        _hand_to_operators(tree, product)
        _say("owned", f"{tree} by {product.user}:{product.ops_group}")
    _git_safe_directory(paths.repo)
    subprocess.run(["systemctl", "daemon-reload"], check=False)

    print(f"\nHost setup complete. Operators in {product.ops_group} can now "
          f"deploy, start and stop\nwithout a password. If anything still asks, "
          f"run: {product.name} doctor")
    # Whether an instance should come up at boot is an operational decision,
    # so report rather than enable
    pending = _pending_at_boot(paths)
    if pending:
        print(f"\nDeployed but not set to start at boot: {' '.join(pending)}")
        print(f"  {product.name} enable --now {' '.join(pending)}")
    return 0


def _pending_at_boot(paths: Paths) -> List[str]:
    """Deployed instances that will not come back after a reboot."""
    deployed = inst.deployed(paths)
    state = units.states(paths.product, deployed)
    return [name for name in deployed if state[name].enabled != "enabled"]
