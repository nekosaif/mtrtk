"""sbgECom test helpers, and the generator of `fixtures/ins/sbg_frames.hex`.

    uv run python tests/sbgtest.py   # rewrite the golden fixture from `golden_frames()`

`test_framer.py::test_golden_fixture_is_reproducible` checks that the committed file is exactly
what `golden_frames()` builds, so a layout change is made here and the fixture regenerated.
(`fixtures/ins/ellipse_d_live_1s.sbg` is a one-off read-only capture of a real unit and cannot be
rebuilt; see the docstring of `test_live_ellipse_d_slice` for how it was edited.)
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

from mtrtk.rover.drivers.sbg.framer import encode
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG

GOLDEN_HEX = Path(__file__).resolve().parent / "fixtures" / "ins" / "sbg_frames.hex"
TS = 123456  # device time stamp (us) shared by the golden logs
TOW = 37815250  # 10:30:15.250 UTC on 2026-09-19 (+18 s leap) as GPS time of week, ms


def ekf_nav_payload() -> bytes:
    return struct.pack(
        "<I3f3f3df3fI",
        TS,
        0.5,
        -0.25,
        0.01,
        0.02,
        0.02,
        0.05,
        23.7275,
        90.3925,
        12.5,
        -55.2,
        0.3,
        0.3,
        0.6,
        (1 << 7) | (1 << 6) | 4,
    )


def golden_frames() -> list[bytes]:
    """The ten synthetic frames of the golden fixture, in file order."""
    sat1 = (
        struct.pack("<BbHHB", 12, 45, 180, 5 | (1 << 3) | (1 << 7), 2)
        + struct.pack("<BBB", 14, 5 | (1 << 3) | (1 << 5), 44)
        + struct.pack("<BBB", 18, 3 | (1 << 3) | (1 << 5), 38)
    )
    sat2 = struct.pack("<BbHHB", 3, 10, 90, 3 | (1 << 3) | (3 << 7), 1)
    sat2 += struct.pack("<BBB", 60, 3 | (1 << 3), 0)
    signals = (1 << 12) | (1 << 13) | (1 << 18)  # GPS L1 + L2, Galileo E1
    payloads: list[tuple[int, int, bytes]] = [
        (0, LOG["EKF_NAV"], ekf_nav_payload()),
        (
            0,
            LOG["EKF_EULER"],
            struct.pack("<I3f3fI", TS, 0.1, -0.2, math.pi / 2, 0.01, 0.01, 0.02, 0x34)
            + struct.pack("<ff", -0.02, 0.5),
        ),
        (
            0,
            LOG["UTC_TIME"],
            struct.pack(
                "<IHHbbbbbiI",
                TS,
                (3 << 1) | (1 << 5) | (2 << 6),
                2026,
                9,
                19,
                10,
                30,
                15,
                250_000_000,
                TOW,
            )
            + struct.pack("<fff", 1e-8, 1e-9, 2e-8),
        ),
        (
            0,
            LOG["GPS1_POS"],
            struct.pack(
                "<IIIdddffff",
                TS,
                (7 << 6) | signals,
                TOW,
                23.7275,
                90.3925,
                12.5,
                -55.2,
                0.02,
                0.02,
                0.05,
            )
            + struct.pack("<BHH", 18, 7, 120)
            + struct.pack("<BI", 24, 0)
            + struct.pack("<BI", 0, 3600),
        ),
        (
            0,
            LOG["GPS1_HDT"],
            struct.pack("<IHIffff", TS, 1 << 6, TOW, 91.25, 0.2, -1.0, 0.3)
            + struct.pack("<fBB", 1.02, 20, 14),
        ),
        (0, LOG["GPS1_SAT"], struct.pack("<IIB", TS, 0, 2) + sat1 + sat2),
        (0, LOG["EVENT_B"], struct.pack("<IHHHHH", 5_000_000, 0b00110, 100, 250, 0, 0)),
        (
            0,
            LOG["IMU_SHORT"],
            struct.pack("<IH3i3ih", TS, 0, 1048576, 0, -2097152, 67108864, 0, 0, 256 * 25),
        ),
        (0, LOG["STATUS"], struct.pack("<IHHIIIHIB", TS, 0x7F, 0, 0, 0x0F, 0, 0, 3600, 42)),
        (CLASS["CMD_0"], CMD["ACK"], struct.pack("<BBH", CMD["OUTPUT_CONF"], CLASS["CMD_0"], 0)),
    ]
    return [encode(cls, msg_id, payload) for cls, msg_id, payload in payloads]


def golden_hex() -> str:
    return "".join(frame.hex() + "\n" for frame in golden_frames())


if __name__ == "__main__":
    GOLDEN_HEX.write_text(golden_hex())
    print(f"wrote {GOLDEN_HEX} ({len(golden_frames())} frames)")
