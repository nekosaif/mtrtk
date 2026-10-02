#!/usr/bin/env bash
# mtrtk native install for Debian / Ubuntu / Raspberry Pi OS (64-bit) with systemd: the
# alternative to `docker compose up -d`. Run it from the clone, as the user who will operate
# mtrtk (not as root, not with sudo): sudo is used for the steps that need it. Idempotent -
# re-run it after a `git pull` to update the environment, the web UI, the unit and the rule.
#
# It installs uv if missing, creates .venv (`uv sync --frozen --no-dev`), builds RTKLIB demo5
# (convbin, rnx2rtkp) from source when they are missing (Debian's `rtklib` package if that
# fails), builds the web UI when Node.js >= 20 is present, creates .env from .env.example when
# there is none (DATA_DIR=<clone>/data and a random NTRIP_PASSWORD) - or points an existing
# .env's container DATA_DIR=/data at <clone>/data, keeping a copy - installs the udev rule
# that keeps ModemManager off u-blox receivers, adds you to `dialout`, installs and starts
# mtrtk.service, and prints `mtrtk doctor`. uninstall.sh reverses the system parts.
#
# usage: ./install.sh [--dry-run] [--no-start] [--no-web] [--rtklib source|apt|skip]
#   --dry-run   print every command that would change something, run none of them
#   --no-start  install and enable the service but do not (re)start it
#   --no-web    do not build the web UI (the API and NTRIP caster still run)
#   --rtklib    how to get convbin/rnx2rtkp when missing (default: source)
# Print only, change nothing (for review and the tests):
#   --render-unit  the systemd unit as it would be installed
#   --print-env    the .env a fresh install would create
set -euo pipefail

RTKLIB_TAG="v2.5.1" # keep in step with RTKLIB_TAG in docker/Dockerfile
RTKLIB_REPO="https://github.com/rtklibexplorer/RTKLIB.git"
UNIT="mtrtk.service"
UNIT_PATH="/etc/systemd/system/$UNIT"
UDEV_RULE="99-mtrtk-ublox.rules"
UDEV_PATH="/etc/udev/rules.d/$UDEV_RULE"
NODE_MIN=20

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

DRY_RUN=0
START=1
BUILD_WEB=1
RTKLIB_MODE="source"
PRINT_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-start) START=0 ;;
    --no-web) BUILD_WEB=0 ;;
    --rtklib)
      [ $# -ge 2 ] || die "--rtklib takes source, apt or skip"
      RTKLIB_MODE="$2"
      shift
      ;;
    --rtklib=*) RTKLIB_MODE="${1#*=}" ;;
    --render-unit) PRINT_ONLY="unit" ;;
    --print-env) PRINT_ONLY="env" ;;
    -h | --help)
      sed -n '2,/^set -euo pipefail/{/^set -euo/d;s/^# \{0,1\}//;p}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done
case "$RTKLIB_MODE" in
  source | apt | skip) ;;
  *) die "--rtklib takes source, apt or skip (got '${RTKLIB_MODE}')" ;;
esac

# Run a command, or under --dry-run only show it.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ %s\n' "$*"
  else
    "$@"
  fi
}
as_root() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ sudo %s\n' "$*"
  else
    sudo "$@"
  fi
}

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
USER_NAME="$(id -un)"
HOME_DIR="${HOME:-$(getent passwd "$USER_NAME" | cut -d: -f6)}"

# The unit as installed: the template with this clone and this user filled in.
render_unit() {
  sed -e "s#__REPO__#$REPO#g" -e "s#__USER__#$USER_NAME#g" "$REPO/systemd/$UNIT"
}
gen_password() {
  local pw
  pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 20 || true)"
  [ ${#pw} -eq 20 ] || return 1
  printf '%s' "$pw"
}
# A fresh .env: .env.example with this clone's DATA_DIR and the NTRIP password $1 (its trailing
# comment kept). Plain BRE, no GNU-only `\b`.
new_env() {
  sed -e "s#^DATA_DIR=.*#DATA_DIR=$REPO/data#" \
    -e "s#^NTRIP_PASSWORD=change-me\$#NTRIP_PASSWORD=$1#" \
    -e "s#^NTRIP_PASSWORD=change-me\([[:space:]]\)#NTRIP_PASSWORD=$1\1#" \
    "$REPO/.env.example"
  grep -q '^DATA_DIR=' "$REPO/.env.example" || printf 'DATA_DIR=%s/data\n' "$REPO"
}
# The DATA_DIR the .env $1 sets (last one wins, `export`, quotes and a trailing comment allowed);
# empty when it sets none, which leaves mtrtk's default, /data.
env_data_dir() {
  local line
  line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?DATA_DIR[[:space:]]*=' "$1" | tail -n 1 || true)"
  [ -n "$line" ] || return 0
  printf '%s' "${line#*=}" | sed -e 's/[[:space:]]#.*$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    -e "s/^[\"']//" -e "s/[\"']\$//"
}

