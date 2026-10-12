#!/usr/bin/env bash
# Enable (or disable) @name@@<instance> units at boot.
#
# `systemctl enable` needs the org.freedesktop.systemd1.manage-unit-files
# polkit action, which polkit cannot scope to a single unit by name. Granting
# it to @ops_group@ would grant enable/disable on every unit on the host. This
# helper is the narrow alternative: it runs as root via a sudoers drop-in that
# is NOPASSWD for @ops_group@ (see install.sh), and it only ever touches
# @name@@<name> for a name that already has an instance file deployed.
#
# Operators normally reach it through `@name@ enable` / `@name@ deploy`, which
# call it as `sudo -n @name@-enable ...`. It stays a standalone root-owned
# script rather than part of the CLI because the CLI runs from the venv, which
# the @name@ user can write to, and root must not run code from there.
#
#   @name@-enable hsfei_adc hsfei_ms      # enable at boot (does not start)
#   @name@-enable --now hsfei_adc         # enable and start
#   @name@-enable --disable hsfei_adc     # disable at boot (does not stop)

set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

INSTANCE_DIR=@etc@/instances

verb=enable
now=()

usage() {
    awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --disable) verb=disable; shift ;;
        --now)     now=(--now); shift ;;
        -h|--help) usage 0 ;;
        --*)       echo "@name@-enable: unknown option $1" >&2; usage 1 ;;
        *)         break ;;
    esac
done

[[ $# -gt 0 ]] || usage 1

# Validate before doing anything, so a typo in the third name does not leave
# the first two half-applied.
for name in "$@"; do
    if [[ ! $name =~ ^[a-z][a-z0-9_]*$ ]]; then
        echo "@name@-enable: refusing '$name': not a valid instance name" >&2
        exit 1
    fi
    if [[ $verb == enable && ! -f "$INSTANCE_DIR/$name.env" ]]; then
        echo "@name@-enable: no $INSTANCE_DIR/$name.env; deploy the instance file first" >&2
        exit 1
    fi
done

for name in "$@"; do
    systemctl "$verb" ${now[@]+"${now[@]}"} "@name@@$name.service"
done
