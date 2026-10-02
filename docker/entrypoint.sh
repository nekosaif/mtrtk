#!/usr/bin/env bash
# Entrypoint of the mtrtk image (docker/Dockerfile), PID 1 until it execs tini.
#
# Starts as root for two things only: taking ownership of /data when a fresh or older bind mount
# holds files the daemon could not write (a directory Docker created, or one a root-run image
# filled), and handing the daemon the groups that open the serial devices. It then drops to the
# unprivileged `mtrtk` user (uid/gid 1000) for good. MTRTK_RUN_AS_ROOT=1 skips both: the escape
# hatch for device permissions no group can satisfy.
set -euo pipefail

APP_UID=1000
APP_GID=1000
DATA="${MTRTK_DATA_ROOT:-/data}"  # the volume; overridable only so the tests can stub it

# `docker run IMAGE doctor` and `docker compose run --rm mtrtk sites list` keep working: a first
# argument that is an option, or not a program on PATH, is an mtrtk subcommand. `type -P` looks
# only for files: `command -v` would also find bash's builtins, and `export` is one of them.
if [ "$#" -eq 0 ]; then
  set -- mtrtk run
elif [ "${1#-}" != "$1" ] || [ -z "$(type -P -- "$1")" ]; then
  set -- mtrtk "$@"
fi

# tini becomes PID 1 *after* the drop, as the same user as the daemon: a root tini without
# CAP_KILL (compose drops every capability) cannot forward `docker stop`'s SIGTERM to a uid-1000
# child, and dies instead, taking the daemon down without its shutdown. -s keeps it reaping the
# children of jobs (convbin, rnx2rtkp) under `docker run --init` too, where it is not PID 1.
init=()
if command -v tini >/dev/null 2>&1; then
  init=(tini -s --)
fi

if [ "$(id -u)" != "0" ] || [ "${MTRTK_RUN_AS_ROOT:-0}" = "1" ]; then
  exec "${init[@]}" "$@"
fi

# Bash as PID 1 has no default action for SIGTERM: without this trap `docker stop` during a long
# chown would wait out the grace period and end in SIGKILL. The chown runs in the background so
# the trap fires at once (bash runs traps only between commands); exec clears the trap.
child=""
trap '[ -n "$child" ] && kill -TERM "$child" 2>/dev/null; exit 143' TERM INT

# Anything under /data not owned by the daemon's uid: an image that ran as root left its SQLite
# database and logs root-owned, even inside a directory the host user owns. Only those files are
# changed, so a chown that keeps failing does not rewalk and rewrite the whole raw archive on
# every start. find does not follow symlinks and `chown -h` changes the link itself, so a link in
# /data cannot hand the daemon a file outside it.
if [ -d "$DATA" ]; then
  stranger="$(find "$DATA" ! -user "$APP_UID" -print -quit 2>/dev/null || true)"
  if [ -n "$stranger" ]; then
    echo "entrypoint: taking ownership of $DATA for uid $APP_UID (found $stranger)" >&2
    find "$DATA" ! -user "$APP_UID" -exec chown -h "$APP_UID:$APP_GID" {} + &
    child=$!
    # A read-only or root-squashed mount refuses; say so and carry on, so the daemon's own
    # error (and `mtrtk doctor`) names the directory instead of a container that never starts.
    if ! wait "$child"; then
      echo "entrypoint: could not take ownership of all of $DATA; the daemon may fail to write it" >&2
    fi
    child=""
  fi
fi

# Supplementary groups: the image's own for `mtrtk` (dialout, gid 20 as on Debian hosts) plus
# every group the container was started with - compose's `group_add: [DIALOUT_GID]` is how the
# host's serial-device group arrives. gosu and `su` would replace them with /etc/group's list, which
# knows nothing of the host's gid. Group 0 (root) is never kept.
# shellcheck disable=SC2046  # word splitting of the two id lists is the point
groups="$(printf '%s\n' $(id -G mtrtk) $(id -G) | grep -vx 0 | sort -un | paste -sd, -)"

export HOME=/home/mtrtk USER=mtrtk LOGNAME=mtrtk
exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --groups="$groups" \
  --inh-caps=-all --no-new-privs -- "${init[@]}" "$@"
