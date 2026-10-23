"""``doctor``: check this host's setup and say what is wrong.
"""
from __future__ import annotations

import argparse
import filecmp
import grp
import os
import pwd
import socket
import stat
from importlib import metadata
from pathlib import Path
from typing import Optional

from . import hotfix
from . import instances as inst
from . import units
from .installer import _render, operator_trees
from .instances import Paths
from .output import paint


class Report:
    """Collects ok/FAIL/warn lines; FAILs decide the exit status."""

    def __init__(self) -> None:
        self.problems = 0

    @staticmethod
    def section(title: str) -> None:
        """Start a group of checks."""
        print()
        print(title)

    @staticmethod
    def ok(text: str) -> None:
        """A check that passed."""
        print(f"  {paint('ok', '32')}    {text}")

    def bad(self, text: str) -> None:
        """A check that failed: something an operator needs is missing."""
        print(f"  {paint('FAIL', '31')}  {text}")
        self.problems += 1

    @staticmethod
    def warn(text: str) -> None:
        """Worth knowing, but not broken."""
        print(f"  {paint('warn', '33')}  {text}")

    @staticmethod
    def hint(text: str) -> None:
        """The command that fixes the line above."""
        print(f"        → {text}")


def _my_groups() -> set:
    names = set()
    for gid in os.getgroups():
        try:
            names.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return names


def _is_file(path: Optional[Path]) -> bool:
    """Whether path is a file, treating one we may not stat as absent."""
    if path is None:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


def _group_exists(name: str) -> bool:
    try:
        grp.getgrnam(name)
        return True
    except KeyError:
        return False


def _owner(path: Path) -> str:
    st = path.stat()
    try:
        user = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        user = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    return f"{user}:{group} {stat.S_IMODE(st.st_mode):o}"


def _version_tuple(text: str) -> tuple:
    try:
        return tuple(int(p) for p in text.split("."))
    except ValueError:
        return ()


def _account(r: Report, paths: Paths) -> None:
    product = paths.product
    group = product.ops_group
    r.section("Your account")
    me = pwd.getpwuid(os.geteuid()).pw_name
    groups = _my_groups()
    if os.geteuid() == 0:
        r.warn("running as root, so the permission checks below say nothing about operators")
        r.hint(f"re-run as your own user: {product.name} doctor")
    if not _group_exists(group):
        r.bad(f"group {group} does not exist")
        r.hint("an admin needs to run systemd/install.sh on this host")
    elif group in groups:
        r.ok(f"you are in {group}")
    else:
        r.bad(f"you are NOT in {group}, which is why systemctl asks for a password")
        r.hint(f"admin: sudo {paths.repo}/systemd/install.sh {me}")
        r.hint(f"then log out and back in (or run: newgrp {group})")
    if "systemd-journal" in groups:
        r.ok("you are in systemd-journal (can read other units' logs)")
    else:
        r.warn(f"you are not in systemd-journal; {product.name} logs will be empty")
        r.hint(f"admin: sudo usermod -aG systemd-journal {me}")


def _install_hint(paths: Paths) -> str:
    return f"admin: sudo {paths.repo}/systemd/install.sh"


def _unit_and_polkit(r: Report, paths: Paths) -> None:
    product = paths.product
    install = _install_hint(paths)
    unit_file = product.unit_template
    polkit_rule = product.polkit_rule
    r.section("Unit and polkit")
    if unit_file.is_file():
        r.ok(f"{unit_file} installed")
        # Against what install would write now, not a committed copy: that is
        # what catches a host whose unit predates a template change
        if unit_file.read_text(encoding="utf-8") != _render("unit.service", product, paths):
            r.warn("installed unit differs from what install would write")
            r.hint(install)
    else:
        r.bad(f"{unit_file} is missing")
        r.hint(install)
    if _is_file(product.legacy_unit_path):
        r.warn(f"the old {product.legacy_unit} is still installed")
        r.hint(f"{install}   # migrates instances to {product.name}@")
    try:
        rule = polkit_rule.read_text(encoding="utf-8")
    except FileNotFoundError:
        r.bad(f"{polkit_rule} is missing, so every start/stop will prompt")
        r.hint(install)
    except OSError:
        # rules.d is 0750 root:polkitd on some hosts, so an operator cannot
        # even stat inside it; unreadable means unknown, not missing
        r.warn(f"cannot read {polkit_rule}, so the polkit rule is unchecked")
        r.hint(f"sudo {product.name} doctor")
    else:
        r.ok(f"{polkit_rule} installed")
        if rule != _render("polkit.rules", product, paths):
            r.bad("the installed polkit rule differs from what install would write")
            r.hint(f"it may be a pre-rename copy; {install}")
    legacy_rule = product.legacy_polkit_path
    if _is_file(legacy_rule):
        r.warn(f"the old {legacy_rule.name} is still installed "
               "(harmless, matches nothing now)")
        r.hint(f"admin: sudo rm {legacy_rule}")
    _polkit_daemon(r)


