#!/usr/bin/env bash
# Front door for the @name@ CLI, installed to /usr/local/bin.
#
# The CLI itself lives in the venv, installed editable from the repo, so a
# `git pull` updates it with no reinstall. This wrapper only exists so that a
# missing or broken venv produces an instruction rather than "command not
# found", since `@name@ doctor` is exactly what people run when things break.

VENV_DIR=${@env_prefix@_VENV_DIR:-@venv@}
REPO_DIR=${@env_prefix@_REPO_DIR:-@repo@}

if [[ ! -x $VENV_DIR/bin/@name@ ]]; then
    echo "@name@: $VENV_DIR/bin/@name@ is missing; the venv needs (re)installing." >&2
    echo "  admin: sudo $REPO_DIR/systemd/install.sh" >&2
    exit 1
fi
exec "$VENV_DIR/bin/@name@" "$@"
