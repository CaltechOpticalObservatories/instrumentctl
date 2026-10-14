"""A throwaway host tree, so the tests exercise real directory reads."""
from __future__ import annotations

import pathlib
import textwrap

import pytest

from instrumentctl.instances import Paths
from instrumentctl.product import Product

INSTRUMENT_TOML = """\
[instrument]
name = "demo"
display_name = "Demo"
user = "demouser"
ops_group = "demo-ops"
env_prefix = "DEMO"
docs_url = "https://example.invalid/demo"

[secrets]
key_suffix = "_env"

[legacy]
unit = "demo-daemon@.service"
polkit_rule = "49-demo-daemons.rules"
"""

# name -> (host it belongs to, config body)
INSTANCES = {
    "red_adc": ("thishost", "hardware:\n  password_env: DEMO_RED_PASSWORD\n"),
    "red_fw": ("thishost", "hardware:\n  port: 9000\n"),
    "blue_fw": ("thishost", "nested:\n  deep:\n    token_env: DEMO_TOKEN\n"),
    "elsewhere_arm": ("otherhost", "hardware:\n  port: 1\n"),
}


@pytest.fixture(name="product")
def product_fixture(tmp_path: pathlib.Path) -> Product:
    config = tmp_path / "instrument.toml"
    config.write_text(INSTRUMENT_TOML, encoding="utf-8")
    return Product.load(config)


@pytest.fixture(name="paths")
def paths_fixture(tmp_path: pathlib.Path, product: Product) -> Paths:
    """A repo with four instances, two of them deployed."""
    repo, etc = tmp_path / "repo", tmp_path / "etc"
    (repo / "systemd" / "instances").mkdir(parents=True)
    (repo / "config" / "arm").mkdir(parents=True)
    (repo / "daemons" / "arm").mkdir(parents=True)
    (repo / "daemons" / "arm" / "runner.py").touch()
    (etc / "instances").mkdir(parents=True)
    (etc / "host").write_text("thishost\n", encoding="utf-8")

    for name, (host, body) in INSTANCES.items():
        (repo / "systemd" / "instances" / f"{name}.env").write_text(textwrap.dedent(f"""\
            DEMO_DAEMON=arm/runner.py
            DEMO_HOST={host}
            """), encoding="utf-8")
        (repo / "config" / "arm" / f"{name}.yaml").write_text(body, encoding="utf-8")

    for name in ("red_adc", "blue_fw"):
        (etc / "instances" / f"{name}.env").write_text(
            (repo / "systemd" / "instances" / f"{name}.env").read_text(), encoding="utf-8")
        (etc / f"{name}.yaml").write_text(
            (repo / "config" / "arm" / f"{name}.yaml").read_text(), encoding="utf-8")

    return Paths.from_env(product, environ={
        "DEMO_REPO_DIR": str(repo),
        "DEMO_ETC_DIR": str(etc),
        "DEMO_VENV_DIR": str(tmp_path / "venv"),
    })
