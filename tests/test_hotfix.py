"""hotfix and update, against real git: a checkout and a bare repo standing in
for GitHub. Only the GitHub API is stubbed."""
from __future__ import annotations

import argparse
import base64
import os
import pathlib
import pwd
import subprocess
from typing import List, Optional

import pytest

from instrumentctl import hotfix
from instrumentctl.instances import Paths

DAEMON = pathlib.Path("daemons") / "arm" / "runner.py"


def run_git(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own git config (signing, hooks) out of it."""
    (tmp_path / "gitconfig").write_text(
        "[user]\n\tname = Setup\n\temail = setup@example.invalid\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(name="origin")
def origin_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "origin.git"


@pytest.fixture(name="checkout")
def checkout_fixture(paths: Paths, origin: pathlib.Path) -> Paths:
    """The fixture repo, committed on main and pushed to origin, with a token."""
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    run_git(paths.repo, "init", "-q", "-b", "main")
    (paths.repo / DAEMON).write_text("print('v1')\n", encoding="utf-8")
    run_git(paths.repo, "add", "-A")
    run_git(paths.repo, "commit", "-q", "-m", "initial")
    run_git(paths.repo, "remote", "add", "origin", str(origin))
    run_git(paths.repo, "push", "-q", "-u", "origin", "main")
    paths.github_token.write_text("ghp_test\n", encoding="utf-8")
    return paths


def hotfix_args(message: Optional[str] = None, **overrides: object) -> argparse.Namespace:
    settings = dict(message=message, push=False, undo=False, yes=True)
    settings.update(overrides)
    return argparse.Namespace(**settings)


def edit(paths: Paths, text: str = "print('fixed')\n") -> None:
    (paths.repo / DAEMON).write_text(text, encoding="utf-8")


def branch(paths: Paths) -> str:
    return run_git(paths.repo, "branch", "--show-current")


def remote_branches(origin: pathlib.Path) -> List[str]:
    return run_git(origin, "for-each-ref", "--format=%(refname:short)",
                   "refs/heads").splitlines()


def merge_on_github(origin: pathlib.Path, tmp_path: pathlib.Path, name: str) -> None:
    """What the merge button does: squash the hotfix onto main."""
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True)
    run_git(dev, "merge", "-q", "--squash", f"origin/{name}")
    run_git(dev, "commit", "-q", "-m", "Merge the hotfix (#7)")
    run_git(dev, "push", "-q", "origin", "main")


def test_a_hotfix_lands_on_its_own_branch_on_github(checkout: Paths,
                                                    origin: pathlib.Path) -> None:
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args("ADC hangs after power cycle")) == 0
    name = branch(checkout)
    assert name.startswith("hotfix/") and name.endswith("-thishost-adc-hangs-after-power-cycle")
    assert name in remote_branches(origin)
    assert run_git(origin, "show", f"{name}:{DAEMON}") == "print('fixed')"
    assert run_git(origin, "rev-parse", "main") == run_git(checkout.repo, "rev-parse", "main")


def test_the_commit_is_in_the_operators_name(checkout: Paths) -> None:
    edit(checkout)
    hotfix.hotfix(checkout, hotfix_args("fix"))
    me = pwd.getpwuid(os.getuid())
    email = run_git(checkout.repo, "log", "-1", "--format=%ae")
    assert email.startswith(f"{me.pw_name}@")
    assert "Hotfix made on thishost" in run_git(checkout.repo, "log", "-1", "--format=%b")


def test_a_second_fix_joins_the_same_branch(checkout: Paths, origin: pathlib.Path) -> None:
    edit(checkout)
    hotfix.hotfix(checkout, hotfix_args("first"))
    first = branch(checkout)
    edit(checkout, "print('fixed again')\n")
    assert hotfix.hotfix(checkout, hotfix_args("second")) == 0
    assert branch(checkout) == first
    assert run_git(origin, "show", f"{first}:{DAEMON}") == "print('fixed again')"


def test_branch_names_never_collide(checkout: Paths) -> None:
    taken = hotfix._new_branch(checkout, "same fix")
    run_git(checkout.repo, "branch", taken)
    assert hotfix._new_branch(checkout, "same fix") == f"{taken}-2"


def test_nothing_is_saved_without_a_yes(checkout: Paths,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    edit(checkout)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert hotfix.hotfix(checkout, hotfix_args("fix", yes=False)) == 1
    assert branch(checkout) == "main"
    assert hotfix.changes(checkout).edited == [str(DAEMON)]


def test_new_files_go_in_only_when_accepted(checkout: Paths,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    edit(checkout)
    (checkout.repo / "daemons" / "arm" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    answers = iter(["n", "y"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert hotfix.hotfix(checkout, hotfix_args("fix", yes=False)) == 0
    assert "helper.py" not in run_git(checkout.repo, "show", "--name-only", "--format=")
    assert hotfix.changes(checkout).new == ["daemons/arm/helper.py"]


def test_with_nothing_edited_there_is_nothing_to_save(checkout: Paths, capsys) -> None:
    assert hotfix.hotfix(checkout, hotfix_args("fix")) == 0
    assert "Nothing to save" in capsys.readouterr().out
    assert branch(checkout) == "main"


def test_a_failed_push_keeps_the_fix_and_can_be_retried(checkout: Paths,
                                                        origin: pathlib.Path,
                                                        tmp_path: pathlib.Path,
                                                        capsys) -> None:
    run_git(checkout.repo, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args("fix")) == 1
    assert "hotfix --push" in capsys.readouterr().out
    name = branch(checkout)
    assert hotfix.unsent(checkout, name) == 1
    run_git(checkout.repo, "remote", "set-url", "origin", str(origin))
    assert hotfix.hotfix(checkout, hotfix_args(push=True)) == 0
    assert name in remote_branches(origin)
    assert hotfix.unsent(checkout, name) == 0


def test_without_a_token_the_fix_is_saved_but_not_sent(checkout: Paths,
                                                       origin: pathlib.Path,
                                                       capsys) -> None:
    checkout.github_token.unlink()
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args("fix")) == 1
    assert "not sent" in capsys.readouterr().out
    assert branch(checkout).startswith("hotfix/")
    assert remote_branches(origin) == ["main"]


def test_a_pull_request_is_opened_against_main(checkout: Paths,
                                               monkeypatch: pytest.MonkeyPatch,
                                               capsys) -> None:
    opened = {}

    def fake_open(token, repo, fields):
        opened.update(fields, token=token, repo=repo)
        return {"html_url": "https://github.com/org/repo/pull/7"}

    monkeypatch.setattr(hotfix, "github_repo", lambda _paths: "org/repo")
    monkeypatch.setattr(hotfix, "open_pull_request", fake_open)
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args("ADC hangs")) == 0
    assert opened["token"] == "ghp_test" and opened["base"] == "main"
    assert opened["head"] == branch(checkout) and opened["title"] == "ADC hangs"
    assert "thishost" in opened["body"]
    assert "pull/7" in capsys.readouterr().out


def test_root_is_refused(checkout: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Files root writes into .git would lock every operator out of it."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args("fix")) == 1
    assert branch(checkout) == "main"


def test_a_checkout_mid_merge_is_refused(checkout: Paths, capsys) -> None:
    (checkout.repo / ".git" / "MERGE_HEAD").write_text(
        run_git(checkout.repo, "rev-parse", "HEAD"), encoding="utf-8")
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args("fix")) == 1
    assert "middle of a merge" in capsys.readouterr().err


def test_status_lists_unsaved_edits(checkout: Paths, capsys) -> None:
    edit(checkout)
    assert hotfix.hotfix(checkout, hotfix_args()) == 0
    out = capsys.readouterr().out
    assert "runner.py" in out and "hotfix --undo" in out


def test_undo_puts_edited_files_back_and_leaves_new_ones(checkout: Paths) -> None:
    edit(checkout)
    new = checkout.repo / "notes.txt"
    new.write_text("mine\n", encoding="utf-8")
    assert hotfix.hotfix(checkout, hotfix_args(undo=True)) == 0
    assert (checkout.repo / DAEMON).read_text() == "print('v1')\n"
    assert new.exists()


def test_update_waits_for_the_merge(checkout: Paths,
                                    monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(hotfix, "github_repo", lambda _paths: "org/repo")
    monkeypatch.setattr(hotfix, "open_pull_request", lambda *a: {"html_url": ""})
    monkeypatch.setattr(hotfix, "find_pull_request", lambda *a: {
        "state": "open", "merged_at": None, "html_url": "https://github.com/org/repo/pull/7"})
    edit(checkout)
    hotfix.hotfix(checkout, hotfix_args("fix"))
    name = branch(checkout)
    assert hotfix.update(checkout, argparse.Namespace(abandon=False)) == 1
    assert "still open" in capsys.readouterr().err
    assert branch(checkout) == name


def test_update_returns_to_main_once_merged(checkout: Paths, origin: pathlib.Path,
                                            tmp_path: pathlib.Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hotfix, "github_repo", lambda _paths: "org/repo")
    monkeypatch.setattr(hotfix, "open_pull_request", lambda *a: {"html_url": ""})
    monkeypatch.setattr(hotfix, "find_pull_request", lambda *a: {
        "state": "closed", "merged_at": "2026-10-15T09:00:00Z", "html_url": ""})
    edit(checkout)
    hotfix.hotfix(checkout, hotfix_args("fix"))
    name = branch(checkout)
    merge_on_github(origin, tmp_path, name)
    assert hotfix.update(checkout, argparse.Namespace(abandon=False)) == 0
    assert branch(checkout) == "main"
    assert (checkout.repo / DAEMON).read_text() == "print('fixed')\n"
    assert name not in run_git(checkout.repo, "branch", "--list")


def test_abandon_leaves_an_unmerged_hotfix_but_keeps_it(checkout: Paths,
                                                        origin: pathlib.Path) -> None:
    edit(checkout)
    hotfix.hotfix(checkout, hotfix_args("fix"))
    name = branch(checkout)
    assert hotfix.update(checkout, argparse.Namespace(abandon=True)) == 0
    assert branch(checkout) == "main"
    assert (checkout.repo / DAEMON).read_text() == "print('v1')\n"
    assert name in run_git(checkout.repo, "branch", "--list")
    assert name in remote_branches(origin)


def test_update_refuses_unsaved_edits(checkout: Paths, capsys) -> None:
    edit(checkout)
    assert hotfix.update(checkout, argparse.Namespace(abandon=False)) == 1
    assert "hotfix --undo" in capsys.readouterr().err


def test_update_on_main_pulls_and_says_what_changed(checkout: Paths, origin: pathlib.Path,
                                                    tmp_path: pathlib.Path,
                                                    capsys) -> None:
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True)
    (dev / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    run_git(dev, "add", "-A")
    run_git(dev, "commit", "-q", "-m", "add a dependency")
    run_git(dev, "push", "-q", "origin", "main")
    assert hotfix.update(checkout, argparse.Namespace(abandon=False)) == 0
    out = capsys.readouterr().out
    assert "pyproject.toml" in out and "venv needs updating" in out


def test_update_leaves_a_developers_branch_alone(checkout: Paths, capsys) -> None:
    run_git(checkout.repo, "switch", "-q", "-c", "feature")
    assert hotfix.update(checkout, argparse.Namespace(abandon=False)) == 1
    assert "leaves it alone" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["git@github.com:org/repo.git",
                                 "https://github.com/org/repo",
                                 "ssh://git@github.com/org/repo.git"])
def test_the_github_repo_is_read_from_the_remote(checkout: Paths, url: str) -> None:
    run_git(checkout.repo, "remote", "set-url", "origin", url)
    assert hotfix.github_repo(checkout) == "org/repo"


def test_the_token_travels_in_the_environment_not_the_command_line() -> None:
    env = hotfix._auth_env("ghp_secret")
    values = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")]
    header = next(v for v in values if v.startswith("Authorization: Basic "))
    assert base64.b64decode(header.split()[-1]) == b"x-access-token:ghp_secret"
    assert "git@github.com:" in values
    assert hotfix._auth_env(None) == {}
