"""Which instrument this CLI drives.

Everything that differs between instruments is here, so the rest of the package
is about deploying daemons. Each instrument ships an ``instrument.toml`` and
passes it to :func:`instrumentctl.run`.

Identity lives in ``[instrument]``. A deployment that does not use the
``/etc/<name>``, ``/opt/<name>`` and ``/var/log/<name>`` defaults records that
in ``[paths]``, so the choice is in the instrument's repository rather than in
each operator's environment. The matching ``<PREFIX>_*_DIR`` variables still
win over both, for a one-off run against a tree somewhere else.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Optional, Union

# A path to an instrument.toml, or an already-open binary handle to one
ConfigSource = Union[str, Path, IO[bytes], Any]

SYSTEMD_DIR = Path("/etc/systemd/system")
POLKIT_RULES_DIR = Path("/etc/polkit-1/rules.d")
SUDOERS_DIR = Path("/etc/sudoers.d")
SBIN_DIR = Path("/usr/local/sbin")
PROFILE_DIR = Path("/etc/profile.d")


# Each attribute is one fact about the instrument rather than hidden state
@dataclass(frozen=True)
class Product:  # pylint: disable=too-many-instance-attributes
    """One instrument's names, and the paths and units derived from them."""

    name: str
    # The instrument's proper name, as it reads in systemctl status
    display_name: str
    user: str
    ops_group: str
    env_prefix: str
    secret_key_suffix: str = "_env"
    docs_url: str = ""
    # Used only in --help, where a real name beats a placeholder
    example_instance: str = "<instance>"
    example_subsystem: str = "<subsystem>"
    # Units and polkit rules this product used to install, reported by doctor
    # so a host that predates a rename can be recognised
    legacy_unit: Optional[str] = field(default=None)
    legacy_polkit_rule: Optional[str] = field(default=None)
    # Deployment layout, each falling back to a name-derived default
    etc_dir: Optional[str] = field(default=None)
    venv_dir: Optional[str] = field(default=None)
    repo_dir: Optional[str] = field(default=None)
    logs_dir: Optional[str] = field(default=None)
    # Where hotfixes go: the remote and branch the host follows, the prefix
    # that marks a hotfix branch, and the domain of the operator's commit email
    git_remote: str = "origin"
    git_base: str = "main"
    git_branch_prefix: str = "hotfix/"
    git_email_domain: str = ""
    # Where the dashboard listens. Loopback unless the instrument opens it up,
    # since anyone who can reach it can start and stop the daemons
    dashboard_bind: str = "127.0.0.1"
    dashboard_port: int = 8750

    @classmethod
    def load(cls, config: "ConfigSource") -> "Product":
        """Build from an ``instrument.toml``, given a path or an open file."""
        if hasattr(config, "read"):
            data = tomllib.load(config)
        else:
            with open(config, "rb") as handle:  # type: ignore[arg-type]
                data = tomllib.load(handle)
        instrument = data["instrument"]
        secrets = data.get("secrets", {})
        legacy = data.get("legacy", {})
        help_text = data.get("help", {})
        paths = data.get("paths", {})
        git = data.get("git", {})
        dashboard = data.get("dashboard", {})
        return cls(
            name=instrument["name"],
            display_name=instrument.get("display_name",
                                        instrument["name"]),
            user=instrument.get("user", instrument["name"]),
            ops_group=instrument.get("ops_group", f"{instrument['name']}-ops"),
            env_prefix=instrument.get("env_prefix", instrument["name"].upper()),
            secret_key_suffix=secrets.get("key_suffix", "_env"),
            docs_url=instrument.get("docs_url", ""),
            example_instance=help_text.get("example_instance", "<instance>"),
            example_subsystem=help_text.get("example_subsystem", "<subsystem>"),
            legacy_unit=legacy.get("unit"),
            legacy_polkit_rule=legacy.get("polkit_rule"),
            etc_dir=paths.get("etc"),
            venv_dir=paths.get("venv"),
            repo_dir=paths.get("repo"),
            logs_dir=paths.get("logs"),
            git_remote=git.get("remote", "origin"),
            git_base=git.get("base", "main"),
            git_branch_prefix=git.get("branch_prefix", "hotfix/"),
            git_email_domain=git.get("email_domain", ""),
            dashboard_bind=dashboard.get("bind", "127.0.0.1"),
            dashboard_port=int(dashboard.get("port", 8750)),
        )

    def unit(self, instance: str) -> str:
        """The systemd unit for an instance."""
        return f"{self.name}@{instance}.service"

    def instance_of(self, unit_id: str) -> Optional[str]:
        """The instance a unit id names, or None if it is not one of ours."""
        prefix, suffix = f"{self.name}@", ".service"
        if not unit_id.startswith(prefix) or not unit_id.endswith(suffix):
            return None
        return unit_id[len(prefix):-len(suffix)]

    def env_key(self, name: str) -> str:
        """An instance file's variable, e.g. ``HISPEC_DAEMON`` for ``DAEMON``."""
        return f"{self.env_prefix}_{name}"

    @property
    def unit_template(self) -> Path:
        """The installed template unit every instance runs from."""
        return SYSTEMD_DIR / f"{self.name}@.service"

    @property
    def dashboard_unit(self) -> Path:
        """The service that serves the dashboard."""
        return SYSTEMD_DIR / f"{self.name}-dashboard.service"

    @property
    def polkit_rule(self) -> Path:
        """The rule granting the ops group start/stop without a password."""
        return POLKIT_RULES_DIR / f"49-{self.name}.rules"

    @property
    def sudoers(self) -> Path:
        """The drop-in granting passwordless use of the enable helper."""
        return SUDOERS_DIR / f"{self.name}-ops"

    @property
    def profile_script(self) -> Path:
        """Login-shell setup that puts the venv on every operator's PATH."""
        return PROFILE_DIR / f"{self.name}.sh"

    @property
    def enable_helper(self) -> Path:
        """Root helper behind ``enable``, which polkit cannot scope per unit."""
        return SBIN_DIR / f"{self.name}-enable"

    @property
    def legacy_unit_path(self) -> Optional[Path]:
        """Where a pre-rename template unit would still be installed."""
        return SYSTEMD_DIR / self.legacy_unit if self.legacy_unit else None

    @property
    def legacy_polkit_path(self) -> Optional[Path]:
        """Where a pre-rename polkit rule would still be installed."""
        if not self.legacy_polkit_rule:
            return None
        return POLKIT_RULES_DIR / self.legacy_polkit_rule
