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

from instrumentctl import dashboard, doctor, hotfix
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


def main(paths: Paths) -> hotfix.Checkout:
    return hotfix.Checkout.main(paths)


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
    taken = hotfix._new_branch(main(checkout), "same fix")
    run_git(checkout.repo, "branch", taken)
    assert hotfix._new_branch(main(checkout), "same fix") == f"{taken}-2"


def test_nothing_is_saved_without_a_yes(checkout: Paths,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    edit(checkout)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert hotfix.hotfix(checkout, hotfix_args("fix", yes=False)) == 1
    assert branch(checkout) == "main"
    assert hotfix.changes(main(checkout)).edited == [str(DAEMON)]


def test_new_files_go_in_only_when_accepted(checkout: Paths,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    edit(checkout)
    (checkout.repo / "daemons" / "arm" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    answers = iter(["n", "y"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert hotfix.hotfix(checkout, hotfix_args("fix", yes=False)) == 0
    assert "helper.py" not in run_git(checkout.repo, "show", "--name-only", "--format=")
    assert hotfix.changes(main(checkout)).new == ["daemons/arm/helper.py"]


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
    assert hotfix.unsent(main(checkout), name) == 1
    run_git(checkout.repo, "remote", "set-url", "origin", str(origin))
    assert hotfix.hotfix(checkout, hotfix_args(push=True)) == 0
    assert name in remote_branches(origin)
    assert hotfix.unsent(main(checkout), name) == 0


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
    assert hotfix.github_repo(main(checkout)) == "org/repo"


def test_the_token_travels_in_the_environment_not_the_command_line() -> None:
    env = hotfix._auth_env("ghp_secret")
    values = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")]
    header = next(v for v in values if v.startswith("Authorization: Basic "))
    assert base64.b64decode(header.split()[-1]) == b"x-access-token:ghp_secret"
    assert "git@github.com:" in values
    assert hotfix._auth_env(None) == {}


DRIVER = "drivers/pi"


@pytest.fixture(name="driver_origin")
def driver_origin_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "pi.git"


@pytest.fixture(name="pulls")
def pulls_fixture(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Pull requests opened, by repo; find_pull_request answers from ``merged``."""
    opened: dict = {"merged": set()}

    def fake_open(token, repo, fields):
        opened[repo] = dict(fields, token=token)
        return {"html_url": f"https://github.com/{repo}/pull/1"}

    def fake_find(_token, repo, branch):
        if repo not in opened:
            return None
        merged = branch in opened["merged"]
        return {"state": "closed" if merged else "open", "html_url": "",
                "merged_at": "2026-11-04T09:00:00Z" if merged else None}

    monkeypatch.setattr(hotfix, "github_repo",
                        lambda checkout: "org/pi" if checkout.submodule else "org/repo")
    monkeypatch.setattr(hotfix, "open_pull_request", fake_open)
    monkeypatch.setattr(hotfix, "find_pull_request", fake_find)
    return opened


@pytest.fixture(name="with_driver")
def with_driver_fixture(checkout: Paths, driver_origin: pathlib.Path,
                        tmp_path: pathlib.Path) -> Paths:
    """The checkout with a driver submodule on a detached HEAD, as a host has it."""
    with (tmp_path / "gitconfig").open("a", encoding="utf-8") as config:
        config.write('[protocol "file"]\n\tallow = always\n')
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(driver_origin)],
                   check=True)
    seed = tmp_path / "pi-seed"
    run_git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "driver.py").write_text("v1\n", encoding="utf-8")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-q", "-m", "initial")
    run_git(seed, "push", "-q", str(driver_origin), "main")
    run_git(checkout.repo, "submodule", "add", "-q", str(driver_origin), DRIVER)
    run_git(checkout.repo, "commit", "-q", "-m", "add the driver")
    run_git(checkout.repo, "push", "-q", "origin", "main")
    run_git(checkout.repo / DRIVER, "switch", "-q", "--detach")
    checkout.owner_token("org").write_text("ghp_driver\n", encoding="utf-8")
    return checkout


def driver_tree(paths: Paths) -> pathlib.Path:
    return paths.repo / DRIVER


def edit_driver(paths: Paths, text: str = "fixed\n") -> None:
    (driver_tree(paths) / "driver.py").write_text(text, encoding="utf-8")


def bump_driver(origin: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """What a developer does after the driver merge: point main at its new main."""
    dev = tmp_path / "bump"
    subprocess.run(["git", "clone", "-q", "--recurse-submodules", str(origin), str(dev)],
                   check=True)
    run_git(dev / DRIVER, "fetch", "-q", "origin")
    run_git(dev / DRIVER, "checkout", "-q", "origin/main")
    run_git(dev, "commit", "-q", "-am", "Bump the driver")
    run_git(dev, "push", "-q", "origin", "main")


def test_a_driver_hotfix_goes_to_the_drivers_repo(with_driver: Paths, origin: pathlib.Path,
                                                  driver_origin: pathlib.Path,
                                                  pulls: dict) -> None:
    before = run_git(with_driver.repo, "rev-parse", "HEAD")
    edit_driver(with_driver)
    assert hotfix.hotfix(with_driver, hotfix_args("pi hangs")) == 0
    name = branch_of(driver_tree(with_driver))
    assert name.startswith("hotfix/") and name in remote_branches(driver_origin)
    assert run_git(driver_origin, "show", f"{name}:driver.py") == "fixed"
    assert pulls["org/pi"]["token"] == "ghp_driver" and pulls["org/pi"]["base"] == "main"
    assert f"bump `{DRIVER}`" in pulls["org/pi"]["body"]
    # The checkout's pointer is left for a developer to bump
    assert "org/repo" not in pulls
    assert branch(with_driver) == "main"
    assert run_git(with_driver.repo, "rev-parse", "HEAD") == before
    assert remote_branches(origin) == ["main"]
    assert hotfix.changes(main(with_driver)) == hotfix.Changes([], [])


def branch_of(tree: pathlib.Path) -> str:
    return run_git(tree, "branch", "--show-current")


def test_a_driver_without_a_token_for_its_owner_is_refused(with_driver: Paths,
                                                           pulls: dict, capsys) -> None:
    with_driver.owner_token("org").unlink()
    edit_driver(with_driver)
    assert hotfix.hotfix(with_driver, hotfix_args("fix")) == 1
    assert "github-token-org" in capsys.readouterr().err
    assert branch_of(driver_tree(with_driver)) == ""
    assert (driver_tree(with_driver) / "driver.py").read_text() == "fixed\n"
    assert not pulls.get("org/pi")


def test_status_and_undo_cover_drivers(with_driver: Paths, capsys) -> None:
    edit_driver(with_driver)
    assert hotfix.hotfix(with_driver, hotfix_args()) == 0
    assert f"Edited in {DRIVER}" in capsys.readouterr().out
    assert hotfix.hotfix(with_driver, hotfix_args(undo=True)) == 0
    assert f"{DRIVER}/driver.py" in capsys.readouterr().out
    assert (driver_tree(with_driver) / "driver.py").read_text() == "v1\n"


def test_a_failed_driver_push_can_be_retried(with_driver: Paths, driver_origin: pathlib.Path,
                                             tmp_path: pathlib.Path, pulls: dict) -> None:
    tree = driver_tree(with_driver)
    run_git(tree, "remote", "set-url", "origin", str(tmp_path / "unreachable"))
    edit_driver(with_driver)
    assert hotfix.hotfix(with_driver, hotfix_args("fix")) == 1
    run_git(tree, "remote", "set-url", "origin", str(driver_origin))
    assert hotfix.update(with_driver, argparse.Namespace(abandon=False)) == 1
    assert hotfix.hotfix(with_driver, hotfix_args(push=True)) == 0
    assert branch_of(tree) in remote_branches(driver_origin)
    assert "org/pi" in pulls


def test_update_keeps_a_driver_hotfix_until_merged_and_bumped(
        with_driver: Paths, origin: pathlib.Path, driver_origin: pathlib.Path,
        tmp_path: pathlib.Path, pulls: dict, capsys) -> None:
    edit_driver(with_driver)
    hotfix.hotfix(with_driver, hotfix_args("fix"))
    tree = driver_tree(with_driver)
    name = branch_of(tree)
    assert hotfix.update(with_driver, argparse.Namespace(abandon=False)) == 0
    assert "not merged" in capsys.readouterr().out and branch_of(tree) == name

    merge_on_github(driver_origin, tmp_path, name)
    pulls["merged"].add(name)
    assert hotfix.update(with_driver, argparse.Namespace(abandon=False)) == 0
    assert "bump it" in capsys.readouterr().out and branch_of(tree) == name

    bump_driver(origin, tmp_path)
    assert hotfix.update(with_driver, argparse.Namespace(abandon=False)) == 0
    assert DRIVER in capsys.readouterr().out
    assert branch_of(tree) == ""
    assert (tree / "driver.py").read_text() == "fixed\n"
    assert name not in run_git(tree, "branch", "--list")


def test_abandon_leaves_a_driver_hotfix_but_keeps_it(with_driver: Paths, pulls: dict) -> None:
    edit_driver(with_driver)
    hotfix.hotfix(with_driver, hotfix_args("fix"))
    tree = driver_tree(with_driver)
    name = branch_of(tree)
    assert hotfix.update(with_driver, argparse.Namespace(abandon=True)) == 0
    assert branch_of(tree) == ""
    assert (tree / "driver.py").read_text() == "v1\n"
    assert name in run_git(tree, "branch", "--list")


def test_update_moves_drivers_onto_a_bumped_pointer(with_driver: Paths, origin: pathlib.Path,
                                                    tmp_path: pathlib.Path,
                                                    capsys) -> None:
    seed = tmp_path / "pi-seed"
    (seed / "driver.py").write_text("v2\n", encoding="utf-8")
    run_git(seed, "commit", "-q", "-am", "v2")
    run_git(seed, "push", "-q", str(tmp_path / "pi.git"), "main")
    bump_driver(origin, tmp_path)
    assert hotfix.update(with_driver, argparse.Namespace(abandon=False)) == 0
    assert (driver_tree(with_driver) / "driver.py").read_text() == "v2\n"
    assert DRIVER in capsys.readouterr().out
    assert hotfix.changes(main(with_driver)) == hotfix.Changes([], [])


def test_doctor_reports_a_driver_running_a_hotfix(with_driver: Paths, pulls: dict,
                                                  capsys) -> None:
    edit_driver(with_driver)
    hotfix.hotfix(with_driver, hotfix_args("fix"))
    capsys.readouterr()
    doctor._hotfixes(doctor.Report(), with_driver)  # pylint: disable=protected-access
    out = capsys.readouterr().out
    assert f"{DRIVER} is running hotfix/" in out
    assert "github-token-org is readable" in out


def test_the_dashboard_lists_a_driver_with_edits(with_driver: Paths) -> None:
    assert dashboard.checkout(with_driver)["submodules"] == []
    edit_driver(with_driver)
    listed = dashboard.checkout(with_driver)["submodules"]
    assert [(d["path"], d["edited"], d["hotfix"]) for d in listed] == [(DRIVER, 1, False)]


def test_update_leaves_a_driver_on_a_developers_branch(with_driver: Paths, capsys) -> None:
    run_git(driver_tree(with_driver), "switch", "-q", "-c", "feature")
    assert hotfix.update(with_driver, argparse.Namespace(abandon=False)) == 0
    assert "leaves it alone" in capsys.readouterr().out
    assert branch_of(driver_tree(with_driver)) == "feature"


def test_update_clones_a_driver_added_on_main(checkout: Paths, origin: pathlib.Path,
                                              tmp_path: pathlib.Path) -> None:
    with (tmp_path / "gitconfig").open("a", encoding="utf-8") as config:
        config.write('[protocol "file"]\n\tallow = always\n')
    seed = tmp_path / "pi-seed"
    run_git(tmp_path, "init", "-q", "-b", "main", str(seed))
    (seed / "driver.py").write_text("v1\n", encoding="utf-8")
    run_git(seed, "add", "-A")
    run_git(seed, "commit", "-q", "-m", "initial")
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True)
    run_git(dev, "submodule", "add", "-q", str(seed), DRIVER)
    run_git(dev, "commit", "-q", "-m", "add the driver")
    run_git(dev, "push", "-q", "origin", "main")
    assert hotfix.update(checkout, argparse.Namespace(abandon=False)) == 0
    assert (driver_tree(checkout) / "driver.py").read_text() == "v1\n"
