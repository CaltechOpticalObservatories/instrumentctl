"""Turning what an operator typed into instance names."""
from __future__ import annotations

import pytest

from instrumentctl.instances import TargetError, resolve

NAMES = ["red_adc", "red_fw", "blue_fw"]


def test_a_shared_prefix_selects_the_subsystem() -> None:
    assert resolve(["red"], NAMES) == ["red_adc", "red_fw"]


def test_all_selects_everything_in_order() -> None:
    assert resolve(["all"], NAMES) == NAMES


def test_an_exact_name_wins_over_prefix_matching() -> None:
    assert resolve(["red_adc"], NAMES) == ["red_adc"]


def test_targets_keep_command_line_order() -> None:
    # `stop power fei` must not silently become `stop fei power`
    assert resolve(["blue", "red"], NAMES) == ["blue_fw", "red_adc", "red_fw"]


def test_duplicates_are_dropped_but_order_survives() -> None:
    assert resolve(["red", "red_adc"], NAMES) == ["red_adc", "red_fw"]


def test_an_unmatched_target_raises_rather_than_selecting_nothing() -> None:
    with pytest.raises(TargetError):
        resolve(["green"], NAMES)


def test_a_partial_word_does_not_match_a_longer_name() -> None:
    # "re" is not a subsystem; matching it would start daemons nobody asked for
    with pytest.raises(TargetError):
        resolve(["re"], NAMES)
