"""Handing /opt/<name> and /etc/<name> to operators."""
from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
from typing import List
from unittest import mock

import pytest

from instrumentctl import installer
from instrumentctl.instances import Paths
from instrumentctl.product import Product


@pytest.fixture(name="tree")
def tree_fixture(tmp_path: pathlib.Path) -> pathlib.Path:
    """A venv-shaped tree: an executable, a plain file, nested directories."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "lib" / "pkg").mkdir(parents=True)
    (tmp_path / "bin" / "python3").touch(mode=0o755)
    (tmp_path / "lib" / "pkg" / "mod.py").touch(mode=0o644)
    return tmp_path


def hand_over(tree: pathlib.Path, product: Product,
              keep: tuple = ()) -> List[List[str]]:
    """Run the real mode changes; chown needs accounts a test does not have.

    Return the commands it ran, which are recorded rather than executed.
    """
    commands: List[List[str]] = []
    with mock.patch.object(installer.shutil, "chown", lambda *a, **k: None), \
            mock.patch.object(installer.subprocess, "run",
                              lambda command, **k: commands.append(command)):
        installer._hand_to_operators(tree, product, keep=keep)
    return commands


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


def test_kept_files_are_left_alone(tree: pathlib.Path, product: Product) -> None:
    """secrets.env stays root only even though it sits in a shared tree."""
    secret = tree / "secrets.env"
    secret.touch(mode=0o600)
    hand_over(tree, product, keep=(secret,))
    assert mode(secret) == 0o600


def test_the_default_acl_skips_symlinks(tree: pathlib.Path, product: Product) -> None:
    """The venv's python links into /usr, which must not be touched."""
    commands = hand_over(tree, product)
    assert commands == [["setfacl", "-R", "-P", "-m", installer.DEFAULT_ACL, str(tree)]]


@pytest.mark.skipif(shutil.which("setfacl") is None, reason="needs the acl package")
def test_new_files_are_group_writable_despite_the_umask(tree: pathlib.Path) -> None:
    subprocess.run(["setfacl", "-R", "-P", "-m", installer.DEFAULT_ACL, str(tree)],
                   check=True)
    old = os.umask(0o077)
    try:
        (tree / "lib" / "added.py").touch()
    finally:
        os.umask(old)
    assert mode(tree / "lib" / "added.py") & stat.S_IWGRP


def paths_under(product: Product, venv: str, repo: str) -> Paths:
    return Paths.from_env(product, environ={"DEMO_VENV_DIR": venv, "DEMO_REPO_DIR": repo})


def test_the_default_layout_shares_opt_and_etc(product: Product) -> None:
    paths = paths_under(product, "/opt/demo/venv", "/opt/demo/app")
    assert installer.operator_trees(paths) == [pathlib.Path("/opt/demo"),
                                               pathlib.Path("/etc/demo")]


def test_a_venv_moved_elsewhere_is_shared_too(product: Product) -> None:
    paths = paths_under(product, "/srv/venv", "/opt/demo/app")
    assert installer.operator_trees(paths)[-1] == pathlib.Path("/srv/venv")


def test_submodules_are_read_from_gitmodules(tmp_path: pathlib.Path) -> None:
    """Each one needs its own safe.directory entry."""
    (tmp_path / ".gitmodules").write_text(
        '[submodule "src/driver/pi"]\n\tpath = src/driver/pi\n\turl = https://x/pi\n'
        '[submodule "external/lib"]\n\tpath = external/lib\n\turl = https://x/lib\n',
        encoding="utf-8")
    assert installer._submodules(tmp_path) == [tmp_path / "src/driver/pi",
                                               tmp_path / "external/lib"]


def test_no_gitmodules_means_no_submodules(tmp_path: pathlib.Path) -> None:
    assert installer._submodules(tmp_path) == []
