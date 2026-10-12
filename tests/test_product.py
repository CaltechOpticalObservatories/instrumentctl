"""Product: what an instrument declares, and what follows from it."""
from __future__ import annotations

import pathlib

from instrumentctl.product import Product


def test_names_and_paths_follow_from_the_product(product: Product) -> None:
    assert product.unit("red_adc") == "demo@red_adc.service"
    assert product.env_key("DAEMON") == "DEMO_DAEMON"
    assert product.unit_template == pathlib.Path("/etc/systemd/system/demo@.service")
    assert product.polkit_rule == pathlib.Path("/etc/polkit-1/rules.d/49-demo.rules")
    assert product.sudoers == pathlib.Path("/etc/sudoers.d/demo-ops")
    assert product.enable_helper == pathlib.Path("/usr/local/sbin/demo-enable")


def test_instance_of_ignores_units_that_are_not_ours(product: Product) -> None:
    assert product.instance_of("demo@red_adc.service") == "red_adc"
    assert product.instance_of("sshd.service") is None
    # A different instrument's units must not be claimed
    assert product.instance_of("other@red_adc.service") is None


def test_optional_keys_default_from_the_name(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "minimal.toml"
    config.write_text('[instrument]\nname = "bare"\n', encoding="utf-8")
    bare = Product.load(config)
    assert (bare.display_name, bare.user, bare.ops_group) == ("bare", "bare", "bare-ops")
    assert bare.env_prefix == "BARE"
    assert bare.secret_key_suffix == "_env"
    assert bare.legacy_unit_path is None


def test_legacy_paths_appear_only_when_declared(product: Product) -> None:
    assert product.legacy_unit_path is not None
    assert product.legacy_unit_path.name == "demo-daemon@.service"
    assert product.legacy_polkit_path.name == "49-demo-daemons.rules"
