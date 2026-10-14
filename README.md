# instrumentctl

The operator command for an instrument whose daemons run as systemd template
units: deploy an instance, start and stop it, read its logs, and diagnose a
host that will not cooperate.

Nothing here is specific to one instrument. Each ships an `instrument.toml`
and a console script, and gets the same commands under its own name.

## What an instrument provides

An `instrument.toml`, shipped as package data:

```toml
[instrument]
name = "hispec"
ops_group = "hispec-ops"
env_prefix = "HISPEC"
```

and a console script that passes it:

```python
# src/hispec/cli/__init__.py
from importlib import resources
from pathlib import Path

from instrumentctl import run


def main(argv=None):
    config = resources.files(__package__).joinpath("instrument.toml")
    return run(config, repo_hint=Path(__file__).resolve().parents[3], argv=argv)
```

```toml
[project.scripts]
hispec = "hispec.cli:main"
```

`repo_hint` is where the instrument package lives. Under an editable install
that locates the checkout, which is what makes the deployed CLI follow a
`git pull`; from a wheel it has no instance files and the standard path wins.

## Configuration

Everything except `name` is optional. `instrument.toml.example` lists the
common keys with their defaults, ready to copy.

| key | default | what it does |
| --- | --- | --- |
| `name` | required | the command, the unit (`<name>@.service`), and the default paths below |
| `display_name` | `name` | the instrument's proper name, as it reads in `systemctl status` |
| `user` | `name` | the account the daemons run as |
| `ops_group` | `<name>-ops` | the group that may start, stop and deploy without a password |
| `env_prefix` | `NAME` | instance files set `<PREFIX>_DAEMON` and `_HOST`; a host overrides a path with `<PREFIX>_ETC_DIR`, `_VENV_DIR`, `_REPO_DIR` or `_LOGS_DIR` |
| `paths.etc` | `/etc/<name>` | configs, instance files and `secrets.env` |
| `paths.venv` | `/opt/<name>/venv` | the interpreter the daemons run under |
| `paths.repo` | `/opt/<name>/app` | the checkout holding `daemons/` and `config/` |
| `paths.logs` | `/var/log/<name>` | the daemons' own log files |
| `docs_url` | none | linked from `--help` |
| `secrets.key_suffix` | `_env` | a config key ending in this names the variable holding a credential |
| `help.example_instance` | `<instance>` | used in `--help`, where a real name beats a placeholder |
| `help.example_subsystem` | `<subsystem>` | likewise |
| `legacy.unit` | none | a template unit an older version installed, reported by `doctor` |
| `legacy.polkit_rule` | none | likewise for its polkit rule |

Which credentials a host needs is deliberately not configured. It is derived
from the deployed configs and from the daemon code that reads them, so it
cannot go stale the way a hand-written list does.

## The deployment shape it assumes

An instance is a name with three files behind it: `<repo>/systemd/instances/
<name>.env` naming a daemon script and a config, `<repo>/config/<subsystem>/
<name>.yaml`, and their deployed copies under `/etc/<name>`. Deployed means the
`.env` is in `/etc/<name>/instances/`, which is what the template unit reads.

## Tests

```bash
uv run --extra test pytest
```

They build a throwaway host tree and exercise real directory reads, stubbing
only systemd. The rendering tests assert that no `@placeholder@` survives,
which is what catches a template gaining a variable nobody wired up.

## Host setup

`<name> install`, as root, converges a host: it creates the ops group and the
daemon account, enrols operators, makes `/etc/<name>`, `/etc/<name>/instances`
and `/var/log/<name>` setgid, seeds `secrets.env` and the host role file, and
renders the template unit, the polkit rule, the sudoers drop-in and the two
helper scripts from the same config the rest of the CLI reads. It is safe to
re-run and reports only what it changes.

Building the virtualenv is not its job, since this code lives inside it. Each
instrument keeps a short `install.sh` that bootstraps uv, runs `uv sync`, and
hands over:

```bash
"$UV_BIN" sync --all-packages
exec "$VENV_DIR/bin/<name>" install "$@"
```

Everything else needs no root. `doctor` compares the installed unit and polkit
rule against what `install` would write now, so a host whose unit predates a
template change is reported rather than silently stale.
