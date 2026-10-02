#!/usr/bin/env bash
# /usr/local/mtrtk/bin/mtrtk in the image (docker/Dockerfile), first on PATH: runs the real
# /app/.venv/bin/mtrtk as the daemon's user.
#
# `docker exec` and `docker compose exec` skip docker/entrypoint.sh and start as root, with the
# compose service's DAC_OVERRIDE. As root, `mtrtk doctor` would call a serial port the daemon
# cannot open "read/write ok" (os.access always passes), and `mtrtk export --out /data/...`
# or `mtrtk sites add` would leave root-owned files the daemon cannot rewrite. So a root caller
# drops to uid/gid 1000 with the same groups as the entrypoint gives the daemon, the healthcheck
# included. MTRTK_RUN_AS_ROOT=1 (the entrypoint's escape hatch) keeps root, as the daemon does.
set -euo pipefail

real="${MTRTK_REAL_BIN:-/app/.venv/bin/mtrtk}"  # overridable only so the tests can stub it

if [ "$(id -u)" != "0" ] || [ "${MTRTK_RUN_AS_ROOT:-0}" = "1" ]; then
  exec "$real" "$@"
fi

# The same list as docker/entrypoint.sh: mtrtk's own groups plus the container's (compose's
# group_add), never root's group 0.
# shellcheck disable=SC2046  # word splitting of the two id lists is the point
groups="$(printf '%s\n' $(id -G mtrtk) $(id -G) | grep -vx 0 | sort -un | paste -sd, -)"

export HOME=/home/mtrtk USER=mtrtk LOGNAME=mtrtk
exec setpriv --reuid=1000 --regid=1000 --groups="$groups" \
  --inh-caps=-all --no-new-privs -- "$real" "$@"
