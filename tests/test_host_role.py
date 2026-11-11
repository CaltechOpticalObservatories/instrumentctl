"""install records the host role it is given and never guesses one."""
from __future__ import annotations

import argparse

import pytest

from instrumentctl import installer
from instrumentctl.instances import Paths


def install_args(role: str | None) -> argparse.Namespace:
    return argparse.Namespace(operators=[], host_role=role)


@pytest.fixture(name="as_root")
def as_root_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/usr/bin/setfacl")


@pytest.mark.usefixtures("as_root")
def test_install_without_a_role_refuses(paths: Paths, capsys) -> None:
    paths.host_file.unlink()
    assert installer.install(paths, install_args(None)) == 1
    assert "--host-role" in capsys.readouterr().out
    assert not paths.host_file.exists()


def test_the_role_given_is_recorded(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "chown", lambda *_args: None)
    paths.host_file.unlink()
    installer._secrets_and_role(  # pylint: disable=protected-access
        paths.product, paths, "feiserver")
    assert paths.host_file.read_text(encoding="utf-8") == "feiserver\n"


def test_a_recorded_role_is_kept(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "chown", lambda *_args: None)
    installer._secrets_and_role(  # pylint: disable=protected-access
        paths.product, paths, "feiserver")
    assert paths.host_file.read_text(encoding="utf-8") == "thishost\n"