# ---------------------------------------------------------------- preflight
if [ "$(id -u)" = 0 ]; then
  # The service, .venv and .env would all belong to root, and the receiver port would be opened
  # by a root daemon. `sudo ./install.sh` is the usual way into this, so say what to do instead.
  die "run install.sh as the user who will operate mtrtk, not as root (it uses sudo itself)"
fi
[ -f "$REPO/pyproject.toml" ] && [ -f "$REPO/.env.example" ] && [ -f "$REPO/systemd/$UNIT" ] ||
  die "$REPO does not look like an mtrtk clone (pyproject.toml, .env.example, systemd/$UNIT)"
case "$REPO" in
  # The path goes into a systemd unit (where `%` is a specifier and spaces split ExecStart) and
  # through sed; refusing these few characters is simpler than escaping for both.
  *[[:space:]%\\\"\'\#\&\|\$]*) die "the clone's path '$REPO' has a space or one of %\\\"'#&|\$; move it" ;;
esac
[[ "$USER_NAME" =~ ^[a-z_][a-z0-9_.-]*$ ]] || die "user name '$USER_NAME' is not one systemd accepts"
case "$PRINT_ONLY" in
  unit)
    render_unit
    exit 0
    ;;
  env)
    pw="$(gen_password)" || die "could not generate a password from /dev/urandom"
    new_env "$pw"
    exit 0
    ;;
