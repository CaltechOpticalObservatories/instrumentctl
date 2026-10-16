"""``hotfix`` and ``update``: change the code a host runs without the GitHub
process.

An operator edits the checkout, restarts what uses it, and runs
``hotfix "what I fixed"``. That commits the edit to a hotfix branch in their
name, pushes it, and opens a pull request against the base branch, so a
developer reviews it later while the host keeps running it. ``update`` brings
the host back to the base branch once the pull request is merged.

Pushing and the pull request use one fine-grained token in
``<etc>/github-token``, readable by the ops group, so an operator needs no
GitHub account of their own.
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
from typing import Dict, List, NamedTuple, Optional

from . import instances as inst
from .instances import Paths
from .output import paint

API = "https://api.github.com"
GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$")
# Files whose change means the venv, not just the code, needs updating
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")


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


def git(paths: Paths, *args: str, token: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run git in the checkout and capture its output; never prompts."""
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **_auth_env(token), **(env or {})}
    cmd = ["git", "-C", str(paths.repo), *args]
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


def checkout_problem(paths: Paths) -> Optional[str]:
    """Why git cannot be used in the checkout right now, or None."""
    if not (paths.repo / ".git").exists():
        return f"{paths.repo} is not a git checkout"
    proc = git(paths, "rev-parse", "--verify", "-q", "HEAD")
    if proc.returncode == 127:
        return "git is not installed"
    if "dubious ownership" in proc.stderr:
        return (f"git does not trust {paths.repo} for your account; "
                f"admin: sudo {paths.repo}/systemd/install.sh")
    if proc.returncode != 0:
        return f"git cannot read {paths.repo}: {_why(proc)}"
    for marker, what in (("MERGE_HEAD", "a merge"), ("CHERRY_PICK_HEAD", "a cherry-pick"),
                         ("REVERT_HEAD", "a revert"), ("rebase-merge", "a rebase"),
                         ("rebase-apply", "a rebase")):
        if (paths.repo / _out(git(paths, "rev-parse", "--git-path", marker))).exists():
            return (f"{paths.repo} is in the middle of {what}; "
                    "a developer needs to finish or abort it")
    return None


def current_branch(paths: Paths) -> Optional[str]:
    """The checked-out branch, or None on a detached HEAD."""
    return _out(git(paths, "symbolic-ref", "--short", "-q", "HEAD")) or None


def is_hotfix(paths: Paths, branch: Optional[str]) -> bool:
    """Whether ``branch`` is one ``hotfix`` made."""
    return bool(branch) and branch.startswith(paths.product.git_branch_prefix)


class Changes(NamedTuple):
    """Edits in the checkout not yet committed."""

    edited: List[str]     # tracked files changed or deleted
    new: List[str]        # untracked files git does not ignore


