"""Product: what an instrument declares, and what follows from it."""
from __future__ import annotations

import pathlib

from instrumentctl.instances import Paths
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


def test_paths_fall_back_to_the_name_when_not_declared(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "minimal.toml"
    config.write_text('[instrument]\nname = "bare"\n', encoding="utf-8")
    paths = Paths.from_env(Product.load(config), environ={})
    assert paths.etc == pathlib.Path("/etc/bare")
    assert paths.venv == pathlib.Path("/opt/bare/venv")
    assert paths.logs == pathlib.Path("/var/log/bare")


def test_instrument_toml_sets_the_paths(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "sited.toml"
    config.write_text('[instrument]\nname = "sited"\n\n'
                      '[paths]\netc = "/srv/sited/etc"\nvenv = "/srv/sited/venv"\n'
                      'repo = "/srv/sited/app"\nlogs = "/srv/sited/log"\n',
                      encoding="utf-8")
    paths = Paths.from_env(Product.load(config), environ={})
    assert paths.etc == pathlib.Path("/srv/sited/etc")
    assert paths.repo == pathlib.Path("/srv/sited/app")
    assert paths.logs == pathlib.Path("/srv/sited/log")
    # Derived from etc, so the instance files follow it
    assert paths.config("red_adc") == pathlib.Path("/srv/sited/etc/red_adc.yaml")


def test_the_environment_beats_instrument_toml(tmp_path: pathlib.Path) -> None:
    config = tmp_path / "sited.toml"
    config.write_text('[instrument]\nname = "sited"\n\n[paths]\netc = "/srv/sited/etc"\n',
                      encoding="utf-8")
    paths = Paths.from_env(Product.load(config), environ={"SITED_ETC_DIR": "/tmp/etc"})
    assert paths.etc == pathlib.Path("/tmp/etc")


def test_the_example_file_loads(tmp_path: pathlib.Path) -> None:
    """So it cannot drift from the schema it documents."""
    example = pathlib.Path(__file__).resolve().parents[1] / "instrument.toml.example"
    product = Product.load(example)
    assert product.name == "demo"
    # Every other key is commented out, so each falls back to its default
    assert (product.display_name, product.ops_group) == ("demo", "demo-ops")
    assert Paths.from_env(product, environ={}).etc == pathlib.Path("/etc/demo")