esac
if [ "$DRY_RUN" = 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "sudo is needed (apt-get install sudo, then add $USER_NAME to the sudo group)"
fi
HAVE_SYSTEMD=0
[ -d /run/systemd/system ] && HAVE_SYSTEMD=1
HAVE_APT=0
command -v apt-get >/dev/null 2>&1 && HAVE_APT=1

say "mtrtk native install for $USER_NAME in $REPO$([ "$DRY_RUN" = 1 ] && echo ' (dry run: nothing is changed)')"
if [ "$DRY_RUN" = 0 ]; then
  sudo -v || die "sudo did not authenticate"
fi

APT_UPDATED=0
apt_install() {
  # Only the packages that are not installed yet, so a re-run does not touch apt at all.
  local missing=() pkg status
  for pkg in "$@"; do
    status="$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)"
    [ "$status" = "install ok installed" ] || missing+=("$pkg")
  done
  [ ${#missing[@]} -eq 0 ] && return 0
  if [ "$HAVE_APT" = 0 ]; then
    warn "no apt-get: install ${missing[*]} with your package manager"
    return 1
  fi
  if [ "$APT_UPDATED" = 0 ]; then
    as_root apt-get update -qq || return 1
    APT_UPDATED=1
  fi
  as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
}

# ---------------------------------------------------------------- uv + python environment
if command -v dpkg-query >/dev/null 2>&1; then
  apt_install ca-certificates curl || die "curl and ca-certificates are needed"
elif ! command -v curl >/dev/null 2>&1; then
  # Not a Debian-family host: what is on PATH is all there is to go by.
  die "curl is needed: install it (and CA certificates) with your package manager"
fi
export PATH="$HOME_DIR/.local/bin:$HOME_DIR/.cargo/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "installing uv (https://astral.sh/uv)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ curl -LsSf https://astral.sh/uv/install.sh | sh\n'
  else
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH ($HOME_DIR/.local/bin)"
  fi
fi
say "python environment (.venv)"
run uv --directory "$REPO" sync --frozen --no-dev

# ---------------------------------------------------------------- RTKLIB
build_rtklib() {
  # Every step is checked by hand: errexit does not apply inside a function called from `if`.
  local app="app/consapp" src log
  apt_install build-essential gfortran git || return 1
  say "building RTKLIB demo5 $RTKLIB_TAG (a few minutes on a Raspberry Pi)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ git clone --depth 1 --branch %s %s; make convbin rnx2rtkp\n' "$RTKLIB_TAG" "$RTKLIB_REPO"
    printf '+ sudo install -m 0755 convbin rnx2rtkp /usr/local/bin/\n'
    return 0
  fi
  src="$(mktemp -d)" || return 1
  log="$src/build.log"
  # The compiler's warnings go to the log, which is shown only when the build fails.
  if git clone -q --depth 1 --branch "$RTKLIB_TAG" "$RTKLIB_REPO" "$src/rtklib" >"$log" 2>&1 &&
    make -C "$src/rtklib/$app/convbin/gcc" -j"$(nproc)" >>"$log" 2>&1 &&
    make -C "$src/rtklib/$app/rnx2rtkp/gcc" -j"$(nproc)" >>"$log" 2>&1 &&
    sudo install -m 0755 "$src/rtklib/$app/convbin/gcc/convbin" \
      "$src/rtklib/$app/rnx2rtkp/gcc/rnx2rtkp" /usr/local/bin/; then
    rm -rf "$src"
    return 0
  fi
  tail -n 30 "$log" >&2 || true
  rm -rf "$src"
  return 1
}
if command -v convbin >/dev/null 2>&1 && command -v rnx2rtkp >/dev/null 2>&1; then
  say "RTKLIB: convbin and rnx2rtkp found ($(command -v convbin))"
else
  case "$RTKLIB_MODE" in
    source)
      if ! build_rtklib; then
        warn "the RTKLIB demo5 build failed; falling back to Debian's rtklib package (stock 2.4.3)"
        apt_install rtklib || warn "RTKLIB is not installed: RINEX export and PPK will not work"
      fi
      ;;
    apt) apt_install rtklib || warn "RTKLIB is not installed: RINEX export and PPK will not work" ;;
    skip) warn "--rtklib skip: without convbin/rnx2rtkp RINEX export and PPK will not work" ;;
  esac
fi

# ---------------------------------------------------------------- web UI
STATIC="$REPO/src/mtrtk/web/static"
node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null)" || return 1
  [ "${major:-0}" -ge "$NODE_MIN" ]
}
pnpm_cmd() {
  # The lockfile and pnpm-workspace.yaml are pnpm 11's; an older pnpm would misread them.
  local major
  if command -v pnpm >/dev/null 2>&1; then
    major="$(pnpm --version 2>/dev/null | cut -d. -f1)"
    if [ "${major:-0}" -ge 11 ]; then
      echo pnpm
      return 0
    fi
  fi
  if command -v corepack >/dev/null 2>&1; then
    echo "corepack pnpm@11"
  elif command -v npx >/dev/null 2>&1; then
    echo "npx --yes pnpm@11"
  else
    return 1
  fi
}
build_web() {
  local pnpm
  pnpm="$(pnpm_cmd)" || return 1
  say "building the web UI ($pnpm)"
  # shellcheck disable=SC2086 # $pnpm is a command plus arguments
  run env COREPACK_ENABLE_DOWNLOAD_PROMPT=0 $pnpm --dir "$REPO/web" install --frozen-lockfile &&
    run env COREPACK_ENABLE_DOWNLOAD_PROMPT=0 $pnpm --dir "$REPO/web" run build &&
    run rm -rf "$STATIC" &&
    run cp -r "$REPO/web/dist" "$STATIC"
}
if [ "$BUILD_WEB" = 0 ]; then
  say "web UI: skipped (--no-web)"
elif node_ok; then
  build_web || warn "the web UI build failed; the API and NTRIP caster still run"
elif [ -f "$STATIC/index.html" ]; then
  warn "Node.js >= $NODE_MIN not found: keeping the web UI already built (it may be older than this code)"
else
  warn "Node.js >= $NODE_MIN not found, so there is no web UI (the API and NTRIP caster still run)."
  warn "  install Node.js $NODE_MIN+ (https://nodejs.org) and re-run ./install.sh"
fi

# ---------------------------------------------------------------- .env + data directory
ENV_FILE="$REPO/.env"
if [ -f "$ENV_FILE" ]; then
  data_value="$(env_data_dir "$ENV_FILE")"
  if [ -z "$data_value" ] || { [ "$data_value" = "/data" ] && [ ! -w /data ]; }; then
    # .env.example's DATA_DIR=/data, from the quick start's `cp .env.example .env` or a move from
    # Docker: the service runs as $USER_NAME, who cannot create or write /data, so it would never
    # start. The container kept its data in ./data, which is exactly this path.
    say "DATA_DIR in .env is '${data_value:-unset, so /data}', the container's path; setting it to $REPO/data"
    if [ "$DRY_RUN" = 1 ]; then
      printf '+ set DATA_DIR=%s/data in .env (the old file kept as .env.bak-<UTC>)\n' "$REPO"
    else
      env_backup="$ENV_FILE.bak-$(date -u +%Y%m%dT%H%M%SZ)"
      cp -p "$ENV_FILE" "$env_backup"
      chmod 600 "$env_backup"  # every secret in clear; .gitignore keeps it out of a commit
      # mtrtk's own writer: the same one the web UI uses, keeping every other line, quoting and
      # comment as they are.
      "$REPO/.venv/bin/python" - "$ENV_FILE" "$REPO/data" <<'PY' ||
