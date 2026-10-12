"""Finding the credentials a host needs, which is what doctor reports."""
from __future__ import annotations

import os

import pytest

from instrumentctl.instances import Paths, required_secrets, secrets_set


def test_credential_keys_are_found_at_any_depth(paths: Paths) -> None:
    needed = required_secrets(paths)
    # red_adc declares one at the top of hardware:, blue_fw two levels down
    assert "DEMO_RED_PASSWORD" in needed
    assert "DEMO_TOKEN" in needed


def test_each_variable_records_why_it_is_needed(paths: Paths) -> None:
    assert needed_reason(paths, "DEMO_RED_PASSWORD") == "red_adc (hardware.password_env)"


def needed_reason(paths: Paths, variable: str) -> str:
    return required_secrets(paths)[variable][0]


def test_instances_that_are_not_deployed_are_not_scanned(paths: Paths) -> None:
    # elsewhere_arm and red_fw exist in the repo but are not deployed here
    assert all("elsewhere_arm" not in reason
               for reasons in required_secrets(paths).values()
               for reason in reasons)


def test_a_config_with_no_credentials_contributes_nothing(paths: Paths) -> None:
    assert not any("red_fw" in reason
                   for reasons in required_secrets(paths).values()
                   for reason in reasons)


def test_an_empty_value_counts_as_unset(paths: Paths) -> None:
    paths.secrets.write_text("DEMO_TOKEN=\nDEMO_RED_PASSWORD=x\n", encoding="utf-8")
    present = secrets_set(paths)
    assert not present["DEMO_TOKEN"]
    assert present["DEMO_RED_PASSWORD"] == "x"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads an unreadable file")
def test_an_unreadable_secrets_file_is_unknown_not_empty(paths: Paths) -> None:
    """Reporting every credential as missing would be worse than saying so."""
    paths.secrets.write_text("DEMO_TOKEN=x\n", encoding="utf-8")
    paths.secrets.chmod(0o000)
    try:
        assert secrets_set(paths) is None
    finally:
        paths.secrets.chmod(0o600)


def test_quotes_and_comments_are_handled(paths: Paths) -> None:
    paths.secrets.write_text(
        '# a comment\nDEMO_TOKEN="quoted value"\n\nDEMO_OTHER=bare\n', encoding="utf-8")
    present = secrets_set(paths)
    assert present["DEMO_TOKEN"] == "quoted value"
    assert present["DEMO_OTHER"] == "bare"
