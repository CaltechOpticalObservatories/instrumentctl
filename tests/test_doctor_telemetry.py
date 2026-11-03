"""The telemetry checks doctor runs once otel.env names a backend."""
from __future__ import annotations

import dataclasses
import socket
import subprocess
from importlib import metadata
from typing import Iterator, List
from unittest import mock

import pytest

from instrumentctl import doctor, telemetry, units
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
            mock.patch.object(Paths, "telemetry_bin", new_callable=mock.PropertyMock,
                              return_value=telemetry_bin(paths, collector)), \
            mock.patch.object(units, "run", run):
        doctor._telemetry(report, paths)  # pylint: disable=protected-access
    return failures


def telemetry_bin(paths: Paths, collector: bool):
    """A stand-in bin directory, holding the agent's Collector or not."""
    directory = paths.etc / "telemetry-bin"
    directory.mkdir(exist_ok=True)
    if collector:
        (directory / telemetry.AGENT_BINARY).touch()
    return directory


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


def check_backend(paths: Paths, role: str, active: str = "active") -> List[str]:
    """Run the backend section with this host as ``role``; return the FAIL lines."""
    report = doctor.Report()
    failures: List[str] = []
    real_bad = report.bad

    def bad(text: str) -> None:
        failures.append(text)
        real_bad(text)

    bin_dir = telemetry_bin(paths, collector=True)
    module = mock.Mock(RELEASES=[mock.Mock(binary="otelcol-contrib")],
                       SERVICES=("otelcol", "prometheus"))
    product = dataclasses.replace(paths.product, telemetry_backend_host="backendhost")
    with mock.patch.object(report, "bad", bad), \
            mock.patch.object(telemetry, "backend", return_value=module), \
            mock.patch.object(doctor.inst, "host_role", return_value=role), \
            mock.patch.object(Paths, "telemetry_bin", new_callable=mock.PropertyMock,
                              return_value=bin_dir), \
            mock.patch.object(units, "run", lambda command, **_: subprocess.CompletedProcess(
                command, 0, f"{active}\n", "")):
        doctor._telemetry_backend(report,  # pylint: disable=protected-access
                                  dataclasses.replace(paths, product=product))
    return failures


def test_other_hosts_skip_the_backend_checks(paths: Paths) -> None:
    assert not check_backend(paths, role="thishost", active="inactive")


def test_the_backend_host_checks_every_service(paths: Paths) -> None:
    assert check_backend(paths, role="backendhost", active="inactive") == [
        "demo-otelcol.service is inactive", "demo-prometheus.service is inactive"]
