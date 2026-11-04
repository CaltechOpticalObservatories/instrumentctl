"""The journalctl command line that ``logs`` hands over to."""
from __future__ import annotations

import pathlib
from typing import List

import pytest

from instrumentctl import cli
from instrumentctl.instances import Paths


class Exec(Exception):
    """Stand in for os.execvp so the test can see the command it was given."""

    def __init__(self, argv: List[str]) -> None:
        super().__init__(argv)
        self.argv = argv


def _fake_exec(_file: str, argv: List[str]) -> None:
    raise Exec(argv)


@pytest.fixture(name="journalctl")
def journalctl_fixture(tmp_path: pathlib.Path, paths: Paths, monkeypatch: pytest.MonkeyPatch):
    """Run ``demo logs ...`` and return the journalctl argv it would exec."""
    monkeypatch.setenv("DEMO_REPO_DIR", str(paths.repo))
    monkeypatch.setenv("DEMO_ETC_DIR", str(paths.etc))
    monkeypatch.setattr("os.execvp", _fake_exec)

    def run(*argv: str) -> List[str]:
        with pytest.raises(Exec) as caught:
            cli.run(tmp_path / "instrument.toml", argv=["logs", "red_adc", *argv])
        return caught.value.argv[3:]

    return run


def test_logs_follow_from_the_last_100_lines_by_default(journalctl) -> None:
    assert journalctl() == ["-f", "-n", "100"]


def test_no_follow_prints_the_last_100_lines_and_exits(journalctl) -> None:
    assert journalctl("--no-follow") == ["-n", "100"]


def test_an_explicit_line_count_wins(journalctl) -> None:
    assert journalctl("-n", "5") == ["-f", "-n", "5"]


def test_since_replaces_the_default_line_count(journalctl) -> None:
    assert journalctl("--since", "today") == ["-f", "--since", "today"]
