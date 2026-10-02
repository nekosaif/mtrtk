#!/usr/bin/env bash
# Checks an exposure of the base station end to end, from outside: the web UI's /healthz, then an
# NTRIP v2 request for the mountpoint, which must deliver RTCM3 frames within CHECK_TIMEOUT_S.
#
# Usage: scripts/check-exposure.sh <web-url> <ntrip-url> [user] [password]
#   scripts/check-exposure.sh https://rtk.example.com https://ntrip.example.com/MTRK rover password
#
# Environment:
#   CHECK_TIMEOUT_S          seconds to wait for RTCM (default 15)
#   CHECK_BYTES              stop reading once this many bytes have arrived (default 2000)
#   CF_ACCESS_CLIENT_ID      a Cloudflare Access service token, sent with the /healthz request
#   CF_ACCESS_CLIENT_SECRET  when an Access policy guards the web hostname
#
# Exit status: 0 both checks passed, 1 a check failed, 2 bad usage or a missing tool.
set -euo pipefail

usage() {
	sed -n '5,6p' "$0" | sed 's/^# //' >&2
	exit 2
}

[ $# -ge 2 ] || usage
WEB="${1%/}"
NTRIP="$2"
NTRIP_USER="${3:-}"
NTRIP_PASS="${4:-}"
TIMEOUT="${CHECK_TIMEOUT_S:-15}"
WANT_BYTES="${CHECK_BYTES:-2000}"

for tool in curl od awk; do
	command -v "$tool" >/dev/null || {
		echo "FAIL: $tool is not installed" >&2
		exit 2
	}
done

now() { # seconds, with a fraction where bash has EPOCHREALTIME (5.0+)
	if [ -n "${EPOCHREALTIME:-}" ]; then echo "${EPOCHREALTIME/,/.}"; else date +%s; fi
}
elapsed() { awk -v a="$1" -v b="$(now)" 'BEGIN { printf "%.1f", b - a }'; }

tmp=$(mktemp -d)
curl_pid=
# shellcheck disable=SC2329 # run by the EXIT trap below
cleanup() {
	if [ -n "$curl_pid" ]; then kill "$curl_pid" 2>/dev/null || true; fi
	rm -rf "$tmp"
}
trap cleanup EXIT

# ---------------------------------------------------------------- web
echo "== web: $WEB/healthz =="
web_args=(-sS --max-time 10 -o "$tmp/healthz" -w '%{http_code}')
if [ -n "${CF_ACCESS_CLIENT_ID:-}" ]; then
	web_args+=(-H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID")
	web_args+=(-H "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET:-}")
fi
if ! code=$(curl "${web_args[@]}" "$WEB/healthz"); then
	echo "FAIL: $WEB is unreachable (DNS, TLS, the tunnel or Caddy)"
	exit 1
fi
body=$(head -c 300 "$tmp/healthz")
if [ "$code" != 200 ]; then
	echo "FAIL: /healthz answered HTTP $code: $body"
	exit 1
fi
case "$body" in
*'"status":"ok"'*) echo "$body" ;;
*)
	echo "FAIL: /healthz answered, but not as mtrtk (a login page in front of it? An Access"
	echo "      policy on the hostname needs CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET): $body"
	exit 1
	;;
esac

# ---------------------------------------------------------------- NTRIP
# HTTP/1.1, as rovers speak it: curl would otherwise negotiate HTTP/2 with Cloudflare's edge.
echo "== NTRIP v2: $NTRIP (up to ${TIMEOUT} s) =="
ntrip_args=(-sS --http1.1 -N --max-time "$TIMEOUT" -D "$tmp/head" -o "$tmp/data")
ntrip_args+=(-H "Ntrip-Version: Ntrip/2.0" -H "User-Agent: NTRIP mtrtk-check-exposure")
if [ -n "$NTRIP_USER" ]; then ntrip_args+=(-u "$NTRIP_USER:$NTRIP_PASS"); fi
: >"$tmp/data"
start=$(now)
curl "${ntrip_args[@]}" "$NTRIP" 2>"$tmp/err" &
curl_pid=$!
first_byte=
polls=$((TIMEOUT * 10 + 20)) # a bound of its own, in case curl outlives its --max-time
while [ "$polls" -gt 0 ] && kill -0 "$curl_pid" 2>/dev/null; do
	polls=$((polls - 1))
	size=$(wc -c <"$tmp/data")
	if [ -z "$first_byte" ] && [ "$size" -gt 0 ]; then first_byte=$(elapsed "$start"); fi
	[ "$size" -ge "$WANT_BYTES" ] && break
	sleep 0.1
done
took=$(elapsed "$start")
kill "$curl_pid" 2>/dev/null || true
wait "$curl_pid" 2>/dev/null || true
curl_pid=
size=$(wc -c <"$tmp/data")
if [ -z "$first_byte" ] && [ "$size" -gt 0 ]; then first_byte=$took; fi

status=$(grep -E '^HTTP/' "$tmp/head" 2>/dev/null | tail -n 1 | tr -d '\r' || true)
if [ -z "$status" ]; then
	echo "FAIL: no HTTP answer from $NTRIP: $(tr -d '\r' <"$tmp/err" | head -c 300)"
	exit 1
fi
case "$status" in
*" 200"*) ;;
*" 401"*)
	echo "FAIL: $status - check the NTRIP user and password"
	exit 1
	;;
*)
	echo "FAIL: $status - check the tunnel's NTRIP hostname, the mountpoint and NTRIP_BIND"
	exit 1
	;;
esac
if grep -qi '^content-type: *gnss/sourcetable' "$tmp/head"; then
	echo "FAIL: got the caster's sourcetable, not a stream: add the mountpoint, e.g. $NTRIP/MTRK"
	exit 1
fi

# A frame counts when another RTCM3 preamble follows it exactly where its 10-bit length says it
# ends (0xD3, 6 zero bits, the length, the payload, a 24-bit CRC): stray 0xD3 bytes do not.
# Decimal bytes from od, because mawk (Debian's awk) has no hex conversion.
frames=$(od -An -v -tu1 "$tmp/data" | awk '
	{ for (i = 1; i <= NF; i++) b[n++] = $i }
	END {
		for (i = 0; i + 2 < n; i++) {
			if (b[i] == 211 && b[i + 1] < 4) {
				end = i + 6 + b[i + 1] * 256 + b[i + 2]
				if (end < n && b[end] == 211) { frames++; i = end - 1 }
			}
		}
		print frames + 0
	}')

if [ "$frames" -gt 0 ]; then
	echo "OK: $frames RTCM3 frames in $size bytes; first byte after ${first_byte} s, read for ${took} s"
	exit 0
fi
if [ "$size" -gt 0 ]; then
	echo "FAIL: $size bytes in ${took} s but no RTCM3 frames in them"
else
	echo "FAIL: no RTCM3 bytes in ${took} s ($status): is the base sending corrections? A stream"
	echo "      that stalls only through the tunnel is being buffered: use the public-IP path"
fi
exit 1