def _polkit_daemon(r: Report) -> None:
    proc = units.run(["pkaction", "--version"], capture=True)
    if proc.returncode == 127:
        r.warn("pkaction not found; cannot check the polkit version")
    else:
        version = (proc.stdout.split() or [""])[-1]
        # JavaScript .rules files need polkit 0.106+; older polkit ignores
        # them silently and every operator gets an auth prompt instead.
        if _version_tuple(version) >= (0, 106):
            r.ok(f"polkit {version} supports JavaScript .rules")
        else:
            r.bad(f"polkit {version or 'unknown'} is too old for .rules files (need 0.106+)")
            r.hint("the rule is ignored silently; operators will keep getting auth prompts")
    if any(units.run(["systemctl", "is-active", "--quiet", svc]).returncode == 0
           for svc in ("polkit.service", "polkitd.service")):
        r.ok("polkit is running")
    else:
        r.warn("polkit does not look like it is running")
        r.hint("admin: sudo systemctl start polkit")


def _sudo(r: Report, paths: Paths) -> None:
    product = paths.product
    install = _install_hint(paths)
    sudoers, helper = product.sudoers, str(product.enable_helper)
    if not sudoers.is_file():
        r.warn(f"no {sudoers}; enabling an instance at boot needs an admin")
        r.hint(install)
    elif os.geteuid() != 0 and product.ops_group in _my_groups():
        if units.run(["sudo", "-n", "-l", helper], capture=True).returncode == 0:
            r.ok(f"you can run {product.name} enable without a password")
        else:
            r.bad(f"sudo will not run {helper} for you without a password")
            r.hint(f"{sudoers} or {helper} is out of date; {install}")
    else:
        r.ok(f"{sudoers} installed (passwordless {product.name} enable)")


def _paths(r: Report, paths: Paths) -> None:
    install = _install_hint(paths)
    r.section("Paths")
    for directory in (paths.etc, paths.instances, paths.logs):
        if directory.is_dir():
            r.ok(f"{directory} exists ({_owner(directory)})")
        else:
            r.bad(f"{directory} is missing")
            r.hint(install)
    if paths.repo.is_dir():
        r.ok(f"{paths.repo} checked out")
    else:
        r.bad(f"{paths.repo} is missing")
    if (paths.venv / "bin" / "python3").exists():
        r.ok(f"{paths.venv} venv present")
    else:
        r.bad(f"{paths.venv}/bin/python3 is missing")
        r.hint(install)
    _extras(r, paths)
    for tree in operator_trees(paths):
        _shared_tree(r, paths, tree)
    profile = paths.product.profile_script
    if not _is_file(profile):
        r.warn(f"no {profile}, so the venv is not on operators' PATH")
        r.hint(install)
    elif profile.read_text(encoding="utf-8") != _render("profile.sh", paths.product, paths):
        r.warn(f"{profile} differs from what install would write")
        r.hint(install)
    else:
        r.ok(f"{profile} puts {paths.venv}/bin on PATH")


def _extras(r: Report, paths: Paths) -> None:
    """Whether the venv still holds the extras this host was installed with."""
    name = paths.product.name
    try:
        requirements = metadata.requires(name) or []
    except metadata.PackageNotFoundError:
        requirements = []
    for extra in inst.recorded_extras(paths):
        wanted = inst.extra_requirements(requirements, extra)
        missing = inst.missing_from_venv(wanted)
        if not wanted:
            r.warn(f"{paths.extras} names '{extra}', which {name} does not declare")
        elif missing:
            r.bad(f"extra '{extra}' is missing {' '.join(missing)} from {paths.venv}")
            r.hint(f"{_install_hint(paths)}   # a plain uv sync removes extras")
        else:
            r.ok(f"extra '{extra}' installed")


def _has_default_acl(path: Path) -> bool:
    try:
        os.getxattr(path, "system.posix_acl_default")
        return True
    except OSError:
        return False


def _shared_tree(r: Report, paths: Paths, tree: Path) -> None:
    """Whether the ops group owns a tree and keeps owning what is added to it."""
    if not tree.is_dir():
        return
    group = paths.product.ops_group
    st = tree.stat()
    owned = _group_exists(group) and st.st_gid == grp.getgrnam(group).gr_gid
    acl = _has_default_acl(tree)
    if owned and st.st_mode & stat.S_ISGID and st.st_mode & stat.S_IWGRP and acl:
        r.ok(f"{tree} is shared with {group}")
    else:
        r.bad(f"{tree} is not shared with {group} "
              f"({_owner(tree)}, default ACL {'set' if acl else 'missing'})")
        r.hint(_install_hint(paths))


