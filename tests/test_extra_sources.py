"""Host-local clones an extra is installed from, which doctor reports."""
from __future__ import annotations

import dataclasses
import pathlib
import subprocess

import pytest

from instrumentctl import doctor
from instrumentctl.instances import Paths, extra_checkouts


def run_git(tree: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True)


@pytest.fixture(name="extra_paths")
def extra_paths_fixture(paths: Paths, tmp_path: pathlib.Path) -> Paths:
    """The fixture host with an extra dir holding one clone pushed to its origin."""
    origin, clone = tmp_path / "origin.git", tmp_path / "extra" / "camera"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True,
                   capture_output=True)
    run_git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "initial")
    run_git(clone, "push", "-q", "origin", "main")
    (tmp_path / "extra" / "notes").mkdir()
    return dataclasses.replace(paths, extra=tmp_path / "extra")


def report(paths: Paths, capsys: pytest.CaptureFixture[str]) -> str:
    doctor._extra_sources(doctor.Report(), paths)  # pylint: disable=protected-access
    return capsys.readouterr().out


def test_only_git_clones_count(extra_paths: Paths) -> None:
    assert extra_checkouts(extra_paths) == [extra_paths.extra / "camera"]


def test_no_extra_dir_means_no_section(paths: Paths, tmp_path: pathlib.Path,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    assert report(dataclasses.replace(paths, extra=tmp_path / "absent"), capsys) == ""


def test_a_clean_pushed_clone_is_still_reported(extra_paths: Paths,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    out = report(extra_paths, capsys)
    assert "camera (main) replaces the GitHub build" in out
    assert "uncommitted" not in out and "not on GitHub" not in out


def test_edits_and_unpushed_commits_are_called_out(extra_paths: Paths,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    clone = extra_paths.extra / "camera"
    run_git(clone, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
            "--allow-empty", "-m", "local")
    (clone / "edited.cpp").write_text("int x;\n", encoding="utf-8")
    out = report(extra_paths, capsys)
    assert "1 uncommitted change(s)" in out
    assert "1 commit(s)" in out


def test_a_branch_never_pushed_is_called_out(extra_paths: Paths,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    run_git(extra_paths.extra / "camera", "switch", "-q", "-c", "try-fix")
    assert "try-fix in" in report(extra_paths, capsys)
