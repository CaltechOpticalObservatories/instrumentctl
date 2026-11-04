"""Putting libby's telemetry backend and agent binaries on a host.

libby describes the backend in ``libby.telemetry_backend``: pinned releases,
configs and unit templates. This fetches and verifies those releases, and
supplies the values the templates need from ``instrument.toml``. The backend's
services go on the host whose role ``[telemetry] backend_host`` names; the
agent's Collector binary goes on every host that exports.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import IO, Callable, Dict, Iterator, List

from . import instances as inst
from .instances import Paths

AGENT_BINARY = "otelcol-contrib"
# Services that keep data, each under its own directory
DATA_SERVICES = ("prometheus", "tempo", "loki")
DOWNLOAD_TIMEOUT_S = 300
CHUNK_BYTES = 1 << 20

Opener = Callable[..., IO[bytes]]


def backend() -> ModuleType:
    """Return ``libby.telemetry_backend``, from the instrument's libby."""
    try:
        # Deferred: libby is the instrument's dependency, not instrumentctl's
        # pylint: disable=import-outside-toplevel
        from libby import telemetry_backend
    except ImportError as ex:
        raise RuntimeError("telemetry needs a libby with telemetry_backend in the venv; "
                           "upgrade libby") from ex
    return telemetry_backend


def is_backend_host(paths: Paths) -> bool:
    """Whether this host runs the telemetry backend."""
    role = paths.product.telemetry_backend_host
    return role is not None and inst.host_role(paths) == role


def wanted(paths: Paths) -> bool:
    """Whether this host needs any telemetry binaries at all."""
    return is_backend_host(paths) or paths.otel_env.exists()


def releases(paths: Paths, module: ModuleType) -> List:
    """The releases this host needs: all on the backend host, else the agent's."""
    if is_backend_host(paths):
        return list(module.RELEASES)
    return [release for release in module.RELEASES if release.binary == AGENT_BINARY]


def values(paths: Paths) -> Dict[str, str]:
    """The placeholders libby's backend templates read, for this instrument."""
    product = paths.product
    return {
        "name": product.name,
        "display_name": product.display_name,
        "user": product.user,
        "bin": str(paths.telemetry_bin),
        "etc": str(paths.telemetry_etc),
        "data": str(paths.telemetry_data),
        "otlp_bind": product.telemetry_otlp_bind,
        "query_bind": product.telemetry_query_bind,
        "metrics_retention": product.telemetry_metrics_retention,
        "trace_retention": product.telemetry_trace_retention,
    }


def fetch(release, bin_dir: Path, opener: Opener = urllib.request.urlopen) -> bool:
    """Install one pinned binary into ``bin_dir``; return whether it changed.

    The archive is checked against the release's SHA-256 before anything is
    extracted, and the binary replaces any older one atomically.
    """
    stamp = bin_dir / f".{release.binary}.version"
    pinned = f"{release.version} {release.sha256}\n"
    if (bin_dir / release.binary).exists() and stamp.exists() and stamp.read_text() == pinned:
        return False
    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=bin_dir) as scratch:
        archive = Path(scratch) / "archive"
        _download(release, archive, opener)
        extracted = Path(scratch) / release.binary
        with _open_member(archive, release) as source, open(extracted, "wb") as target:
            shutil.copyfileobj(source, target, CHUNK_BYTES)
        extracted.chmod(0o755)
        os.replace(extracted, bin_dir / release.binary)
    stamp.write_text(pinned)
    return True


def _download(release, archive: Path, opener: Opener) -> None:
    digest = hashlib.sha256()
    with opener(release.url, timeout=DOWNLOAD_TIMEOUT_S) as response, \
            open(archive, "wb") as target:
        while chunk := response.read(CHUNK_BYTES):
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != release.sha256:
        raise ValueError(f"{release.url}: SHA-256 is {digest.hexdigest()}, "
                         f"expected {release.sha256}; not installing it")


@contextmanager
def _open_member(archive: Path, release) -> Iterator[IO[bytes]]:
    if release.url.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle, bundle.open(release.member) as member:
            yield member
        return
    with tarfile.open(archive, "r:gz") as bundle:
        member = bundle.extractfile(release.member)
        if member is None:
            raise ValueError(f"{release.url}: {release.member} is not a file")
        with member:
            yield member