def _deployed(r: Report, paths: Paths) -> None:
    r.section("Deployed instances")
    names = inst.deployed(paths)
    if not names:
        r.warn(f"no instance files in {paths.instances} — nothing is deployed on this host")
        r.hint(f"{paths.product.name} deploy <name>   "
               f"# or: {paths.product.name} deploy --new")
        return
    product = paths.product
    state = units.states(product, names)
    for name in names:
        env_file = paths.instances / f"{name}.env"
        env = inst.read_env(env_file)
        script = env.get(product.env_key("DAEMON"), "")
        config = paths.config(name)
        repo_env = paths.repo_instances / f"{name}.env"
        if env.get(product.env_key("CONFIG")):
            r.warn(f"{name}: {env_file} sets {product.env_key('CONFIG')}, "
                   f"which is no longer read; the config path is {config}")
            r.hint(f"{product.name} deploy {name}")
        if not script:
            r.bad(f"{name}: {env_file} sets no {product.env_key('DAEMON')}")
        elif not (paths.repo / "daemons" / script).is_file():
            r.bad(f"{name}: daemon script {paths.repo}/daemons/{script} does not exist")
        elif not config.is_file():
            r.bad(f"{name}: config {config} does not exist")
            r.hint(f"{product.name} deploy {name}")
        elif state[name].active == "active":
            r.ok(f"{name}: running")
        else:
            r.warn(f"{name}: {state[name].active}")
            r.hint(f"{product.name} start {name}")
            r.hint(f"{product.name} logs {name}")
        if repo_env.is_file() and not filecmp.cmp(env_file, repo_env, shallow=False):
            r.warn(f"{name}: deployed instance file differs from {repo_env}")
            r.hint(f"{product.name} deploy {name}   "
                   "# updates the instance file, keeps the config")
        elif not repo_env.is_file():
            r.warn(f"{name}: deployed here but not defined in {paths.repo_instances}")


def _secrets(r: Report, paths: Paths) -> None:
    r.section("Credentials")
    needed = inst.required_secrets(paths)
    present = inst.secrets_set(paths)
    if present is None:
        r.warn(f"cannot read {paths.secrets}, so the credentials below are unchecked")
        r.hint(f"sudo {paths.product.name} doctor")
        return
    for variable in sorted(needed):
        if present.get(variable):
            r.ok(f"{variable} is set")
        else:
            r.bad(f"{variable} is unset, needed by {'; '.join(needed[variable])}")
            r.hint(f"add {variable}=<value> to {paths.secrets}, then restart that daemon")


def _placement(r: Report, paths: Paths) -> None:
    """Flag instances running on the wrong host, or claiming no host at all.

    Only looks at what is deployed: which of the repo's instances a host runs
    is an operational decision, and ``status`` already lists the rest.
    """
    r.section("Host assignment")
    role = inst.host_role(paths)
    r.ok(f"this host is '{role}'")
    unassigned = []
    for name in inst.deployed(paths):
        claimed = inst.assigned_host(paths.product, paths.instances / f"{name}.env")
        if claimed is None:
            unassigned.append(name)
        elif claimed != role:
            r.bad(f"{name}: deployed here but assigned to '{claimed}'")
    if unassigned:
        r.warn(f"no {paths.product.env_key('HOST')}, so nothing says where they belong: "
               f"{' '.join(unassigned)}")


def _hotfixes(r: Report, paths: Paths) -> None:
    """Whether hotfix can work, and whether this host has drifted from the base."""
    product = paths.product
    name, base = product.name, product.git_base
    r.section("Hotfixes")
    token = paths.github_token
    if not _is_file(token):
        r.warn(f"no {token}, so hotfix cannot push")
        r.hint("admin: put a fine-grained GitHub token there (see the README)")
    elif hotfix.read_token(paths) is None:
        r.bad(f"cannot read {token}, so hotfix cannot push")
        r.hint(_install_hint(paths))
    else:
        r.ok(f"{token} is readable")
    problem = hotfix.checkout_problem(paths)
    if problem:
        r.warn(problem)
        return
    branch = hotfix.current_branch(paths)
    if hotfix.is_hotfix(paths, branch):
        assert branch is not None
        r.warn(f"this host is running {branch}, not {base}")
        r.hint(f"once it is merged: {name} update")
        waiting = hotfix.unsent(paths, branch)
        if waiting:
            r.warn(f"{waiting} commit(s) on {branch} are not on GitHub")
            r.hint(f"{name} hotfix --push")
    elif branch == base:
        r.ok(f"{paths.repo} is on {base}")
    else:
        r.warn(f"{paths.repo} is on {branch or 'a detached HEAD'}, not {base}")
    edited = hotfix.changes(paths).edited
    if edited:
        r.warn(f"{len(edited)} edited file(s) in {paths.repo} not saved: {' '.join(edited)}")
        r.hint(f'{name} hotfix "what you fixed"   # or: {name} hotfix --undo')


def doctor(paths: Paths, _args: argparse.Namespace) -> int:
    """Run every check and exit 1 if any FAILed."""
    me = pwd.getpwuid(os.geteuid()).pw_name
    print(f"{paths.product.name} doctor: {socket.gethostname()}, running as {me}")
    r = Report()
    _account(r, paths)
    _unit_and_polkit(r, paths)
    _sudo(r, paths)
    _paths(r, paths)
    _deployed(r, paths)
    _placement(r, paths)
    _secrets(r, paths)
    _hotfixes(r, paths)
    print()
    if r.problems == 0:
        print("No problems found.")
        return 0
    print(f"{r.problems} problem(s) found — see the FAIL lines above.")
    return 1
