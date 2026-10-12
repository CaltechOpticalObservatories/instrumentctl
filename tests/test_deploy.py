"""Deploy planning: what gets copied, and what is refused before anything is."""
from __future__ import annotations

import argparse

import pytest

from instrumentctl import commands
from instrumentctl.instances import Paths


def plan_args(**overrides: object) -> argparse.Namespace:
    settings = dict(names=[], new=False, force=False, any_host=False,
                    no_start=True, no_enable=True, dry_run=True)
    settings.update(overrides)
    return argparse.Namespace(**settings)


def test_a_subsystem_deploys_every_instance_in_it(paths: Paths, capsys) -> None:
    assert commands.deploy(paths, plan_args(names=["red"])) == 0
    out = capsys.readouterr().out
    assert "red_adc" in out and "red_fw" in out


def test_an_instance_assigned_elsewhere_is_refused(paths: Paths, capsys) -> None:
    assert commands.deploy(paths, plan_args(names=["elsewhere_arm"])) == 1
    assert "belongs on 'otherhost'" in capsys.readouterr().err


def test_any_host_overrides_the_assignment(paths: Paths) -> None:
    assert commands.deploy(paths, plan_args(names=["elsewhere_arm"],
                                            any_host=True)) == 0


def test_one_bad_target_copies_nothing(paths: Paths, capsys) -> None:
    """A typo in the third name must not leave the first two half-applied."""
    assert commands.deploy(paths, plan_args(names=["red_adc", "nonexistent"])) == 1
    assert "red_adc" not in capsys.readouterr().out


def test_an_instance_whose_config_is_missing_is_refused(paths: Paths, capsys) -> None:
    (paths.repo / "config" / "arm" / "red_fw.yaml").unlink()
    assert commands.deploy(paths, plan_args(names=["red_fw"])) == 1
    assert "expected one" in capsys.readouterr().err


def test_deploy_with_no_targets_asks_for_one(paths: Paths, capsys) -> None:
    assert commands.deploy(paths, plan_args()) == 2
    assert "name the instances to deploy" in capsys.readouterr().err


def test_new_deploys_only_what_is_not_here_yet(paths: Paths, capsys) -> None:
    assert commands.deploy(paths, plan_args(new=True, any_host=True)) == 0
    out = capsys.readouterr().out
    # red_adc and blue_fw are already deployed by the fixture
    assert "red_fw" in out and "elsewhere_arm" in out
    assert "would copy" in out


def test_a_deployed_config_that_differs_is_kept(paths: Paths, capsys) -> None:
    """It holds this host's real hardware values; the repo copy does not."""
    (paths.etc / "red_adc.yaml").write_text("hardware:\n  port: 9999\n", encoding="utf-8")
    commands.deploy(paths, plan_args(names=["red_adc"], dry_run=False))
    assert "kept" in capsys.readouterr().out
    assert "9999" in (paths.etc / "red_adc.yaml").read_text()


def test_force_replaces_it(paths: Paths) -> None:
    (paths.etc / "red_adc.yaml").write_text("hardware:\n  port: 9999\n", encoding="utf-8")
    commands.deploy(paths, plan_args(names=["red_adc"], force=True, dry_run=False))
    assert "9999" not in (paths.etc / "red_adc.yaml").read_text()


def test_the_instance_file_always_follows_the_repo(paths: Paths) -> None:
    """It only points at things, so a host has no reason to diverge."""
    deployed = paths.instances / "red_adc.env"
    deployed.write_text("DEMO_DAEMON=stale/thing.py\n", encoding="utf-8")
    commands.deploy(paths, plan_args(names=["red_adc"], dry_run=False))
    assert "stale/thing.py" not in deployed.read_text()