import sys
from pathlib import Path

from mtrtk.web.envfile import update_env

update_env(Path(sys.argv[1]), {"DATA_DIR": sys.argv[2]})
PY
        die "could not update $ENV_FILE: set DATA_DIR=$REPO/data in it by hand and re-run"
      say "the previous .env is kept as $env_backup"
    fi
  else
    say ".env exists; left as it is"
  fi
  # The quick start's `cp .env.example .env` leaves it group- and world-readable (umask 002).
  run chmod 600 "$ENV_FILE"
else
  say "creating .env from .env.example"
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ cp .env.example .env (DATA_DIR=%s/data, random NTRIP_PASSWORD, mode 0600)\n' "$REPO"
  else
    ntrip_password="$(gen_password)" || die "could not generate a password from /dev/urandom"
    umask_before="$(umask)"
    umask 077
    new_env "$ntrip_password" >"$ENV_FILE"
    umask "$umask_before"
    say "NTRIP password for rovers (user 'rover'): $ntrip_password  - it is NTRIP_PASSWORD in .env"
  fi
fi
run mkdir -p "$REPO/data"

# ---------------------------------------------------------------- udev rule + dialout
say "udev rule ($UDEV_PATH) and the dialout group"
as_root install -D -m 0644 "$REPO/udev/$UDEV_RULE" "$UDEV_PATH"
if command -v udevadm >/dev/null 2>&1 && [ -d /run/udev ]; then
  as_root udevadm control --reload-rules || warn "udevadm could not reload the rules"
  as_root udevadm trigger --action=change --subsystem-match=tty || true
  as_root udevadm trigger --action=change --subsystem-match=usb --attr-match=idVendor=1546 || true
else
  warn "udev is not running here; the rule applies from the next boot"
fi
JOINED_DIALOUT=0
if [[ " $(id -nG "$USER_NAME") " == *" dialout "* ]]; then
  say "$USER_NAME is already in dialout"
else
  as_root usermod -aG dialout "$USER_NAME"
  JOINED_DIALOUT=1
fi

# ---------------------------------------------------------------- systemd unit
say "systemd unit ($UNIT_PATH)"
if [ "$DRY_RUN" = 1 ]; then
  printf '+ render systemd/%s with __REPO__=%s __USER__=%s into %s\n' "$UNIT" "$REPO" "$USER_NAME" "$UNIT_PATH"
else
  rendered="$(mktemp)"
  render_unit >"$rendered"
  sudo install -D -m 0644 "$rendered" "$UNIT_PATH"
  rm -f "$rendered"
fi
SERVICE_OK=1
if [ "$HAVE_SYSTEMD" = 1 ]; then
  as_root systemctl daemon-reload
  as_root systemctl enable "$UNIT"
  if [ "$START" = 1 ]; then
    # restart, not `enable --now`: after a `git pull` the running daemon is the old code.
    as_root systemctl restart "$UNIT"
    if [ "$DRY_RUN" = 0 ]; then
      sleep 3
      if ! systemctl is-active --quiet "$UNIT"; then
        SERVICE_OK=0
        warn "$UNIT did not stay up; its last log lines:"
        sudo journalctl -u "$UNIT" -n 20 --no-pager >&2 || true
      fi
    fi
  fi
else
  warn "systemd is not running here (a container, or WSL without systemd): the unit is installed"
  warn "  but not enabled; run the daemon by hand with: cd $REPO && .venv/bin/mtrtk run"
fi

# ---------------------------------------------------------------- doctor
say "mtrtk doctor"
if [ "$DRY_RUN" = 1 ]; then
  printf '+ %s/.venv/bin/mtrtk doctor\n' "$REPO"
else
  (cd "$REPO" && MTRTK_ENV_FILE="$ENV_FILE" "$REPO/.venv/bin/mtrtk" doctor) || true
  if [ "$JOINED_DIALOUT" = 1 ]; then
    echo "   (this shell is not in dialout until you log in again, so doctor may report the"
    echo "    receiver as 'no permission'; the service already has the group)"
  fi
fi

TS_IP="$(tailscale ip -4 2>/dev/null | head -n 1 || true)"
echo
echo "Done. Logs: journalctl -u mtrtk -f   UI: http://${TS_IP:-<tailscale-ip>}:8080"
echo "Settings: $ENV_FILE (or the web UI's Settings page). Backups: .venv/bin/mtrtk backup --out FILE"
[ "$SERVICE_OK" = 1 ] || exit 1
