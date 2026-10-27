"""The dashboard: what the page is told, and what its buttons are allowed to do."""
from __future__ import annotations

import subprocess
from typing import Dict, List, Sequence

import pytest

from instrumentctl import dashboard, units
from instrumentctl.instances import Paths


class FakeSystemd:
    """Answers ``systemctl show`` from a table, and records every other call."""

    def __init__(self, active: Dict[str, str], refuse: Sequence[str] = ()) -> None:
        self.active = active
        self.refuse = refuse
        self.calls: List[List[str]] = []

    def __call__(self, cmd: Sequence[str], capture: bool = False,
                 prog: str = "") -> subprocess.CompletedProcess:
        cmd = list(cmd)
        if cmd[:2] == ["systemctl", "show"]:
            blocks = []
            for unit in cmd[4:]:
                name = unit.split("@", 1)[1].removesuffix(".service")
                blocks.append("\n".join([
                    f"Id={unit}", f"ActiveState={self.active.get(name, 'inactive')}",
                    "SubState=running", "UnitFileState=enabled",
                    "StateChangeTimestamp=Mon 2026-10-26 09:14:02 PDT",
                    "NRestarts=2", "Result=success", "ExecMainStatus=0",
                    "MainPID=4242", "MemoryCurrent=[not set]"]))
            return subprocess.CompletedProcess(cmd, 0, "\n\n".join(blocks) + "\n", "")
        self.calls.append(cmd)
        if cmd[-1].split("@", 1)[1].removesuffix(".service") in self.refuse:
            return subprocess.CompletedProcess(cmd, 1, "", "Failed: Access denied\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture(name="systemd")
def systemd_fixture(monkeypatch) -> FakeSystemd:
    fake = FakeSystemd({"red_adc": "active", "blue_fw": "failed"})
    monkeypatch.setattr(units, "run", fake)
    return fake


@pytest.fixture(name="client")
def client_fixture(paths: Paths, systemd: FakeSystemd):
    del systemd  # stubbed for the app's sake
    return dashboard.create_app(paths).test_client()


def test_the_snapshot_reports_every_deployed_instance(paths: Paths,
                                                      systemd: FakeSystemd) -> None:
    del systemd
    found = {i["name"]: i for i in dashboard.snapshot(paths)["instances"]}
    assert set(found) == {"red_adc", "blue_fw"}
    assert found["red_adc"]["active"] == "active"
    assert found["red_adc"]["subsystem"] == "red"
    assert found["red_adc"]["daemon"] == "arm/runner.py"
    assert found["red_adc"]["restarts"] == 2
    assert found["red_adc"]["pid"] == 4242
    assert found["red_adc"]["since"] == "2026-10-26 09:14:02"
    # Not tracked, rather than a number that means nothing
    assert found["red_adc"]["memory"] is None
    assert found["blue_fw"]["active"] == "failed"


def test_undeployed_lists_only_what_belongs_on_this_host(paths: Paths,
                                                         systemd: FakeSystemd) -> None:
    del systemd
    # elsewhere_arm is assigned to otherhost, so it is not missing here
    assert dashboard.snapshot(paths)["undeployed"] == ["red_fw"]


def test_a_config_edited_on_the_host_is_flagged(paths: Paths, systemd: FakeSystemd) -> None:
    del systemd
    paths.config("red_adc").write_text("hardware:\n  port: 9999\n", encoding="utf-8")
    paths.config("blue_fw").unlink()
    found = {i["name"]: i["config"] for i in dashboard.snapshot(paths)["instances"]}
    assert found == {"red_adc": "differs", "blue_fw": "missing"}


def test_the_page_carries_the_instruments_own_name(client) -> None:
    page = client.get("/").get_data(as_text=True)
    assert "<title>Demo daemons</title>" in page
    assert 'const PROG = "demo"' in page


def test_status_includes_the_checkout(client) -> None:
    body = client.get("/api/status").get_json()
    assert body["host"] == "thishost"
    # The test repo is not a git checkout, which is reported rather than fatal
    assert "not a git checkout" in body["checkout"]["problem"]


def test_a_subsystem_stops_in_reverse_order(client, systemd: FakeSystemd) -> None:
    response = client.post("/api/stop", json={"targets": ["blue_fw", "red_adc"]})
    assert response.status_code == 200
    # blue_fw has failed, so is not running and needs no stop
    assert [r["detail"] for r in response.get_json()["results"]] == ["stopped", "not running"]
    assert systemd.calls == [["systemctl", "--no-ask-password", "stop",
                              "demo@red_adc.service"]]


def test_starting_what_runs_is_left_alone(client, systemd: FakeSystemd) -> None:
    response = client.post("/api/start", json={"targets": ["red"]})
    assert response.get_json()["results"] == [
        {"name": "red_adc", "ok": True, "detail": "already running"}]
    assert not systemd.calls


def test_a_refused_action_is_reported(client, systemd: FakeSystemd, capsys) -> None:
    systemd.refuse = ["blue_fw"]
    response = client.post("/api/restart", json={"targets": ["blue_fw"]})
    assert response.status_code == 502
    assert response.get_json()["results"][0] == {
        "name": "blue_fw", "ok": False, "detail": "Failed: Access denied"}
    # With no login, the journal is the only record of who did what
    assert "restart blue_fw: Failed: Access denied" in capsys.readouterr().out


def test_only_deployed_instances_can_be_acted_on(client, systemd: FakeSystemd) -> None:
    for target in ("red_fw", "sshd", "../etc"):
        response = client.post("/api/start", json={"targets": [target]})
        assert response.status_code == 404
    assert not systemd.calls


def test_only_the_three_verbs_are_accepted(client, systemd: FakeSystemd) -> None:
    for verb in ("enable", "kill", "mask"):
        assert client.post(f"/api/{verb}", json={"targets": ["red_adc"]}).status_code == 404
    assert not systemd.calls


def test_a_form_post_from_another_page_is_refused(client, systemd: FakeSystemd) -> None:
    """A form can be posted cross-site without a preflight; JSON cannot."""
    response = client.post("/api/stop", data={"targets": "red_adc"})
    assert response.status_code == 415
    assert not systemd.calls


@pytest.mark.parametrize("body", [{}, {"targets": []}, {"targets": "red_adc"},
                                  {"targets": [1]}, [], ["red_adc"]])
def test_malformed_targets_are_refused(client, systemd: FakeSystemd, body) -> None:
    assert client.post("/api/stop", json=body).status_code == 400
    assert not systemd.calls


def test_viewers_share_one_systemctl_call(monkeypatch) -> None:
    computed = []
    clock = iter([0.0, 0.0, 0.5, 2.0, 2.0])
    monkeypatch.setattr(dashboard.time, "monotonic", lambda: next(clock))
    cached = dashboard.Cached(1.0, lambda: computed.append(1) or len(computed))
    assert [cached.get(), cached.get(), cached.get()] == [1, 1, 2]
