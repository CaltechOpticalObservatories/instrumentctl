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
| `git.remote` | `origin` | where `hotfix` pushes and `update` fetches from |
| `git.base` | `main` | the branch hosts run, and hotfixes are reviewed into |
| `git.branch_prefix` | `hotfix/` | marks a branch `hotfix` made |
| `git.email_domain` | the hostname | operators commit as `<user>@<this>` |
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

## Hotfixes

An operator who has to change the code at night should not need to know the
GitHub process. They edit the checkout, restart what uses it, and run:

```bash
<name> hotfix "ADC hangs after power cycle"
```

which shows what changed, asks, commits it in their name to a branch such as
`hotfix/2026-10-15-<host>-adc-hangs-after-power-cycle`, pushes it, and opens a
pull request against `main`. The host keeps running the fix. Once a developer
merges it, `<name> update` returns the host to `main`; until then it refuses,
so a fix is never dropped by accident. On `main`, `update` just pulls.

- `<name> hotfix`, with no description, lists unsaved edits.
- `<name> hotfix --undo` puts edited files back as committed.
- `<name> hotfix --push` retries a push or pull request that failed.
- `<name> update --abandon` leaves an unmerged hotfix a developer rejected;
  the branch stays, here and on GitHub.
- `doctor` warns when a host is on a hotfix branch, has commits GitHub does
  not, or has edits nobody saved.

Pushing uses one token, so operators need no GitHub account. Make a
fine-grained personal access token limited to the instrument's repository, with
Contents and Pull requests read and write, and put it alone in
`/etc/<name>/github-token`. `install` makes that file root-owned and readable
by the ops group, which means every operator, and the daemon account, can read
it: scope it to the one repository, and protect `main` so that it needs a
reviewed pull request. Fine-grained tokens expire, at most a year out, and
`doctor` cannot tell an expired one from a good one, so put the date in a
calendar.

## Tests

```bash
uv run --extra test pytest
```

They build a throwaway host tree and exercise real directory reads, stubbing
only systemd. The rendering tests assert that no `@placeholder@` survives,
which is what catches a template gaining a variable nobody wired up.

## Host setup

Every instrument installs the same way. The instrument's own docs add only its
apt packages and host roles.

```bash
sudo apt install acl git curl
sudo git clone --recurse-submodules <repo-url> /opt/<name>/app
sudo /opt/<name>/app/systemd/install.sh <dev-user> <operator>... [--host-role <role>] [--extra <name>]
# log out and back in, for the new groups and PATH
$EDITOR /etc/<name>/secrets.env    # the credentials doctor asks for
<name> doctor
```

Each instrument keeps a short `install.sh`, since building the virtualenv has
to happen before this code exists on the host. It bootstraps a pinned uv,
refreshes the lock, syncs, and hands over:

```bash
"$UV_BIN" lock --upgrade
"$UV_BIN" sync --all-packages "${EXTRA_FLAGS[@]}"
exec "$VENV_DIR/bin/<name>" install "${INSTALL_ARGS[@]}"
```

A host that needs an optional dependency, such as a camera module only one
host can build, passes `--extra <name>`. `install.sh` records it in
`/etc/<name>/extras` once the sync succeeds, and every later run syncs with the
recorded extras, so a plain rerun keeps them. `doctor` reports a recorded extra
whose packages are gone from the venv, which is what a plain `uv sync` by hand
leaves behind.

`<name> install` then converges the host, reporting only what it changes:

- creates the ops group and the daemon account, and enrols each user named
  (the dev user included) into the ops group and `systemd-journal`;
- makes `/etc/<name>`, `/etc/<name>/instances` and `/var/log/<name>`, seeds
  `secrets.env` and the host role file;
- renders the template unit, the polkit rule, the sudoers drop-in, the two
  helper scripts and `/etc/profile.d/<name>.sh`, which puts the venv's `bin`
  on every login shell's `PATH`, so `python` and the venv's tools need no
  `activate`;
- gives `/opt/<name>` and `/etc/<name>` to the daemon account and the ops
  group: group-writable, setgid, with a default ACL so files anyone adds later
  stay group-writable whatever their umask. git is told to trust the checkout
  and to keep what it writes group-writable.

Operators can therefore pull, hotfix, sync the venv and edit configs without
sudo. That grants nothing new: they already decide what the daemons run,
through the configs they edit and the restarts they trigger. The exceptions:

- `secrets.env` stays `root:<ops> 660`, editable without sudo but kept out of
  the recursive hand-over;
- the GitHub token is root-owned and only readable by the ops group;
- `/var/log/<name>` is written only by the daemons.

Re-run `install.sh` to enrol another operator, after a dependency change, or
to repair a host. Adding a daemon needs none of this: `<name> deploy <instance>`.
`doctor` compares every rendered file against what `install` would write now,
and checks that both trees are still shared with the ops group and that the
recorded extras are installed.
