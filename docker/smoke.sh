#!/bin/sh
# Smoke test run INSIDE the built image (CI, once per platform): the convbin the image ships
# (demo5) converts the raw fixture with the flags the wrapper uses, and the rnx2crx bundled in
# the hatanaka wheel - compiled for this platform - Hatanaka-compresses the result.
#   docker run --rm --entrypoint sh -v "$PWD/tests/fixtures:/fx:ro" -v "$PWD/docker:/smoke:ro" IMAGE /smoke/smoke.sh
set -eu
FX=${FX:-/fx/f9p_hpg113_raw_60s.ubx}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cp "$FX" "$WORK/spliced.ubx"
cd "$WORK"
convbin -r ubx -v 3.04 -od -os -oi -ot -ol -f 2 -scan -ti 30 \
    -hm MTRK -hc "mtrtk smoke" -o o.rnx -n o.nav spliced.ubx >/dev/null 2>&1
grep -q "END OF HEADER" o.rnx
grep -q "TIME OF FIRST OBS" o.rnx
test "$(grep -c '^>' o.rnx)" -ge 1
grep -q "END OF HEADER" o.nav
python - <<'PY'
import importlib.resources
import subprocess
import sys

# Where mtrtk.rinex.export.rnx2crx_binary() finds it.
rnx2crx = str(importlib.resources.files("hatanaka.bin").joinpath("rnx2crx"))
with open("o.rnx", "rb") as src, open("o.crx", "wb") as dst:
    rc = subprocess.run([rnx2crx, "-"], stdin=src, stdout=dst, check=False).returncode
if rc not in (0, 2):
    sys.exit(f"rnx2crx exited {rc}")
with open("o.crx") as fh:
    if "CRINEX" not in fh.readline():
        sys.exit("rnx2crx wrote no Compact RINEX header")
PY
mtrtk --version
echo "smoke ok: $(uname -m), $(grep -c '^>' o.rnx) epochs"
