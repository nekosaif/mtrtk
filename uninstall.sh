#!/usr/bin/env bash
# Undo install.sh's system changes: stop and disable mtrtk.service, remove the unit and the udev
# rule. Kept: data/ (raw logs, database), .env, .venv, dialout membership, and RTKLIB in
# /usr/local/bin (remove convbin and rnx2rtkp there by hand if install.sh built them).
#
# usage: ./uninstall.sh [--dry-run]
set -euo pipefail

UNIT="mtrtk.service"
UNIT_PATH="/etc/systemd/system/$UNIT"
UDEV_PATH="/etc/udev/rules.d/99-mtrtk-ublox.rules"

DRY_RUN=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  -h | --help)
    sed -n '2,/^set -euo pipefail/{/^set -euo/d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  *)
    echo "error: unknown option: $1 (see --help)" >&2
    exit 1
    ;;
esac

as_root() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ sudo %s\n' "$*"
  elif [ "$(id -u)" = 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

if [ -d /run/systemd/system ]; then
  # Not installed is not an error: an uninstall must be safe to repeat.
  as_root systemctl disable --now "$UNIT" 2>/dev/null || true
fi
as_root rm -f "$UNIT_PATH" "$UDEV_PATH"
if [ -d /run/systemd/system ]; then
  as_root systemctl daemon-reload
  as_root systemctl reset-failed "$UNIT" 2>/dev/null || true
fi
if command -v udevadm >/dev/null 2>&1 && [ -d /run/udev ]; then
  as_root udevadm control --reload-rules || true
fi
echo "mtrtk service and udev rule removed; data/, .env and .venv kept."
