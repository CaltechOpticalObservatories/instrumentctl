"""Undeploy: what is removed, and what is refused before anything is."""
from __future__ import annotations

import argparse
import subprocess
from unittest import mock

from instrumentctl import commands, units
from instrumentctl.instances import Paths


def undeploy_args(**overrides: object) -> argparse.Namespace:
    settings = dict(names=[], force=False, dry_run=False)
    settings.update(overrides)
    return argparse.Namespace(**settings)


def undeploy(paths: Paths, **overrides: object) -> int:
    """Run undeploy with systemd and the enable helper stubbed out."""
    done = subprocess.CompletedProcess([], 0, "", "")
    with mock.patch.object(units, "run", return_value=done), \
            mock.patch.object(units, "enable_helper", return_value=done):
        return commands.undeploy(paths, undeploy_args(**overrides))


def test_an_instance_not_deployed_here_is_refused(paths: Paths, capsys) -> None:
    assert undeploy(paths, names=["red_fw"]) == 1
    assert "not deployed on this host: red_fw" in capsys.readouterr().err


def test_both_deployed_files_are_removed(paths: Paths) -> None:
    assert undeploy(paths, names=["red_adc"]) == 0
    assert not (paths.instances / "red_adc.env").exists()
    assert not paths.config("red_adc").exists()
    assert (paths.instances / "blue_fw.env").exists()


def test_an_edited_config_needs_force(paths: Paths, capsys) -> None:
    """It may hold this host's real hardware values."""
    paths.config("red_adc").write_text("hardware:\n  port: 1234\n", encoding="utf-8")
    assert undeploy(paths, names=["red_adc"]) == 1
    assert "pass --force" in capsys.readouterr().err
    assert paths.config("red_adc").exists()
    assert undeploy(paths, names=["red_adc"], force=True) == 0
    assert not paths.config("red_adc").exists()


def test_a_config_the_repo_dropped_needs_force(paths: Paths) -> None:
    """The renamed-instance case: nothing in the repo to fall back on."""
    for committed in (paths.repo / "config").glob("*/red_adc.yaml"):
        committed.unlink()
    assert undeploy(paths, names=["red_adc"]) == 1
    assert undeploy(paths, names=["red_adc"], force=True) == 0


def test_dry_run_removes_nothing(paths: Paths, capsys) -> None:
    assert undeploy(paths, names=["red_adc"], dry_run=True) == 0
    assert (paths.instances / "red_adc.env").exists()
    assert "would remove" in capsys.readouterr().out
