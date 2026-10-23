"""Recorded extras, which doctor checks are still in the venv."""
from __future__ import annotations

from instrumentctl.instances import (Paths, extra_requirements, missing_from_venv,
                                     recorded_extras)

REQUIRES = [
    "pyyaml",
    'camera_interface_tracking @ git+https://example.invalid/cam ; extra == "atcam"',
    'pycamerad>=1.0; extra == "atcam"',
    'pytest; extra == "test"',
    'tomli; python_version < "3.11"',
]


def test_no_file_means_no_extras(paths: Paths) -> None:
    assert recorded_extras(paths) == []


def test_extras_are_read_one_per_word(paths: Paths) -> None:
    paths.extras.write_text("atcam\ntest\n", encoding="utf-8")
    assert recorded_extras(paths) == ["atcam", "test"]


def test_an_extra_names_only_its_own_requirements() -> None:
    assert extra_requirements(REQUIRES, "atcam") == ["camera_interface_tracking",
                                                     "pycamerad"]


def test_an_undeclared_extra_names_nothing() -> None:
    assert not extra_requirements(REQUIRES, "atc")


def test_only_absent_distributions_are_missing() -> None:
    assert missing_from_venv(["pyyaml", "no-such-distribution-here"]) == [
        "no-such-distribution-here"]
