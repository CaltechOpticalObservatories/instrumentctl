"""Fetching the pinned telemetry binaries, and which host gets which."""
from __future__ import annotations

import dataclasses
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from instrumentctl import telemetry
from instrumentctl.instances import Paths

BINARY = b"#!/bin/sh\necho collector\n"


@dataclasses.dataclass(frozen=True)
class FakeRelease:
    """The fields of a libby Release that fetch reads."""

    binary: str
    version: str
    url: str
    sha256: str
    member: str


def tarball(member: str) -> bytes:
    """A gzipped tar holding BINARY at ``member``."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(BINARY)
        archive.addfile(info, io.BytesIO(BINARY))
    return buffer.getvalue()


def zipped(member: str) -> bytes:
    """A zip holding BINARY at ``member``."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, BINARY)
    return buffer.getvalue()


def release_for(archive: bytes, url: str, member: str, version: str = "1.0.0") -> FakeRelease:
    """A release whose checksum matches ``archive``."""
    return FakeRelease("otelcol-contrib", version, url,
                       hashlib.sha256(archive).hexdigest(), member)


def opener_for(archive: bytes) -> mock.Mock:
    """An urlopen stand-in that serves ``archive`` and counts calls."""
    return mock.Mock(side_effect=lambda url, timeout: io.BytesIO(archive))


@pytest.mark.parametrize(("pack", "url", "member"), [
    (tarball, "https://example.invalid/collector.tar.gz", "dist/otelcol-contrib"),
    (zipped, "https://example.invalid/collector.zip", "otelcol-contrib-linux-amd64"),
])
def test_a_verified_archive_installs_its_binary(tmp_path: Path, pack, url: str,
                                               member: str) -> None:
    archive = pack(member)
    assert telemetry.fetch(release_for(archive, url, member), tmp_path, opener_for(archive))
    installed = tmp_path / "otelcol-contrib"
    assert installed.read_bytes() == BINARY
    assert installed.stat().st_mode & 0o111


def test_a_checksum_mismatch_installs_nothing(tmp_path: Path) -> None:
    archive = tarball("otelcol-contrib")
    release = dataclasses.replace(
        release_for(archive, "https://example.invalid/c.tar.gz", "otelcol-contrib"),
        sha256="0" * 64)
    with pytest.raises(ValueError, match="SHA-256"):
        telemetry.fetch(release, tmp_path, opener_for(archive))
    assert list(tmp_path.iterdir()) == []


def test_the_pinned_version_is_not_fetched_twice(tmp_path: Path) -> None:
    archive = tarball("otelcol-contrib")
    release = release_for(archive, "https://example.invalid/c.tar.gz", "otelcol-contrib")
    opener = opener_for(archive)
    assert telemetry.fetch(release, tmp_path, opener)
    assert not telemetry.fetch(release, tmp_path, opener)
    assert opener.call_count == 1
    # A new pin replaces it
    assert telemetry.fetch(dataclasses.replace(release, version="1.1.0"), tmp_path, opener)


def backend_paths(paths: Paths, role: str) -> Paths:
    """``paths`` for an instrument whose backend runs on host role ``role``."""
    return dataclasses.replace(
        paths, product=dataclasses.replace(paths.product, telemetry_backend_host=role))


def test_only_the_named_role_is_the_backend_host(paths: Paths) -> None:
    assert telemetry.is_backend_host(backend_paths(paths, "thishost"))
    assert not telemetry.is_backend_host(backend_paths(paths, "otherhost"))
    assert not telemetry.is_backend_host(paths)


def test_a_host_that_neither_exports_nor_is_the_backend_needs_nothing(paths: Paths) -> None:
    assert not telemetry.wanted(backend_paths(paths, "otherhost"))
    paths.otel_env.write_text("OTEL_EXPORTER_OTLP_ENDPOINT=http://b:4318\n")
    assert telemetry.wanted(backend_paths(paths, "otherhost"))


def test_an_exporting_host_gets_only_the_agent_binary(paths: Paths) -> None:
    module = mock.Mock(RELEASES=[mock.Mock(binary="otelcol-contrib"),
                                 mock.Mock(binary="prometheus")])
    names = [r.binary for r in telemetry.releases(backend_paths(paths, "otherhost"), module)]
    assert names == ["otelcol-contrib"]
    names = [r.binary for r in telemetry.releases(backend_paths(paths, "thishost"), module)]
    assert names == ["otelcol-contrib", "prometheus"]


def test_values_cover_every_placeholder_libby_needs(paths: Paths) -> None:
    backend = pytest.importorskip("libby.telemetry_backend")
    assert set(telemetry.values(paths)) == set(backend.PLACEHOLDERS)
