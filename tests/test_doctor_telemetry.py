"""The telemetry checks doctor runs once otel.env names a backend."""
from __future__ import annotations

import socket
import subprocess
from importlib import metadata
from typing import Iterator, List
from unittest import mock

import pytest

from instrumentctl import doctor, units
from instrumentctl.instances import Paths


@pytest.fixture(name="backend")
def backend_fixture() -> Iterator[str]:
    """A listening loopback port standing in for the backend Collector."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"


def check(paths: Paths, *, agent: str = "active", sdk: bool = True,
          collector: bool = True) -> List[str]:
    """Run the telemetry section against a stubbed host; return the FAIL lines."""
    report = doctor.Report()
    failures: List[str] = []
    real_bad = report.bad

    def bad(text: str) -> None:
        failures.append(text)
        real_bad(text)

    def version(name: str) -> str:
        if not sdk:
            raise metadata.PackageNotFoundError(name)
        return "1.0"

    def run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, f"{agent}\n", "")

    with mock.patch.object(report, "bad", bad), \
            mock.patch.object(doctor.metadata, "version", version), \
            mock.patch.object(doctor, "OTEL_COLLECTOR", collector_path(paths, collector)), \
            mock.patch.object(units, "run", run):
        doctor._telemetry(report, paths)  # pylint: disable=protected-access
    return failures


def collector_path(paths: Paths, present: bool):
    """A stand-in for the otelcol-contrib binary, present or not."""
    binary = paths.etc / "otelcol-contrib"
    if present:
        binary.touch()
    return binary


def test_no_file_means_telemetry_is_off_not_broken(paths: Paths) -> None:
    assert not check(paths)


def test_a_file_without_the_endpoint_is_a_problem(paths: Paths) -> None:
    paths.otel_env.write_text("# nothing yet\n", encoding="utf-8")
    assert ["OTEL_EXPORTER_OTLP_ENDPOINT" in failure for failure in check(paths)] == [True]


def test_a_complete_setup_passes(paths: Paths, backend: str) -> None:
    paths.otel_env.write_text(f"OTEL_EXPORTER_OTLP_ENDPOINT={backend}\n", encoding="utf-8")
    assert not check(paths)


def test_an_unreachable_backend_is_a_problem(paths: Paths) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    paths.otel_env.write_text(f"OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:{closed}\n",
                              encoding="utf-8")
    assert ["unreachable" in failure for failure in check(paths)] == [True]


@pytest.mark.parametrize(("broken", "expected"), [
    ({"sdk": False}, "opentelemetry-sdk"),
    ({"collector": False}, "otelcol-contrib"),
    ({"agent": "inactive"}, "demo-otel-agent.service is inactive"),
])
def test_each_missing_piece_is_named(paths: Paths, backend: str, broken: dict,
                                     expected: str) -> None:
    paths.otel_env.write_text(f"OTEL_EXPORTER_OTLP_ENDPOINT={backend}\n", encoding="utf-8")
    assert [expected in failure for failure in check(paths, **broken)] == [True]
