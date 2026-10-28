"""Run the dashboard against a pretend host, to see it without systemd.

    uv run --extra test python scripts/dashboard_demo.py [--port 8750] [--hotfix]

Builds a throwaway host tree (repo checkout, /etc, instance files) in a
temporary directory and replaces systemctl with an in-memory one, so start,
stop and restart work and take a moment, as real daemons do. One instance
crashes every 30 seconds and is restarted, to show what a flapping daemon
looks like; one starts out failed; one is refused, as polkit would refuse an
account outside the ops group.
"""
from __future__ import annotations

import argparse
import itertools
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from instrumentctl import dashboard, units
from instrumentctl.instances import Paths
from instrumentctl.product import Product

INSTRUMENT_TOML = """\
[instrument]
name = "demo"
display_name = "Demo Spectrograph"
"""

# name -> state at start; the last is in the repo for this host, not deployed
INSTANCES = {
    "power_pdu1": "active",
    "power_pdu2": "active",
    "fei_adc": "active",
    "fei_atc": "active",
    "fei_fw": "inactive",
    "cam_ccd": "failed",
    "guider_tt": None,
}
FLAKY = "fei_atc"           # crashes every CRASH_EVERY seconds, then restarts
REFUSED = "power_pdu2"      # every action on it is denied
CRASH_EVERY = 30.0
TRANSITION = 1.5            # seconds a start or stop takes
RESTART_DELAY = 5.0         # RestartSec= in the real unit
pids = itertools.count(41000)


# One attribute per property the real systemctl show reports
@dataclass
class Unit:  # pylint: disable=too-many-instance-attributes
    """One pretend daemon."""

    active: str
    since: float
    sub: str = "dead"
    restarts: int = 0
    result: str = "success"
    exit_status: int = 0
    pid: int = 0
    # (state, when): where a start or stop in progress will land
    pending: Optional[tuple] = None

    def become(self, active: str, now: float) -> None:
        """Change state, as systemd records it."""
        self.active, self.since, self.pending = active, now, None
        self.sub = {"active": "running", "failed": "failed"}.get(active, "dead")
        self.pid = next(pids) if active == "active" else 0


class FakeSystemd:
    """Stands in for ``units.run``: answers ``show`` and acts on start/stop/restart."""

    def __init__(self, product: Product, initial: Dict[str, str]) -> None:
        self.product = product
        self.lock = threading.Lock()
        now = time.time()
        self.units = {name: Unit(active="inactive", since=now)
                      for name in initial}
        for name, state in initial.items():
            self.units[name].become(state, now)
        failed = self.units.get("cam_ccd")
        if failed:
            failed.result, failed.exit_status = "exit-code", 1

    def __call__(self, cmd: Sequence[str], capture: bool = False,
                 prog: str = "") -> subprocess.CompletedProcess:
        cmd = list(cmd)
        with self.lock:
            now = time.time()
            for name, unit in self.units.items():
                self._settle(name, unit, now)
            if cmd[:2] == ["systemctl", "show"]:
                names = [self.product.instance_of(u) for u in cmd[4:]]
                text = "\n\n".join(self._show(n) for n in names if n in self.units)
                return subprocess.CompletedProcess(cmd, 0, text + "\n", "")
            if cmd[:2] == ["systemctl", "--no-ask-password"]:
                return self._act(cmd, cmd[2], self.product.instance_of(cmd[3]), now)
        return subprocess.CompletedProcess(cmd, 1, "", f"demo: cannot fake {cmd}\n")

    @staticmethod
    def _settle(name: str, unit: Unit, now: float) -> None:
        if unit.pending and now >= unit.pending[1]:
            unit.become(unit.pending[0], now)
        if name == FLAKY and unit.active == "active" and now - unit.since >= CRASH_EVERY:
            # Restart=on-failure: systemd waits, then tries again
            unit.become("activating", now)
            unit.sub, unit.result, unit.exit_status = "auto-restart", "exit-code", 1
            unit.restarts += 1
            unit.pending = ("active", now + RESTART_DELAY)

    def _act(self, cmd: List[str], verb: str, name: Optional[str],
             now: float) -> subprocess.CompletedProcess:
        if name not in self.units:
            return subprocess.CompletedProcess(cmd, 5, "", f"Unit {cmd[3]} not found.\n")
        if name == REFUSED:
            return subprocess.CompletedProcess(
                cmd, 1, "", f"Failed to {verb} {cmd[3]}: Access denied\n")
        unit = self.units[name]
        if verb == "stop":
            unit.become("deactivating", now)
            unit.pending = ("inactive", now + TRANSITION)
        else:
            unit.become("activating", now)
            unit.sub, unit.result, unit.exit_status = "start", "success", 0
            unit.restarts = 0
            unit.pending = ("active", now + TRANSITION)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def _show(self, name: str) -> str:
        unit = self.units[name]
        stamp = time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(unit.since))
        memory = str(30_000_000 + 1_000_000 * len(name)) if unit.pid else "[not set]"
        return "\n".join([
            f"Id={self.product.unit(name)}", f"ActiveState={unit.active}",
            f"SubState={unit.sub}", "UnitFileState=enabled",
            f"StateChangeTimestamp={stamp}", f"NRestarts={unit.restarts}",
            f"Result={unit.result}", f"ExecMainStatus={unit.exit_status}",
            f"MainPID={unit.pid}", f"MemoryCurrent={memory}"])


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=demo",
                    "-c", "user.email=demo@example.invalid", *args],
                   check=True, capture_output=True)


