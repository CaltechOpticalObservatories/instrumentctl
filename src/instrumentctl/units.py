"""Thin wrappers around systemctl and the root enable helper.

Everything that touches systemd goes through ``run``, so tests can replace it.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .product import Product


def run(cmd: Sequence[str], capture: bool = False,
        prog: str = "") -> subprocess.CompletedProcess:
    """Run a command, inheriting the terminal unless ``capture``."""
    try:
        return subprocess.run(list(cmd), check=False, text=True, capture_output=capture)
    except FileNotFoundError:
        message = f"{cmd[0]}: not found"
        if not capture:
            print(f"{prog or cmd[0]}: {message}", file=sys.stderr)
        return subprocess.CompletedProcess(list(cmd), 127, "", message + "\n")


# Each attribute is one property systemd reports
@dataclass(frozen=True)
class UnitState:  # pylint: disable=too-many-instance-attributes
    """What systemd says about one instance."""

    active: str     # active, inactive, failed, activating, deactivating
    enabled: str    # enabled, disabled, or empty if systemd does not know it
    since: str      # when ``active`` last changed, as systemd prints it
    sub: str = ""           # running, exited, auto-restart, dead, ...
    restarts: int = 0       # automatic restarts since the unit was last started
    result: str = ""        # success, or why it last stopped: exit-code, signal, ...
    exit_status: Optional[int] = None
    pid: Optional[int] = None
    memory: Optional[int] = None    # bytes, when accounting is on

    @property
    def running(self) -> bool:
        """Up, or on its way up."""
        return self.active in ("active", "activating", "reloading")


UNKNOWN = UnitState(active="unknown", enabled="", since="")

_PROPERTIES = ("Id", "ActiveState", "SubState", "UnitFileState", "StateChangeTimestamp",
               "NRestarts", "Result", "ExecMainStatus", "MainPID", "MemoryCurrent")


def states(product: Product, names: List[str]) -> Dict[str, UnitState]:
    """Query every instance in one systemctl call."""
    if not names:
        return {}
    proc = run(
        ["systemctl", "show", "--no-pager", f"--property={','.join(_PROPERTIES)}",
         *[product.unit(n) for n in names]],
        capture=True,
    )
    found = {}
    for block in proc.stdout.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        name = product.instance_of(props.get("Id", ""))
        if name is None:
            continue
        found[name] = UnitState(
            active=props.get("ActiveState", "unknown"),
            enabled=props.get("UnitFileState", ""),
            since=_short_time(props.get("StateChangeTimestamp", "")),
            sub=props.get("SubState", ""),
            restarts=_number(props.get("NRestarts")) or 0,
            result=props.get("Result", ""),
            exit_status=_number(props.get("ExecMainStatus")),
            # 0 means no process, not process 0
            pid=_number(props.get("MainPID")) or None,
            memory=_number(props.get("MemoryCurrent")),
        )
    return {n: found.get(n, UNKNOWN) for n in names}


def _number(value: Optional[str]) -> Optional[int]:
    # systemd prints "[not set]", or 2**64 - 1, for a value it does not track
    if not value or not value.isdigit():
        return None
    number = int(value)
    return None if number >= 2 ** 63 else number


def _short_time(stamp: str) -> str:
    # "Mon 2026-09-21 09:14:02 HST" -> "2026-09-21 09:14:02"
    parts = stamp.split()
    return " ".join(parts[1:3]) if len(parts) >= 3 else stamp


def needless(state: UnitState, verb: str) -> Optional[str]:
    """Why ``verb`` would do nothing to a unit in ``state``, or None if it would act."""
    if verb == "start" and state.active == "active":
        return "already running"
    if verb == "stop" and not state.running:
        return "not running"
    return None


def systemctl(product: Product, verb: str, name: str) -> bool:
    """start/stop/restart one instance. Output goes to the terminal."""
    return run(["systemctl", verb, product.unit(name)],
               prog=product.name).returncode == 0


def show_status(product: Product, name: str, lines: int) -> bool:
    """Print ``systemctl status`` for one instance, straight to the terminal.

    False only if systemctl could not report on it at all: an exit code of 3
    just means the daemon is not running, which is an answer, not a failure.
    """
    rc = run(["systemctl", "status", "--no-pager", "--full", f"--lines={lines}",
              product.unit(name)], prog=product.name).returncode
    return rc in (0, 3)


def enable_helper(product: Product, args: List[str]) -> subprocess.CompletedProcess:
    """Run the root enable/disable helper without a password prompt.

    ``-n`` makes sudo fail instead of prompting, so a missing sudoers entry
    is reported as such rather than as a mystery password request.
    """
    return run(["sudo", "-n", str(product.enable_helper), *args], capture=True)
