import asyncio
from collections.abc import AsyncIterator

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkNak, LinkTimeout, UbxLink
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM
from ubxtest import ACK_ACK, ACK_NAK, CFG_VALSET, FakeReceiver, ubx_frame

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


async def test_a_late_answer_is_not_given_to_the_next_request(link_and_rx: LinkAndRx) -> None:
    """The answer to a request that already timed out belongs to nobody.

    ACKs correlate on class/id alone, so the stale one used to resolve the *next* VALSET: a
    base could be told its fixed position was rejected when the receiver had accepted it.
    """
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.02, retries=1)
    rx.silent = False
    # The first VALSET's NAK, arriving after its caller gave up and just before the next one
    # registers its waiter - the dispatcher sees it first.
    rx.inject(ubx_frame(*ACK_NAK, bytes(CFG_VALSET)))
    assert await link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM) is True
    assert rx.config == {"CFG_RATE_NAV": 1}


async def test_a_lost_race_does_not_discard_the_next_answer(link_and_rx: LinkAndRx) -> None:
    """Only a request that got nothing leaves a discard behind - a poll answered by its data
    frame retires an unused ACK waiter every time, and those must not eat later answers."""
    link, rx = link_and_rx
    rx.config = {"CFG_RATE_MEAS": 200}
    for _ in range(3):
        assert await link.valget(["CFG_RATE_MEAS"]) == {"CFG_RATE_MEAS": 200}
    rx.unsupported_polls = {(0x0A, 0x31)}
    assert (await link.poll("MON", "MON-SPAN", timeout=0.2)).identity == "ACK-NAK"


async def test_a_silent_request_does_not_latch_out_the_next_ones(link_and_rx: LinkAndRx) -> None:
    """One lost ACK costs exactly one extra timeout - on the request that follows it - and the
    chain stops there.

    The credit a silent request leaves is spent on the *next* request's own answer when that
    request follows back to back, the way `_apply_with_bisect` sends its chunked VALSETs. That
    request times out in turn; were it to book a credit of its own, every later request would
    be answered by a credit and book another one, and a single silent VALSET would latch the
    link into permanent failure while the receiver goes on applying and acknowledging every
    write it is sent. The robbed request books nothing, so its retry is answered normally.
    """
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.05, retries=1)
    rx.silent = False
    assert await link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM, timeout=0.005) is True
    assert await link.valset([("CFG_RATE_NAV", 2)], LAYERS_RAM, timeout=0.005) is True
    assert await link.valset([("CFG_RATE_NAV", 3)], LAYERS_RAM, timeout=0.005) is True
    assert await link.valget(["CFG_RATE_NAV"], timeout=0.005) == {"CFG_RATE_NAV": 3}
    assert rx.config == {"CFG_RATE_NAV": 3}
    # the silent attempt; the request robbed by its credit, written twice; one write for each
    # of the other three: the single extra timeout, and nothing else retried
    assert len(rx.writes) == 1 + 2 + 1 + 1 + 1
    assert link._stale == {}


async def test_a_late_answer_arriving_after_the_next_waiter_registered_is_discarded(
    link_and_rx: LinkAndRx,
) -> None:
    """A times out; B registers its waiter; only *then* A's late answer arrives, and after it
    B's own. The credit eats A's NAK and B receives its own ACK, and nothing is left behind:
    B was robbed of a frame but not of its answer, so it owes no credit either."""
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.005, retries=1)
    b = asyncio.create_task(link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM, timeout=1.0, retries=1))
    await asyncio.sleep(0)  # B runs up to its wait: waiter registered, request written
    assert len(rx.writes) == 2
    rx.inject(ubx_frame(*ACK_NAK, bytes(CFG_VALSET)))  # A's answer, late: a NAK
    rx.inject(ubx_frame(*ACK_ACK, bytes(CFG_VALSET)))  # B's own answer
    assert await b is True
    assert len(rx.writes) == 2  # B never retried
    assert link._stale == {}
    rx.silent = False
    assert await link.valset([("CFG_RATE_NAV", 2)], LAYERS_RAM, timeout=0.005) is True
    assert len(rx.writes) == 3


async def test_a_slow_receiver_is_answered_on_the_retry(link_and_rx: LinkAndRx) -> None:
    """Every answer arrives between one and two timeouts after its request: after the caller
    gave up on that attempt, while its retry is already waiting. The retry must return the
    receiver's real verdict instead of timing out as well.

    The answer to attempt n is released when attempt n+1 is written, so it lands while n+1 is
    the waiter at the head of the queue: exactly the frame the credit left by attempt n eats.
    """
    link, rx = link_and_rx
    rx.valset_nak_keys = {"CFG_RATE_NAV"}
    held: list[bytes] = []
    real_inject, real_write = rx.inject, rx.write

    async def slow_write(data: bytes) -> None:
        previous, held[:] = held[:], []
        await real_write(data)  # this attempt's own answer is held back ...
        for frame in previous:
            real_inject(frame)  # ... and the previous attempt's lands now, one timeout late

    rx.inject = held.append  # type: ignore[method-assign]
    rx.write = slow_write  # type: ignore[method-assign]
    assert await link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM, timeout=0.02) is False
    assert len(rx.writes) == 3  # attempt 1 unanswered in time, attempt 2 robbed, attempt 3 answered


async def test_an_expired_credit_is_purged_not_spent(link_and_rx: LinkAndRx) -> None:
    """A credit is good only for the timeout of the request that left it: past that, the
    receiver was simply silent and nothing late is coming. The next answer is the next
    request's own, and that request must not be robbed of it."""
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.005, retries=1)
    assert len(link._stale["ack:068a"]) == 1
    link._stale["ack:068a"][0] = asyncio.get_running_loop().time() - 1.0  # lapsed, no sleeping
    rx.silent = False
    assert await link.valset([("CFG_RATE_NAV", 1)], LAYERS_RAM, timeout=0.005) is True
    assert len(rx.writes) == 2  # nothing retried
    assert link._stale == {}


async def test_credits_do_not_pile_up_on_a_dead_receiver(link_and_rx: LinkAndRx) -> None:
    """A receiver that never answers leaves one unclaimed credit per request; the lapsed ones
    are dropped when the next is booked, so the ledger cannot grow for the life of the link."""
    link, rx = link_and_rx
    rx.silent = True
    for _ in range(5):
        with pytest.raises(LinkTimeout):
            await link.valset([("CFG_RATE_MEAS", 1000)], LAYERS_RAM, timeout=0.002, retries=1)
    assert len(link._stale["ack:068a"]) <= 2


async def test_only_the_ack_key_is_ever_credited(link_and_rx: LinkAndRx) -> None:
    """A poll's data key is also the identity of the unsolicited periodic message of the same
    name, which would spend the credit at once - and a CFG-VALGET answers with its data frame,
    so crediting that key would make the next read fail for no reason."""
    link, rx = link_and_rx
    rx.silent = True
    with pytest.raises(LinkTimeout):
        await link.valget(["CFG_RATE_MEAS"], timeout=0.005)
    assert set(link._stale) == {"ack:068b"}
    rx.silent = False
    rx.config = {"CFG_RATE_MEAS": 200}
    assert await link.valget(["CFG_RATE_MEAS"]) == {"CFG_RATE_MEAS": 200}
