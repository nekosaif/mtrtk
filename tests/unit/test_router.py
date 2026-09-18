from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.router import TOPIC_RAW_NMEA, TOPIC_RAW_RTCM, TOPIC_RAW_UBX, Router, topics_for
from ubxtest import nmea_frame, rtcm_frame, ubx_frame


def test_topics_for_each_protocol() -> None:
    framer = Framer()
    ubx, rtcm, nmea = framer.feed(
        ubx_frame(0x01, 0x07, b"\x00" * 92)
        + rtcm_frame(1005, b"\x00" * 16)
        + nmea_frame("GNGGA,,,,,,0,00,99.99,,,,,,")
    )
    assert topics_for(ubx) == ("raw.ubx", "ubx.NAV-PVT")
    assert topics_for(rtcm) == ("raw.rtcm", "rtcm.1005")
    assert topics_for(nmea) == ("raw.nmea", "nmea.GNGGA")


def test_topics_for_unknown_ubx_message_uses_fallback_identity() -> None:
    (frame,) = Framer().feed(ubx_frame(0x63, 0x42, b"\x00" * 4))
    assert topics_for(frame) == ("raw.ubx", "ubx.UBX-63-42")


async def test_router_publishes_to_matching_subscribers() -> None:
    bus = Bus()
    pvt_sub = bus.subscribe("ubx.NAV-PVT")
    raw_ubx = bus.subscribe(TOPIC_RAW_UBX)
    raw_rtcm = bus.subscribe(TOPIC_RAW_RTCM)
    router = Router(bus)
    data = (
        ubx_frame(0x01, 0x07, b"\x00" * 92) + ubx_frame(0x02, 0x15, b"\x00" * 16) + rtcm_frame(1077)
    )
    frames = router.feed(data)
    assert len(frames) == 3
    assert pvt_sub.queue.qsize() == 1
    assert raw_ubx.queue.qsize() == 2
    assert raw_rtcm.queue.qsize() == 1
    assert router.bytes_in == len(data)


async def test_router_reassembles_split_feeds_and_counts_all_bytes() -> None:
    bus = Bus()
    every = bus.subscribe("*")
    router = Router(bus)
    data = ubx_frame(0x01, 0x07, b"\x00" * 92) + nmea_frame("GPGSV,1,1,01,01,00,000,00")
    assert router.feed(b"") == []
    assert router.feed(data[:20]) == []
    assert every.queue.qsize() == 0
    frames = router.feed(data[20:])
    assert [f.identity for f in frames] == ["NAV-PVT", "GPGSV"]
    assert [every.queue.get_nowait()[0] for _ in range(4)] == [
        TOPIC_RAW_UBX,
        "ubx.NAV-PVT",
        TOPIC_RAW_NMEA,
        "nmea.GPGSV",
    ]
    assert router.bytes_in == len(data)
