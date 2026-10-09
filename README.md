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

Everything except `name` is optional.

| key | default | what it does |
| --- | --- | --- |
| `name` | required | the command, the unit (`<name>@.service`), and `/opt/<name>/app`, `/opt/<name>/venv`, `/etc/<name>` |
| `user` | `name` | the account the daemons run as |
| `ops_group` | `<name>-ops` | the group that may start, stop and deploy without a password |
| `env_prefix` | `NAME` | instance files set `<PREFIX>_DAEMON`, `_CONFIG` and `_HOST`; operators override paths with `<PREFIX>_REPO_DIR`, `_VENV_DIR`, `_ETC_DIR` |
| `subsystem_prefix` | none | set it only where instance names share one, so `start fei` finds `hsfei_*` |
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

Host setup, meaning the user, the group, the unit file, the polkit rule and the
venv, is still an `install.sh` in each instrument. Nothing here needs root.
