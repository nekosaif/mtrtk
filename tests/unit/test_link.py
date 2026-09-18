import asyncio
from collections.abc import AsyncIterator

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkNak, LinkTimeout, UbxLink
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM
from ubxtest import FakeReceiver, ubx_frame

LinkAndRx = tuple[UbxLink, FakeReceiver]


@pytest.fixture
async def link_and_rx() -> AsyncIterator[LinkAndRx]:
    bus = Bus()
    rx = FakeReceiver(bus)
    link = UbxLink(rx, bus)
    await link.start()
    try:
        yield link, rx
    finally:
        await link.stop()


async def test_valset_ack(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    assert await link.valset([("CFG_RATE_MEAS", 1000), ("CFG_RATE_NAV", 1)], LAYERS_ALL) is True
    assert rx.config == {"CFG_RATE_MEAS": 1000, "CFG_RATE_NAV": 1}
    assert rx.valsets[0][0] == LAYERS_ALL


async def test_valset_nak(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    rx.valset_nak_keys = {"CFG_RATE_NAV"}
    assert await link.valset([("CFG_RATE_MEAS", 1000), ("CFG_RATE_NAV", 1)], LAYERS_RAM) is False


async def test_valset_timeout_retries_then_raises(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.02, retries=2)
    assert len(rx.writes) == 2


async def test_valget_roundtrip(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    rx.config = {"CFG_RATE_MEAS": 200, "CFG_USBOUTPROT_RTCM3X": 1, "CFG_TMODE_ECEF_X_HP": -5}
    got = await link.valget(["CFG_RATE_MEAS", "CFG_USBOUTPROT_RTCM3X", "CFG_TMODE_ECEF_X_HP"])
    assert got == {"CFG_RATE_MEAS": 200, "CFG_USBOUTPROT_RTCM3X": 1, "CFG_TMODE_ECEF_X_HP": -5}


async def test_valset_then_valget_bitfield_key(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    assert await link.valset([("CFG_INFMSG_UBX_USB", b"\x00")], LAYERS_RAM) is True
    assert rx.config == {"CFG_INFMSG_UBX_USB": b"\x00"}
    assert await link.valget(["CFG_INFMSG_UBX_USB"]) == {"CFG_INFMSG_UBX_USB": b"\x00"}


async def test_valget_unknown_key_raises_nak(link_and_rx: LinkAndRx) -> None:
    link, _rx = link_and_rx
    with pytest.raises(LinkNak):
        await link.valget(["CFG_MSGOUT_UBX_MON_SPAN_USB"])


async def test_poll_returns_message(link_and_rx: LinkAndRx) -> None:
    link, _rx = link_and_rx
    frame = await link.poll("MON", "MON-VER")
    assert frame.identity == "MON-VER"


async def test_poll_unsupported_returns_nak(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    rx.unsupported_polls = {(0x0A, 0x31)}
    frame = await link.poll("MON", "MON-SPAN", timeout=0.2)
    assert frame.identity == "ACK-NAK"


async def test_concurrent_requests_are_serialised(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    rx.config = {"CFG_RATE_MEAS": 1}
    results = await asyncio.gather(
        link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM),
        link.valget(["CFG_RATE_MEAS"]),
        link.poll("MON", "MON-VER"),
    )
    assert (
        results[0] is True
        and results[1] == {"CFG_RATE_MEAS": 1}
        and results[2].identity == "MON-VER"
    )


async def test_dispatch_survives_malformed_ack(link_and_rx: LinkAndRx) -> None:
    link, rx = link_and_rx
    # checksum-valid ACK-ACK carrying only one payload byte: no clsID/msgID pair to match
    rx.inject(ubx_frame(0x05, 0x01, b"\x06"))
    await asyncio.sleep(0)
    frame = await link.poll("MON", "MON-VER")
    assert frame.identity == "MON-VER"


async def test_valget_prefers_the_data_frame_over_its_ack(link_and_rx: LinkAndRx) -> None:
    # A real F9P answers a CFG-VALGET poll with the data frame *and* an ACK-ACK in the same
    # burst, so both waiters resolve in one dispatch batch. The earlier-listed key must win.
    link, rx = link_and_rx
    rx.config = {"CFG_RATE_MEAS": 200}
    for _ in range(25):
        assert await link.valget(["CFG_RATE_MEAS"]) == {"CFG_RATE_MEAS": 200}
