"""The polkit probe doctor runs when an operator cannot read the rule."""
from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from instrumentctl import doctor, units
from instrumentctl.instances import Paths


def probe(paths: Paths, active: str, returncode: int) -> tuple:
    """Run the probe against a stubbed systemd; return (tested, problems, calls)."""
    report = doctor.Report()
    state = units.UnitState(active=active, enabled="enabled", since="")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, "", "")

    with mock.patch.object(doctor.os, "geteuid", return_value=1000), \
            mock.patch.object(units, "states",
                              lambda product, names: {name: state for name in names}), \
            mock.patch.object(units, "run", run):
        tested = doctor._polkit_lets_me_manage(report, paths)
    return tested, report.problems, calls


def test_a_healthy_unit_is_probed_with_a_no_op(paths: Paths) -> None:
    tested, problems, calls = probe(paths, "active", 0)
    assert tested and problems == 0
    assert calls == [["systemctl", "--no-ask-password", "reset-failed", "demo@blue_fw.service"]]


def test_a_refusal_is_a_problem(paths: Paths) -> None:
    tested, problems, _ = probe(paths, "active", 1)
    assert tested and problems == 1


def test_failed_units_are_never_reset(paths: Paths) -> None:
    """reset-failed would clear the state an operator still needs to see."""
    tested, _, calls = probe(paths, "failed", 0)
    assert not tested and not calls


@pytest.mark.parametrize("euid", [0])
def test_root_is_not_probed(paths: Paths, euid: int) -> None:
    report = doctor.Report()
    with mock.patch.object(doctor.os, "geteuid", return_value=euid):
        assert not doctor._polkit_lets_me_manage(report, paths)