def changes(paths: Paths) -> Changes:
    """What differs from the last commit."""
    edited = git(paths, "diff", "--name-only", "-z", "HEAD").stdout.split("\0")
    new = git(paths, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    return Changes([f for f in edited if f], [f for f in new if f])


def _ref_exists(paths: Paths, ref: str) -> bool:
    return git(paths, "rev-parse", "--verify", "-q", ref).returncode == 0


def unsent(paths: Paths, branch: str) -> int:
    """Commits on ``branch`` that are not on GitHub."""
    remote = paths.product.git_remote
    upstream = f"refs/remotes/{remote}/{branch}"
    if not _ref_exists(paths, upstream):
        upstream = f"refs/remotes/{remote}/{paths.product.git_base}"
    if not _ref_exists(paths, upstream):
        return 0
    count = _out(git(paths, "rev-list", "--count", f"{upstream}..{branch}"))
    return int(count) if count.isdigit() else 0


def _slugify(text: str, limit: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def _new_branch(paths: Paths, message: str) -> str:
    """A branch name saying when, where and what, unused locally and on GitHub."""
    product = paths.product
    stem = (f"{product.git_branch_prefix}{datetime.date.today():%Y-%m-%d}-"
            f"{_slugify(inst.host_role(paths), 20) or 'host'}-"
            f"{_slugify(message, 40) or 'fix'}")
    name, n = stem, 1
    while (_ref_exists(paths, f"refs/heads/{name}")
           or _ref_exists(paths, f"refs/remotes/{product.git_remote}/{name}")):
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


def read_token(paths: Paths) -> Optional[str]:
    """The token in <etc>/github-token, or None if there is none to read."""
    try:
        return paths.github_token.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def github_repo(paths: Paths) -> Optional[str]:
    """``owner/repo`` for the remote, or None if it is not on GitHub."""
    url = _out(git(paths, "remote", "get-url", paths.product.git_remote))
    match = GITHUB_REMOTE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


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


def _show_edits(paths: Paths, found: Changes) -> None:
    if found.edited:
        print("Edited:")
        for line in _out(git(paths, "diff", "--stat", "HEAD")).splitlines():
            print(f"  {line.strip()}")
    if found.new:
        print("New files:")
        for name in found.new:
            print(f"  {name}")


def _usable(paths: Paths, command: str) -> bool:
    """Whether ``command`` can run git here, explaining why not."""
    if os.geteuid() == 0:
        # Files root writes into .git would lock every operator out of it
        _err(paths, f"run {command} as your own user, not root: "
             f"{paths.product.name} {command}")
        return False
    problem = checkout_problem(paths)
    if problem:
        _err(paths, problem)
        return False
    return True


def hotfix(paths: Paths, args: argparse.Namespace) -> int:
    """Save edits to the checkout on a hotfix branch and send them for review."""
    if not _usable(paths, "hotfix"):
        return 1
    if args.undo:
        return _undo(paths, args.yes)
    if args.push:
        return _send(paths, current_branch(paths))
    if not args.message:
        return _status(paths)
    return _save(paths, args.message, args.yes)


def _status(paths: Paths) -> int:
    name, base = paths.product.name, paths.product.git_base
    branch = current_branch(paths)
    found = changes(paths)
    print(f"{paths.repo} is on {branch or 'a detached HEAD'}")
    if is_hotfix(paths, branch):
        print(f"  This host is running a hotfix. Once it is merged: {name} update")
        waiting = unsent(paths, branch)
        if waiting:
            print(paint(f"  {waiting} commit(s) not sent to GitHub yet: "
                        f"{name} hotfix --push", "33"))
    elif branch != base:
        print(paint(f"  not {base}, so {name} update will leave it alone", "33"))
    print()
    if not found.edited and not found.new:
        print("No unsaved edits.")
        return 0
    _show_edits(paths, found)
    print()
    print(f'Save and send them:  {name} hotfix "what you fixed"')
    print(f"Or put them back:    {name} hotfix --undo")
    return 0


def _save(paths: Paths, message: str, assume_yes: bool) -> int:
    found = changes(paths)
    if not found.edited and not found.new:
        print(f"Nothing to save: {paths.repo} matches its last commit.")
        return 0
    _show_edits(paths, found)
    include = []
    if found.new:
        print()
        if _confirm("Include the new files?", assume_yes):
            include = found.new
    if not found.edited and not include:
        print("Nothing to save.")
        return 0
    print()
    if not _confirm("Save and send this fix?", assume_yes):
        print("Nothing saved; your edits are still in place.")
        return 1
    branch = _commit(paths, message, include)
    if branch is None:
        return 1
    _say("saved", f"{len(found.edited) + len(include)} file(s) on {branch}")
    return _send(paths, branch)


def _commit(paths: Paths, message: str, include: List[str]) -> Optional[str]:
    """Commit every edit, and ``include``, on a hotfix branch. Returns the branch."""
    branch = current_branch(paths)
    if not is_hotfix(paths, branch):
        # Takes the edits along; a second fix the same night joins this branch
        branch = _new_branch(paths, message)
        proc = git(paths, "switch", "-q", "-c", branch)
        if proc.returncode != 0:
            _err(paths, f"cannot make branch {branch}: {_why(proc)}")
            return None
    for proc in (git(paths, "add", "-u"),
                 git(paths, "add", "--", *include) if include else None):
        if proc is not None and proc.returncode != 0:
            _err(paths, f"cannot stage the edits: {_why(proc)}")
            return None
    me = pwd.getpwuid(os.getuid()).pw_name
    text = f"{message}\n\nHotfix made on {inst.host_role(paths)} by {me}.\n"
    # --no-verify: a developer's commit hooks need tools a host may not have,
    # and review catches what they would
    proc = git(paths, "commit", "-q", "--no-verify", "-F", "-",
               env=_identity(paths), stdin=text)
    if proc.returncode != 0:
        _err(paths, f"cannot commit: {_why(proc)}")
        return None
    return branch


def _send(paths: Paths, branch: Optional[str]) -> int:
    """Push the hotfix branch and make sure a pull request is open for it."""
    product = paths.product
    if not is_hotfix(paths, branch):
        _err(paths, f"{paths.repo} is on {branch or 'a detached HEAD'}, "
             "not a hotfix branch, so there is nothing to send")
        return 1
    assert branch is not None
    retry = f"The fix is saved on this host. Try again with: {product.name} hotfix --push"
    token = read_token(paths)
    if not token:
        _say("not sent", f"cannot read {paths.github_token}", "31")
        print(retry)
        print(f"An admin needs to put a GitHub token there; run: {product.name} doctor")
        return 1
    proc = git(paths, "push", "-q", "--set-upstream", product.git_remote,
               f"{branch}:refs/heads/{branch}", token=token)
    if proc.returncode != 0:
        _say("not sent", _why(proc), "31")
        print(retry)
        return 1
    _say("pushed", f"{product.git_remote} {branch}")

    repo = github_repo(paths)
    if repo is None:
        print(f"{product.git_remote} is not on GitHub; open a pull request by hand.")
        return 0
    base = product.git_base
    commits = _out(git(paths, "log", "--reverse", "--format=%s (%an)",
                       f"{product.git_remote}/{base}..{branch}")).splitlines()
    title = commits[0].rsplit(" (", 1)[0] if commits else branch
    body = ("\n".join(f"- {line}" for line in commits)
            + f"\n\nMade with `{product.name} hotfix` on `{inst.host_role(paths)}`, "
            "which runs this code until it is merged. Once it is, run "
            f"`{product.name} update` there to go back to `{base}`.\n")
    try:
        pull = open_pull_request(token, repo, {"head": branch, "base": base,
                                               "title": title, "body": body})
    except GitHubError as exc:
        _say("no review", str(exc), "31")
        print(f"The fix is pushed. Open the pull request with: {product.name} hotfix --push")
        return 1
    _say("review", str(pull.get("html_url", "")))
    print()
    print(f"This host keeps running the fix. Once it is merged, run: {product.name} update")
    return 0


def _undo(paths: Paths, assume_yes: bool) -> int:
    name = paths.product.name
    found = changes(paths)
    if not found.edited:
        print(f"Nothing to undo: no file in {paths.repo} differs from its last commit.")
        return 0
    _show_edits(paths, Changes(found.edited, []))
    print()
    if not _confirm("Discard these edits and put the files back as committed?", assume_yes):
        print("Nothing changed.")
        return 1
    proc = git(paths, "restore", "--source=HEAD", "--staged", "--worktree", "--", ".")
    if proc.returncode != 0:
        _err(paths, f"cannot restore: {_why(proc)}")
        return 1
    for file in found.edited:
        _say("restored", file)
    if found.new:
        print(f"New files are left alone: {' '.join(found.new)}")
    print()
    print(f"Running daemons still have the edited code; restart them: {name} restart <target>")
    return 0


def update(paths: Paths, args: argparse.Namespace) -> int:
    """Bring the checkout up to date with the base branch on GitHub."""
    product = paths.product
    if not _usable(paths, "update"):
        return 1
    found = changes(paths)
    if found.edited:
        _err(paths, f"{len(found.edited)} file(s) in {paths.repo} are edited and not saved; "
             f'save them with: {product.name} hotfix "what you fixed", '
             f"or put them back with: {product.name} hotfix --undo")
        return 1

    branch = current_branch(paths)
    token = read_token(paths)
    leaving = is_hotfix(paths, branch)
    if leaving:
        assert branch is not None
        if not _may_leave(paths, branch, token, args.abandon):
            return 1
    elif branch != product.git_base:
        _err(paths, f"{paths.repo} is on {branch or 'a detached HEAD'}, not "
             f"{product.git_base}; a developer put it there, so update leaves it alone")
        return 1

    before = _out(git(paths, "rev-parse", "HEAD"))
    if not _move_to_base(paths, token, leaving):
        return 1
    if leaving and not args.abandon:
        # Squash-merged, so git cannot tell it is merged; GitHub said so
        git(paths, "branch", "-q", "-D", branch)
    elif leaving:
        print(f"{branch} is kept, here and on GitHub, in case it is wanted later.")
    _report(paths, before)
    return 0


def _move_to_base(paths: Paths, token: Optional[str], leaving: bool) -> bool:
    """Fetch the base branch and fast-forward onto it, switching to it first."""
    base, remote = paths.product.git_base, paths.product.git_remote
    steps = [("fetch", ("fetch", "-q", remote, base))]
    if leaving:
        steps.append(("switch to", ("switch", "-q", base)))
    steps.append(("update", ("merge", "-q", "--ff-only", f"{remote}/{base}")))
    for what, command in steps:
        proc = git(paths, *command, token=token)
        if proc.returncode != 0:
            _err(paths, f"cannot {what} {base}: {_why(proc)}")
            return False
    return True


def _report(paths: Paths, before: str) -> None:
    """Say what changed since ``before``, and what to do about it."""
    product = paths.product
    after = _out(git(paths, "rev-parse", "HEAD"))
    changed = [f for f in _out(git(paths, "diff", "--name-only", before, after)).splitlines()
               if f]
    if not changed:
        print(f"Up to date with {product.git_remote}/{product.git_base}.")
        return
    print(f"Now on {product.git_base} at {after[:9]}; {len(changed)} file(s) changed:")
    for file in changed:
        print(f"  {file}")
    print()
    if any(f in DEPENDENCY_FILES for f in changed):
        print(paint("Dependencies changed, so the venv needs updating: "
                    f"admin: sudo {paths.repo}/systemd/install.sh", "33"))
    print(f"Running daemons still have the old code; restart them: "
          f"{product.name} restart <target>")


def _may_leave(paths: Paths, branch: str, token: Optional[str], abandon: bool) -> bool:
    """Whether the host may stop running a hotfix branch, explaining why not."""
    name = paths.product.name
    waiting = unsent(paths, branch)
    if waiting:
        _err(paths, f"{waiting} commit(s) on {branch} are not on GitHub yet; "
             f"send them first: {name} hotfix --push")
        return False
    if abandon:
        return True
    repo = github_repo(paths)
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
