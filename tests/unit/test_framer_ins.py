import time
from collections.abc import Iterator

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.frames import (
    FRAME_NAMERS,
    FRAME_PARSERS,
    Frame,
    Framer,
    Proto,
    register_namer,
    register_parser,
)
from mtrtk.core.router import TOPIC_RAW_SBG, TOPIC_RAW_VN, Router, topics_for


@pytest.fixture(autouse=True)
def _isolated_registries() -> Iterator[None]:
    """Vendor packages register at import; a test that swaps an entry must put it back."""
    namers, parsers = dict(FRAME_NAMERS), dict(FRAME_PARSERS)
    yield
    FRAME_NAMERS.clear()
    FRAME_NAMERS.update(namers)
    FRAME_PARSERS.clear()
    FRAME_PARSERS.update(parsers)


def test_proto_members() -> None:
    assert Proto.SBG.value == "sbg" and Proto.VN.value == "vn"


def test_sbg_identity_via_registry() -> None:
    raw = bytes([0xFF, 0x5A, 0x08, 0x00, 0x02, 0x00, 0xAA, 0xBB, 0x00, 0x00, 0x33])
    f = Frame(Proto.SBG, raw, time.monotonic(), time.time())
    FRAME_NAMERS.pop(Proto.SBG, None)
    assert f.identity == "SBG-00-08"
    register_namer(Proto.SBG, lambda r: {0x08: "EKF_NAV"}.get(r[2], f"SBG-{r[3]:02X}-{r[2]:02X}"))
    assert f.identity == "EKF_NAV"
    assert f.payload == b"\xaa\xbb"
    assert topics_for(f) == ("raw.sbg", "sbg.EKF_NAV")


def test_vn_identity_and_topics() -> None:
    binary = Frame(Proto.VN, b"\xfa\x01\x00\x00\x00\x00", time.monotonic(), time.time())
    ascii_ = Frame(Proto.VN, b"$VNRRG,01,VN-200*4B\r\n", time.monotonic(), time.time())
    assert binary.identity == "VN-BIN" and ascii_.identity == "VN-ASCII"
    assert binary.payload == binary.raw
    assert topics_for(binary) == ("raw.vn", "vn.VN-BIN")
    assert topics_for(ascii_) == ("raw.vn", "vn.VN-ASCII")


def test_parsed_dispatch_and_cache() -> None:
    calls: list[int] = []

    def parse(f: Frame) -> dict[str, bool]:
        calls.append(1)
        return {"ok": True}

    register_parser(Proto.VN, parse)
    f = Frame(Proto.VN, b"\xfa\x01\x00\x00\x00\x00", 0.0, 0.0)
    assert f.parsed() == {"ok": True} and f.parsed() == {"ok": True}
    assert calls == [1]


def test_vendor_frame_without_parser_is_an_error() -> None:
    FRAME_PARSERS.pop(Proto.SBG, None)
    f = Frame(Proto.SBG, bytes(11), 0.0, 0.0)
    with pytest.raises(ValueError, match="no parser registered"):
        f.parsed()


def test_router_accepts_a_vendor_framer() -> None:
    class SixByteVn:
        def __init__(self) -> None:
            self.stats = object()

        def feed(self, data: bytes) -> list[Frame]:
            return [Frame(Proto.VN, data[i : i + 6], 0.0, 0.0) for i in range(0, len(data), 6)]

    bus = Bus()
    raw_vn = bus.subscribe(TOPIC_RAW_VN)
    raw_sbg = bus.subscribe(TOPIC_RAW_SBG)
    router = Router(bus, SixByteVn())
    assert len(router.feed(b"\xfa\x01\x00\x00\x00\x00" * 2)) == 2
    assert raw_vn.queue.qsize() == 2 and raw_sbg.queue.qsize() == 0


def test_ubx_framer_ignores_vendor_preambles() -> None:
    """SBG (FF 5A) and VN (FA) bytes are only ever fed to their own framers; the UBX one skips."""
    framer = Framer()
    assert framer.feed(bytes([0xFF, 0x5A, 0x08, 0x00, 0x00, 0x00]) + b"\xfa\x01\x00") == []
    assert framer.stats.frames == {"ubx": 0, "rtcm3": 0, "nmea": 0}
