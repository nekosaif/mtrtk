"""Shared by the RINEX export tests: the raw fixture and the marker for tests that run convbin.

`MTRTK_REQUIRE_CONVBIN=1` (CI) turns a missing convbin or fixture into a failure instead of a
quiet skip, for every test carrying `needs_convbin`, not only the wrapper's own.
"""

import os
from pathlib import Path

import pytest

from mtrtk.rinex.convbin import convbin_available

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_raw_60s.ubx"
REQUIRE_CONVBIN = os.environ.get("MTRTK_REQUIRE_CONVBIN") == "1"

needs_convbin = pytest.mark.skipif(
    not REQUIRE_CONVBIN and (not convbin_available() or not FIXTURE.exists()),
    reason="RTKLIB convbin is not installed (apt install rtklib) or the raw fixture is missing",
)
