from collections import Counter
from pathlib import Path

from mtrtk.core.frames import Framer, Proto

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_10s_fixture_frames_cleanly() -> None:
    framer = Framer()
    frames = framer.feed((FIXTURES / "f9p_hpg113_raw_10s.ubx").read_bytes())
    idents = Counter(f.identity for f in frames if f.proto is Proto.UBX)
    assert idents["NAV-PVT"] >= 8
    assert idents["RXM-RAWX"] >= 8
    assert idents["RXM-SFRBX"] >= 8
    assert idents["NAV-SAT"] >= 8
    assert framer.stats.checksum_errors == 0
    assert framer.stats.garbage_bytes < 2000  # at most one partial frame at the start


def test_base_fixture_contains_rtcm_and_eoe() -> None:
    framer = Framer()
    frames = framer.feed((FIXTURES / "f9p_hpg113_base_30s.ubx").read_bytes())
    idents = Counter(f.identity for f in frames)
    assert idents["NAV-EOE"] >= 25
    assert idents["1077"] >= 25 or idents["1074"] >= 25
    assert idents["1230"] >= 4
    assert framer.stats.checksum_errors == 0
