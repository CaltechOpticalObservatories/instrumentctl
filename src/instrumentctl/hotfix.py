"""``hotfix`` and ``update``: change the code a host runs without the GitHub
process.

An operator edits the checkout, restarts what uses it, and runs
``hotfix "what I fixed"``. That commits the edit to a hotfix branch in their
name, pushes it, and opens a pull request against the base branch, so a
developer reviews it later while the host keeps running it. ``update`` brings
the host back to the base branch once the pull request is merged.

An edit inside a submodule, such as a driver, goes the same way in the
submodule's own repository. The checkout's pointer to it is left for a
developer to bump once that pull request is merged.

Pushing and the pull request use one fine-grained token in
``<etc>/github-token``, readable by the ops group, so an operator needs no
GitHub account of their own. A submodule under another GitHub owner uses
``<etc>/github-token-<owner>``.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import pwd
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import instances as inst
from .instances import Paths
from .output import paint

API = "https://api.github.com"
GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
# Files whose change means the venv, not just the code, needs updating
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
# What git submodule names a submodule's remote, and its branch if .gitmodules has none
SUBMODULE_REMOTE = "origin"
SUBMODULE_BASE = "main"


@dataclass(frozen=True)
class Checkout:
    """A git tree hotfix saves edits from: the instrument's own, or a submodule of it."""

    paths: Paths
    tree: Path
    remote: str
    base: str
    # Its path in the instrument's checkout; None for that checkout itself
    submodule: Optional[str] = None

    @classmethod
    def main(cls, paths: Paths) -> "Checkout":
        """The instrument's own checkout."""
        product = paths.product
        return cls(paths, paths.repo, product.git_remote, product.git_base)

    def file(self, name: str) -> str:
        """``name``, a path in this tree, as a path in the instrument's checkout."""
        return f"{self.submodule}/{name}" if self.submodule else name


def submodules(paths: Paths) -> List[Checkout]:
    """The checkout's submodules that are cloned."""
    return [checkout for checkout in _declared(paths) if (checkout.tree / ".git").exists()]


def _declared(paths: Paths) -> List[Checkout]:
    """Every submodule .gitmodules lists, cloned or not."""
    gitmodules = paths.repo / ".gitmodules"
    if not gitmodules.is_file():
        return []
    listed = subprocess.run(["git", "config", "--file", str(gitmodules), "-z", "--get-regexp",
                             r"^submodule\..*\.(path|branch)$"], check=False,
                            capture_output=True, text=True).stdout
    settings: Dict[str, Dict[str, str]] = {}
    for entry in filter(None, listed.split("\0")):
        key, _, value = entry.partition("\n")
        name, _, setting = key.removeprefix("submodule.").rpartition(".")
        settings.setdefault(name, {})[setting] = value
    return [Checkout(paths, paths.repo / setting["path"], SUBMODULE_REMOTE,
                     setting.get("branch", SUBMODULE_BASE), setting["path"])
            for setting in settings.values() if "path" in setting]


def _err(paths: Paths, message: str) -> None:
    print(f"{paths.product.name}: {message}", file=sys.stderr)


def _say(action: str, what: str, color: str = "32") -> None:
    print(f"  {paint(f'{action:<10}', color)} {what}")


