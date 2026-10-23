"""Rendering host artefacts: the files that reach a host as root."""
from __future__ import annotations

import re
import subprocess

import pytest

from instrumentctl.installer import _render
from instrumentctl.instances import Paths
from instrumentctl.product import Product

TEMPLATES = ("unit.service", "polkit.rules", "sudoers",
             "front-door.sh", "enable-helper.sh", "profile.sh")

PLACEHOLDER = re.compile(r"@[a-z_]+@")


@pytest.mark.parametrize("template", TEMPLATES)
def test_no_placeholder_survives_rendering(template: str, product: Product,
                                           paths: Paths) -> None:
    """A template gaining a variable nobody wired up would otherwise reach a
    host as a literal @user@ inside a unit file."""
    rendered = _render(template, product, paths)
    assert not PLACEHOLDER.search(rendered), PLACEHOLDER.search(rendered).group()


@pytest.mark.parametrize("template", TEMPLATES)
def test_no_other_instrument_leaks_in(template: str, product: Product,
                                      paths: Paths) -> None:
    assert "hispec" not in _render(template, product, paths).lower()


def test_the_unit_runs_the_right_thing_as_the_right_user(product: Product,
                                                         paths: Paths) -> None:
    unit = _render("unit.service", product, paths)
    assert "Description=Demo hardware daemon (%i)" in unit
    assert "User=demouser\nGroup=demouser" in unit
    assert f"EnvironmentFile={paths.etc}/instances/%i.env" in unit
    # Optional, so a host with no secrets file still starts
    assert f"EnvironmentFile=-{paths.secrets}" in unit
    assert (f"ExecStart={paths.venv}/bin/python3 {paths.repo}/daemons/"
            "${DEMO_DAEMON} -c " + str(paths.etc) + "/%i.yaml") in unit


def test_the_polkit_rule_is_scoped_to_our_units_and_group(product: Product,
                                                          paths: Paths) -> None:
    rule = _render("polkit.rules", product, paths)
    assert 'subject.isInGroup("demo-ops")' in rule
    assert 'unit.indexOf("demo@") == 0' in rule


def test_the_sudoers_line_is_scoped_to_the_one_helper(product: Product,
                                                      paths: Paths) -> None:
    line = [l for l in _render("sudoers", product, paths).splitlines()
            if l.startswith("%")]
    assert line == ["%demo-ops ALL=(root) NOPASSWD: /usr/local/sbin/demo-enable"]


def test_the_enable_helper_only_touches_our_units(product: Product,
                                                  paths: Paths) -> None:
    helper = _render("enable-helper.sh", product, paths)
    assert f"INSTANCE_DIR={paths.etc}/instances" in helper
    assert '"demo@$name.service"' in helper
    # It must refuse a name that is not a deployed instance
    assert "deploy the instance file first" in helper


def test_the_profile_puts_the_venv_first_on_path_once(product: Product, paths: Paths,
                                                      tmp_path) -> None:
    """Sourced by sh at every login, and again by nested login shells."""
    profile = tmp_path / "profile.sh"
    profile.write_text(_render("profile.sh", product, paths), encoding="utf-8")
    shown = subprocess.run(["sh", "-c", f". {profile}; . {profile}; echo $PATH"],
                           env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                           text=True, check=True).stdout.strip()
    assert shown == f"{paths.venv}/bin:/usr/bin:/bin"