def build_host(root: Path, hotfix: bool) -> Paths:
    """A checkout, an /etc with most instances deployed, and the instrument.toml."""
    repo, etc = root / "repo", root / "etc"
    (repo / "systemd" / "instances").mkdir(parents=True)
    (etc / "instances").mkdir(parents=True)
    (etc / "host").write_text("demohost\n", encoding="utf-8")
    for name, state in INSTANCES.items():
        subsystem, device = name.split("_", 1)
        env = repo / "systemd" / "instances" / f"{name}.env"
        env.write_text(f"DEMO_DAEMON={subsystem}/{device}_daemon.py\n"
                       "DEMO_HOST=demohost\n", encoding="utf-8")
        config = repo / "config" / subsystem / f"{name}.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("hardware:\n  port: 9000\n", encoding="utf-8")
        if state is not None:
            (etc / "instances" / env.name).write_bytes(env.read_bytes())
            (etc / config.name).write_bytes(config.read_bytes())
    # Hardware values tuned on the host, as the dashboard should point out
    (etc / "cam_ccd.yaml").write_text("hardware:\n  port: 9001\n", encoding="utf-8")

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial")
    if hotfix:
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(repo, "checkout", "-q", "-b", "hotfix/2026-10-27-demohost-adc-hangs")
        (repo / "config" / "fei" / "fei_adc.yaml").write_text(
            "hardware:\n  port: 9002\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "ADC hangs after power cycle")
        (repo / "notes.txt").write_text("unsaved\n", encoding="utf-8")

    (root / "instrument.toml").write_text(INSTRUMENT_TOML, encoding="utf-8")
    product = Product.load(root / "instrument.toml")
    return Paths.from_env(product, environ={
        "DEMO_REPO_DIR": str(repo), "DEMO_ETC_DIR": str(etc),
        "DEMO_VENV_DIR": str(root / "venv"), "DEMO_LOGS_DIR": str(root / "logs"),
    })


def main(argv: Optional[List[str]] = None) -> int:
    """Build the pretend host and serve the dashboard until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8750)
    parser.add_argument("--hotfix", action="store_true",
                        help="put the checkout on a hotfix branch with an unsaved edit")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="dashboard-demo-") as tmp:
        paths = build_host(Path(tmp), args.hotfix)
        units.run = FakeSystemd(paths.product, {n: s for n, s in INSTANCES.items() if s})
        print(f"pretend host in {tmp}; {FLAKY} crashes every {CRASH_EVERY:.0f}s, "
              f"{REFUSED} refuses every action", flush=True)
        try:
            return dashboard.dashboard(paths, args)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
