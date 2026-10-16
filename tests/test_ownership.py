"""Handing the venv and checkout to operators."""
from __future__ import annotations

import os
import pathlib
import stat
from unittest import mock

import pytest

from instrumentctl import installer
from instrumentctl.product import Product


@pytest.fixture(name="tree")
def tree_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    """A venv-shaped tree: an executable, a plain file, nested directories."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib" / "pkg").mkdir(parents=True)
    (tmp_path / "bin" / "python3").touch(mode=0o755)
    (tmp_path / "lib" / "pkg" / "mod.py").touch(mode=0o644)
    return tmp_path


def hand_over(tree: pathlib.Path, product: Product) -> None:
    """Run the real mode changes; chown needs accounts a test does not have."""
    with mock.patch.object(installer.shutil, "chown", lambda *a, **k: None):
        installer._hand_to_operators(tree, product)


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_operators_can_write_every_file(tree: pathlib.Path, product: Product) -> None:
    hand_over(tree, product)
    for path in (tree / "bin" / "python3", tree / "lib" / "pkg" / "mod.py"):
        assert mode(path) & stat.S_IWGRP, path


def test_operators_can_write_every_directory(tree: pathlib.Path,
                                             product: Product) -> None:
    hand_over(tree, product)
    for path in (tree, tree / "bin", tree / "lib" / "pkg"):
        assert mode(path) & stat.S_IWGRP, path


def test_executables_stay_executable(tree: pathlib.Path, product: Product) -> None:
    """Group-write must not cost the venv its interpreter."""
    hand_over(tree, product)
    assert mode(tree / "bin" / "python3") & stat.S_IXUSR


def test_plain_files_do_not_become_executable(tree: pathlib.Path,
                                              product: Product) -> None:
    hand_over(tree, product)
    assert not mode(tree / "lib" / "pkg" / "mod.py") & stat.S_IXUSR


@pytest.mark.skipif(os.geteuid() != 0 or os.uname().sysname == "Darwin",
                    reason="macOS strips setgid for a non-root caller")
def test_directories_are_setgid(tree: pathlib.Path, product: Product) -> None:
    """So a file an operator adds keeps the group the daemon reads as."""
    hand_over(tree, product)
    assert mode(tree / "bin") & stat.S_ISGID


def test_a_missing_tree_is_not_an_error(tmp_path: pathlib.Path,
                                        product: Product) -> None:
    """install runs before the checkout exists on a fresh host."""
    hand_over(tmp_path / "absent", product)