def _auth_env(token: Optional[str]) -> Dict[str, str]:
    """Environment that sends ``token`` to GitHub over HTTPS.

    Through the environment rather than the command line or the remote URL,
    so it shows up neither in ps nor in .git/config. An SSH remote is
    rewritten to HTTPS, since operators have no SSH key GitHub knows.
    """
    if not token:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    settings = [
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
        ("http.https://github.com/.extraHeader", f"Authorization: Basic {basic}"),
        # An operator's own helper would otherwise be asked first
        ("credential.helper", ""),
    ]
    env = {"GIT_CONFIG_COUNT": str(len(settings))}
    for i, (key, value) in enumerate(settings):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def git(checkout: Checkout, *args: str, token: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run git in the checkout and capture its output; never prompts."""
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **_auth_env(token), **(env or {})}
    cmd = ["git", "-C", str(checkout.tree), *args]
    try:
        return subprocess.run(cmd, check=False, text=True, capture_output=True,
                              env=full_env, input=stdin)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", "git: not found\n")


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout.strip()


def _why(proc: subprocess.CompletedProcess) -> str:
    """The last thing git said, which is the reason it failed."""
    lines = (proc.stderr or proc.stdout).strip().splitlines()
    return lines[-1] if lines else f"git exited {proc.returncode}"


def checkout_problem(checkout: Checkout) -> Optional[str]:
    """Why git cannot be used in the checkout right now, or None."""
    tree = checkout.tree
    if not (tree / ".git").exists():
        return f"{tree} is not a git checkout"
    proc = git(checkout, "rev-parse", "--verify", "-q", "HEAD")
    if proc.returncode == 127:
        return "git is not installed"
    if "dubious ownership" in proc.stderr:
        return (f"git does not trust {tree} for your account; "
                f"admin: sudo {checkout.paths.repo}/systemd/install.sh")
    if proc.returncode != 0:
        return f"git cannot read {tree}: {_why(proc)}"
    for marker, what in (("MERGE_HEAD", "a merge"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
                         ("REVERT_HEAD", "a revert"), ("rebase-merge", "a rebase"),
                         ("rebase-apply", "a rebase")):
        if (tree / _out(git(checkout, "rev-parse", "--git-path", marker))).exists():
            return (f"{tree} is in the middle of {what}; "
                    "a developer needs to finish or abort it")
    return None


def current_branch(checkout: Checkout) -> Optional[str]:
    """The checked-out branch, or None on a detached HEAD."""
    return _out(git(checkout, "symbolic-ref", "--short", "-q", "HEAD")) or None


def is_hotfix(paths: Paths, branch: Optional[str]) -> bool:
    """Whether ``branch`` is one ``hotfix`` made."""
    return bool(branch) and branch.startswith(paths.product.git_branch_prefix)


class Changes(NamedTuple):
    """Edits in the checkout not yet committed."""

    edited: List[str]     # tracked files changed or deleted
    new: List[str]        # untracked files git does not ignore


def changes(checkout: Checkout) -> Changes:
    """What differs from the last commit, leaving submodules to their own checkouts."""
    edited = git(checkout, "diff", "--name-only", "-z", "--ignore-submodules=all",
                 "HEAD").stdout.split("\0")
    new = git(checkout, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    return Changes([f for f in edited if f], [f for f in new if f])


def _ref_exists(checkout: Checkout, ref: str) -> bool:
    return git(checkout, "rev-parse", "--verify", "-q", ref).returncode == 0


def unsent(checkout: Checkout, branch: str) -> int:
    """Commits on ``branch`` that are not on GitHub."""
    upstream = f"refs/remotes/{checkout.remote}/{branch}"
    if not _ref_exists(checkout, upstream):
        upstream = f"refs/remotes/{checkout.remote}/{checkout.base}"
    if not _ref_exists(checkout, upstream):
        return 0
    count = _out(git(checkout, "rev-list", "--count", f"{upstream}..{branch}"))
    return int(count) if count.isdigit() else 0


def _slugify(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def _new_branch(checkout: Checkout, message: str) -> str:
    """A branch name saying when, where and what, unused locally and on GitHub."""
    paths = checkout.paths
    stem = (f"{paths.product.git_branch_prefix}{datetime.date.today():%Y-%m-%d}-"
            f"{_slugify(inst.host_role(paths), 20) or 'host'}-"
            f"{_slugify(message, 40) or 'fix'}")
    name, n = stem, 1
    while (_ref_exists(checkout, f"refs/heads/{name}")
           or _ref_exists(checkout, f"refs/remotes/{checkout.remote}/{name}")):
        n += 1
        name = f"{stem}-{n}"
    return name


def _identity(paths: Paths) -> Dict[str, str]:
    """Commit as the operator, even though the checkout is shared."""
    entry = pwd.getpwuid(os.getuid())
    name = entry.pw_gecos.split(",")[0].strip() or entry.pw_name
    email = f"{entry.pw_name}@{paths.product.git_email_domain or os.uname().nodename}"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


class GitHubError(Exception):
    """GitHub refused, or could not be reached."""


def read_token(token: Optional[Path]) -> Optional[str]:
    """The token in the file ``token``, or None if there is none to read."""
    if token is None:
        return None
    try:
        return token.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def github_repo(checkout: Checkout) -> Optional[str]:
    """``owner/repo`` for the remote, or None if it is not on GitHub."""
    url = _out(git(checkout, "remote", "get-url", checkout.remote))
    match = GITHUB_REMOTE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def token_path(checkout: Checkout) -> Optional[Path]:
    """The token file for the checkout, or None for a submodule not on GitHub."""
    if checkout.submodule is None:
        return checkout.paths.github_token
    repo = github_repo(checkout)
    return checkout.paths.owner_token(repo.split("/")[0]) if repo else None


def _api(token: str, method: str, path: str, body: Optional[dict] = None) -> object:
    request = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "instrumentctl"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("message", "")
        except ValueError:
            detail = ""
        raise GitHubError(f"GitHub said {exc.code} {detail or exc.reason}".strip()) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise GitHubError(f"cannot reach GitHub: {getattr(exc, 'reason', exc)}") from exc


def find_pull_request(token: str, repo: str, branch: str) -> Optional[dict]:
    """The most recent pull request from ``branch``, open or not."""
    owner = repo.split("/")[0]
    query = urllib.parse.urlencode({"head": f"{owner}:{branch}", "state": "all"})
    pulls = _api(token, "GET", f"/repos/{repo}/pulls?{query}")
    return pulls[0] if isinstance(pulls, list) and pulls else None


def open_pull_request(token: str, repo: str, fields: Dict[str, str]) -> dict:
    """Open a pull request, or return the one already open from its branch.

    ``fields`` are GitHub's: head, base, title and body.
    """
    existing = find_pull_request(token, repo, fields["head"])
    if existing and existing.get("state") == "open":
        return existing
    pull = _api(token, "POST", f"/repos/{repo}/pulls", fields)
    assert isinstance(pull, dict)
    return pull


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{question} Not asked: no terminal; pass --yes to answer yes.")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _show_edits(checkout: Checkout, found: Changes) -> None:
    where = f" in {checkout.submodule}" if checkout.submodule else ""
    if found.edited:
        print(f"Edited{where}:")
        for line in _out(git(checkout, "diff", "--stat", "--ignore-submodules=all",
                             "HEAD")).splitlines():
            print(f"  {line.strip()}")
    if found.new:
        print(f"New files{where}:")
        for name in found.new:
            print(f"  {name}")


def _usable(paths: Paths, command: str) -> Optional[List[Checkout]]:
    """The checkout and its submodules, or None after explaining why git cannot run."""
    if os.geteuid() == 0:
        # Files root writes into .git would lock every operator out of it
        _err(paths, f"run {command} as your own user, not root: "
             f"{paths.product.name} {command}")
        return None
    checkouts = [Checkout.main(paths)]
    problem = checkout_problem(checkouts[0])
    if problem is None:
        checkouts += submodules(paths)
        problem = next(filter(None, map(checkout_problem, checkouts[1:])), None)
    if problem:
        _err(paths, problem)
        return None
    return checkouts


def _edits(checkouts: List[Checkout]) -> List[Tuple[Checkout, Changes]]:
    """Each checkout with anything uncommitted, and what."""
    found = [(checkout, changes(checkout)) for checkout in checkouts]
    return [(checkout, edits) for checkout, edits in found if edits.edited or edits.new]


def hotfix(paths: Paths, args: argparse.Namespace) -> int:
    """Save edits to the checkout on a hotfix branch and send them for review."""
    checkouts = _usable(paths, "hotfix")
    if checkouts is None:
        return 1
    if args.undo:
        return _undo(checkouts, args.yes)
    if args.push:
        sending = [c for c in checkouts if is_hotfix(paths, current_branch(c))]
        # The checkout alone when nothing is on a hotfix, so _send says so
        return max(_send(c, current_branch(c)) for c in sending or checkouts[:1])
    if not args.message:
        return _status(checkouts)
    return _save(checkouts, args.message, args.yes)


def _status(checkouts: List[Checkout]) -> int:
    main, drivers = checkouts[0], checkouts[1:]
    paths = main.paths
    name, base = paths.product.name, main.base
    branch = current_branch(main)
    print(f"{main.tree} is on {branch or 'a detached HEAD'}")
    if is_hotfix(paths, branch):
        assert branch is not None
        print(f"  This host is running a hotfix. Once it is merged: {name} update")
        waiting = unsent(main, branch)
        if waiting:
            print(paint(f"  {waiting} commit(s) not sent to GitHub yet: "
                        f"{name} hotfix --push", "33"))
    elif branch != base:
        print(paint(f"  not {base}, so {name} update will leave it alone", "33"))
    for driver in drivers:
        driver_branch = current_branch(driver)
        if not is_hotfix(paths, driver_branch):
            continue
        assert driver_branch is not None
        print(f"{driver.submodule} is on {driver_branch}")
        print(f"  Once it is merged and bumped in {name}: {name} update")
        waiting = unsent(driver, driver_branch)
        if waiting:
            print(paint(f"  {waiting} commit(s) not sent to GitHub yet: "
                        f"{name} hotfix --push", "33"))
    print()
    edited = _edits(checkouts)
    if not edited:
        print("No unsaved edits.")
        return 0
    for checkout, found in edited:
        _show_edits(checkout, found)
    print()
    print(f'Save and send them:  {name} hotfix "what you fixed"')
    print(f"Or put them back:    {name} hotfix --undo")
    return 0


def unsendable(checkout: Checkout) -> Optional[str]:
    """Why a submodule's edits could not reach GitHub, or None."""
    if checkout.submodule is None:
        return None
    token = token_path(checkout)
    if token is None:
        return f"{checkout.submodule} is not on GitHub, so hotfix cannot send it"
    if read_token(token) is None:
        return (f"cannot read {token}, so hotfix cannot send {checkout.submodule}; "
                "an admin needs to put a token for its GitHub owner there (see the README)")
    return None


def _save(checkouts: List[Checkout], message: str, assume_yes: bool) -> int:
    paths = checkouts[0].paths
    edited = _edits(checkouts)
    if not edited:
        print(f"Nothing to save: {paths.repo} matches its last commit.")
        return 0
    # Before anything is committed, since a submodule's commit would block update
    for checkout, _found in edited:
        problem = unsendable(checkout)
        if problem:
            _err(paths, f"{problem}; or put the edits back with: "
                 f"{paths.product.name} hotfix --undo")
            return 1
    for checkout, found in edited:
        _show_edits(checkout, found)
    with_new = False
    if any(found.new for _checkout, found in edited):
        print()
        with_new = _confirm("Include the new files?", assume_yes)
    saving = [(checkout, found.edited + (found.new if with_new else []))
              for checkout, found in edited]
    saving = [(checkout, files) for checkout, files in saving if files]
    if not saving:
        print("Nothing to save.")
        return 0
    print()
    if not _confirm("Save and send this fix?", assume_yes):
        print("Nothing saved; your edits are still in place.")
        return 1
    status = 0
    for checkout, files in saving:
        branch = _commit(checkout, message, files)
        if branch is None:
            status = 1
            continue
        where = f" in {checkout.submodule}" if checkout.submodule else ""
        _say("saved", f"{len(files)} file(s) on {branch}{where}")
        status = max(status, _send(checkout, branch))
    return status


def _commit(checkout: Checkout, message: str, files: List[str]) -> Optional[str]:
    """Commit ``files`` on a hotfix branch. Returns the branch."""
    paths = checkout.paths
    branch = current_branch(checkout)
    if not is_hotfix(paths, branch):
        # Takes the edits along; a second fix the same night joins this branch
        branch = _new_branch(checkout, message)
        proc = git(checkout, "switch", "-q", "-c", branch)
        if proc.returncode != 0:
            _err(paths, f"cannot make branch {branch}: {_why(proc)}")
            return None
    proc = git(checkout, "add", "-A", "--", *files)
    if proc.returncode != 0:
        _err(paths, f"cannot stage the edits: {_why(proc)}")
        return None
    me = pwd.getpwuid(os.getuid()).pw_name
    text = f"{message}\n\nHotfix made on {inst.host_role(paths)} by {me}.\n"
    # --no-verify: a developer's commit hooks need tools a host may not have,
    # and review catches what they would
    proc = git(checkout, "commit", "-q", "--no-verify", "-F", "-", "--", *files,
               env=_identity(paths), stdin=text)
    if proc.returncode != 0:
        _err(paths, f"cannot commit: {_why(proc)}")
        return None
    return branch


def _review_note(checkout: Checkout) -> Tuple[str, str]:
    """What the pull request says about the host, and what to do once it merges."""
    paths = checkout.paths
    name, host = paths.product.name, inst.host_role(paths)
    if checkout.submodule is None:
        return (f"Made with `{name} hotfix` on `{host}`, which runs this code until it "
                f"is merged. Once it is, run `{name} update` there to go back to "
                f"`{checkout.base}`.\n",
                f"This host keeps running the fix. Once it is merged, run: {name} update")
    return (f"Made with `{name} hotfix` on `{host}`, which runs this code as the "
            f"`{checkout.submodule}` submodule of {name} until it is merged. Once it is, "
            f"bump `{checkout.submodule}` in {name}; `{name} update` then moves the host "
            "onto it.\n",
            f"This host keeps running the fix. Once it is merged and {checkout.submodule} "
            f"is bumped in {name}, run: {name} update")


def _send(checkout: Checkout, branch: Optional[str]) -> int:
    """Push the hotfix branch and make sure a pull request is open for it."""
    paths = checkout.paths
    name = paths.product.name
    if not is_hotfix(paths, branch):
        _err(paths, f"{checkout.tree} is on {branch or 'a detached HEAD'}, "
             "not a hotfix branch, so there is nothing to send")
        return 1
    assert branch is not None
    retry = f"The fix is saved on this host. Try again with: {name} hotfix --push"
    token_file = token_path(checkout)
    token = read_token(token_file)
    if not token:
        _say("not sent", f"cannot read {token_file}", "31")
        print(retry)
        print(f"An admin needs to put a GitHub token there; run: {name} doctor")
        return 1
    proc = git(checkout, "push", "-q", "--set-upstream", checkout.remote,
               f"{branch}:refs/heads/{branch}", token=token)
    if proc.returncode != 0:
        _say("not sent", _why(proc), "31")
        print(retry)
        return 1
    _say("pushed", f"{checkout.remote} {branch}")
    return _open_review(checkout, branch, token)


def _open_review(checkout: Checkout, branch: str, token: str) -> int:
    """Make sure a pull request is open for the pushed hotfix branch."""
    repo = github_repo(checkout)
    if repo is None:
        print(f"{checkout.remote} is not on GitHub; open a pull request by hand.")
        return 0
    commits = _out(git(checkout, "log", "--reverse", "--format=%s (%an)",
                       f"{checkout.remote}/{checkout.base}..{branch}")).splitlines()
    title = commits[0].rsplit(" (", 1)[0] if commits else branch
    note, next_step = _review_note(checkout)
    body = "\n".join(f"- {line}" for line in commits) + f"\n\n{note}"
    try:
        pull = open_pull_request(token, repo, {"head": branch, "base": checkout.base,
                                               "title": title, "body": body})
    except GitHubError as exc:
        _say("no review", str(exc), "31")
        print("The fix is pushed. Open the pull request with: "
              f"{checkout.paths.product.name} hotfix --push")
        return 1
    _say("review", str(pull.get("html_url", "")))
    print()
    print(next_step)
    return 0


def _undo(checkouts: List[Checkout], assume_yes: bool) -> int:
    main = checkouts[0]
    name = main.paths.product.name
    found = _edits(checkouts)
    edited = [(checkout, edits.edited) for checkout, edits in found if edits.edited]
    if not edited:
        print(f"Nothing to undo: no file in {main.tree} differs from its last commit.")
        return 0
    for checkout, files in edited:
        _show_edits(checkout, Changes(files, []))
    print()
    if not _confirm("Discard these edits and put the files back as committed?", assume_yes):
        print("Nothing changed.")
        return 1
    for checkout, files in edited:
        proc = git(checkout, "restore", "--source=HEAD", "--staged", "--worktree", "--", *files)
        if proc.returncode != 0:
            _err(main.paths, f"cannot restore: {_why(proc)}")
            return 1
        for file in files:
            _say("restored", checkout.file(file))
    new = [checkout.file(file) for checkout, edits in found for file in edits.new]
    if new:
        print(f"New files are left alone: {' '.join(new)}")
    print()
    print(f"Running daemons still have the edited code; restart them: {name} restart <target>")
    return 0


def update(paths: Paths, args: argparse.Namespace) -> int:
    """Bring the checkout up to date with the base branch on GitHub."""
    product = paths.product
    checkouts = _usable(paths, "update")
    if checkouts is None or not _nothing_unsaved(checkouts):
        return 1
    main, drivers = checkouts[0], checkouts[1:]

    branch = current_branch(main)
    token = read_token(paths.github_token)
    leaving = is_hotfix(paths, branch)
    if leaving:
        assert branch is not None
        if not _may_leave(main, branch, token, args.abandon):
            return 1
    elif branch != product.git_base:
        _err(paths, f"{paths.repo} is on {branch or 'a detached HEAD'}, not "
             f"{product.git_base}; a developer put it there, so update leaves it alone")
        return 1

    before = _out(git(main, "rev-parse", "HEAD"))
    if not _move_to_base(main, token, leaving):
        return 1
    if leaving and not args.abandon:
        # Squash-merged, so git cannot tell it is merged; GitHub said so
        git(main, "branch", "-q", "-D", branch)
    elif leaving:
        print(f"{branch} is kept, here and on GitHub, in case it is wanted later.")
    moved = _update_submodules(main, drivers, args.abandon)
    if moved is not None:
        _report(main, before, moved)
    return 0 if moved is not None else 1


def _nothing_unsaved(checkouts: List[Checkout]) -> bool:
    """Whether every checkout is committed and sent, explaining what is not."""
    paths = checkouts[0].paths
    name = paths.product.name
    clean = True
    for checkout in checkouts:
        edited = changes(checkout).edited
        if edited:
            _err(paths, f"{len(edited)} file(s) in {checkout.tree} are edited and not saved; "
                 f'save them with: {name} hotfix "what you fixed", '
                 f"or put them back with: {name} hotfix --undo")
            clean = False
    for checkout in checkouts[1:]:
        branch = current_branch(checkout)
        waiting = unsent(checkout, branch) if branch and is_hotfix(paths, branch) else 0
        if waiting:
            _err(paths, f"{waiting} commit(s) on {branch} in {checkout.submodule} are not "
                 f"on GitHub yet; send them first: {name} hotfix --push")
            clean = False
    return clean


def _move_to_base(main: Checkout, token: Optional[str], leaving: bool) -> bool:
    """Fetch the base branch and fast-forward onto it, switching to it first."""
    base, remote = main.base, main.remote
    steps = [("fetch", ("fetch", "-q", remote, base))]
    if leaving:
        steps.append(("switch to", ("switch", "-q", base)))
    steps.append(("update", ("merge", "-q", "--ff-only", f"{remote}/{base}")))
    for what, command in steps:
        proc = git(main, *command, token=token)
        if proc.returncode != 0:
            _err(main.paths, f"cannot {what} {base}: {_why(proc)}")
            return False
    return True


def _update_submodules(main: Checkout, drivers: List[Checkout],
                       abandon: bool) -> Optional[List[str]]:
    """Check each submodule out where the checkout points, keeping unmerged hotfixes.

    Returns the submodules that moved, or None if git could not move them.
    """
    paths = main.paths
    kept, merged = set(), []
    for driver in drivers:
        branch = current_branch(driver)
        if branch is None:
            continue
        if not is_hotfix(paths, branch):
            held = "a developer put it there, so update leaves it alone"
        else:
            held = None if abandon else _still_needed(main, driver, branch)
        if held:
            _say("kept", f"{driver.submodule} on {branch}: {held}", "33")
            kept.add(driver.submodule)
        elif abandon:
            print(f"{branch} in {driver.submodule} is kept, here and on GitHub, "
                  "in case it is wanted later.")
        else:
            merged.append((driver, branch))
    # Read after the move, which may have added a submodule
    releasing = [d for d in _declared(paths) if d.submodule not in kept]
    if not releasing:
        return []
    before = {d.submodule: _out(git(d, "rev-parse", "HEAD")) if (d.tree / ".git").exists()
              else "" for d in releasing}
    proc = git(main, "submodule", "update", "--init", "--recursive", "--",
               *[str(d.submodule) for d in releasing])
    if proc.returncode != 0:
        _err(paths, f"cannot update the submodules: {_why(proc)}")
        return None
    for driver, branch in merged:
        git(driver, "branch", "-q", "-D", branch)
    return [str(d.submodule) for d in releasing
            if _out(git(d, "rev-parse", "HEAD")) != before[d.submodule]]


def _still_needed(main: Checkout, driver: Checkout, branch: str) -> Optional[str]:
    """Why a submodule must stay on its hotfix branch, or None once the fix is in."""
    token = read_token(token_path(driver))
    repo = github_repo(driver)
    if not token or repo is None:
        return "cannot tell whether it is merged: " + (
            f"cannot read {token_path(driver)}" if repo else "the remote is not GitHub")
    try:
        pull = find_pull_request(token, repo, branch)
    except GitHubError as exc:
        return f"cannot tell whether it is merged: {exc}"
    if pull is None:
        return f"it has no pull request; open one with: {main.paths.product.name} hotfix --push"
    if not pull.get("merged_at"):
        return f"its pull request is not merged: {pull.get('html_url')}"
    pointer = _out(git(main, "rev-parse", f"HEAD:{driver.submodule}"))
    tip = _out(git(driver, "rev-parse", branch))
    # Until it is bumped, the checkout points where the hotfix branched from
    behind = git(driver, "merge-base", "--is-ancestor", pointer, branch).returncode == 0
    if behind and pointer != tip:
        return (f"merged, but {main.base} still points {driver.submodule} at the commit "
                f"the hotfix started from; a developer needs to bump it in "
                f"{main.paths.product.name}")
    return None


def _report(main: Checkout, before: str, moved: List[str]) -> None:
    """Say what changed since ``before``, and what to do about it."""
    paths = main.paths
    name = paths.product.name
    after = _out(git(main, "rev-parse", "HEAD"))
    changed = [f for f in _out(git(main, "diff", "--name-only", before, after)).splitlines()
               if f]
    changed += [path for path in moved if path not in changed]
    if not changed:
        print(f"Up to date with {main.remote}/{main.base}.")
        return
    print(f"Now on {main.base} at {after[:9]}; {len(changed)} file(s) changed:")
    for file in changed:
        print(f"  {file}")
    print()
    if any(f in DEPENDENCY_FILES for f in changed):
        print(paint("Dependencies changed, so the venv needs updating: "
                    f"admin: sudo {paths.repo}/systemd/install.sh", "33"))
    print(f"Running daemons still have the old code; restart them: {name} restart <target>")


def _may_leave(main: Checkout, branch: str, token: Optional[str], abandon: bool) -> bool:
    """Whether the host may stop running a hotfix branch, explaining why not."""
    paths = main.paths
    name = paths.product.name
    waiting = unsent(main, branch)
    if waiting:
        _err(paths, f"{waiting} commit(s) on {branch} are not on GitHub yet; "
             f"send them first: {name} hotfix --push")
        return False
    if abandon:
        return True
    repo = github_repo(main)
    if not token or repo is None:
        why = f"cannot read {paths.github_token}" if not token else "the remote is not GitHub"
        _err(paths, f"cannot tell whether {branch} is merged: {why}")
        return False
    try:
        pull = find_pull_request(token, repo, branch)
    except GitHubError as exc:
        _err(paths, f"cannot tell whether {branch} is merged: {exc}")
        return False
    if pull and pull.get("merged_at"):
        return True
    if pull is None:
        state = f"it has no pull request; open one with: {name} hotfix --push"
    elif pull.get("state") == "open":
        state = f"its pull request is still open: {pull.get('html_url')}"
    else:
        state = (f"its pull request was closed without merging: {pull.get('html_url')}\n"
                 f"if a developer says to drop it: {name} update --abandon")
    _err(paths, f"this host is running {branch}, which is not merged, so it stays; {state}")
    return False
