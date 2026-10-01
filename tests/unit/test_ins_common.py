import asyncio
import gc
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Proto
from mtrtk.core.source import ByteSource
from mtrtk.core.state import MAX_TIME_MARKS, TimeMark
from mtrtk.rover.drivers.ins_common import InsController, RawCapture, StateAdapter

VN_FRAME = b"\xfa\x01\x00\x00\x00\x00"


class ScriptedSource:
    """Yields queued chunks, then blocks until closed (simulating a quiet port)."""

    name = "scripted"
    ends_at_eof = False

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.written: list[bytes] = []
        self.opened = 0
        self._closed = asyncio.Event()

    async def open(self) -> None:
        self.opened += 1

    async def read(self) -> bytes:
        if self.chunks:
            await asyncio.sleep(0)
            return self.chunks.pop(0)
        await self._closed.wait()
        return b""

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        if data == b"PING":
            self.chunks.append(VN_FRAME)  # a fake reply frame

    async def close(self) -> None:
        self._closed.set()


class FailingOpen(ScriptedSource):
    async def open(self) -> None:
        self.opened += 1
        raise OSError("no such device")


class OneByteFramer:
    """Test framer: every 6 bytes starting with 0xFA is a VN frame."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.stats = None

    def feed(self, data: bytes) -> list[Frame]:
        self.buf += data
        out = []
        while len(self.buf) >= 6:
            if self.buf[0] != 0xFA:
                del self.buf[0]
                continue
            out.append(Frame(Proto.VN, bytes(self.buf[:6]), 0.0, 0.0))
            del self.buf[:6]
        return out


def drain(sub: object) -> list[str]:
    return [topic for topic, _ in drain_items(sub)]


def drain_items(sub: object) -> list[tuple[str, Any]]:
    q = sub.queue  # type: ignore[attr-defined]
    return [q.get_nowait() for _ in range(q.qsize())]


async def next_event(sub: object, topic: str, within: float = 2.0) -> Any:
    """Await the next `(topic, payload)` on *sub* whose topic is *topic*; return the payload."""

    async def scan() -> Any:
        while True:
            got, payload = await sub.queue.get()  # type: ignore[attr-defined]
            if got == topic:
                return payload

    return await asyncio.wait_for(scan(), within)


UNSOLICITED = b"\xfa\x01\x00\x00\x00\x00"
REPLY = b"\xfa\x02\x00\x00\x00\x00"


class QueueSource:
    """A port fed from a queue: `read()` waits for the next chunk, `close()` ends it.

    `write(data)` queues `replies[data]` (if any) and then yields a few times before returning,
    as a real port's reply can arrive before the write call returns.
    """

    name = "queued"
    ends_at_eof = False

    def __init__(
        self, chunks: list[bytes] | None = None, replies: dict[bytes, list[bytes]] | None = None
    ) -> None:
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        for chunk in chunks or []:
            self.q.put_nowait(chunk)
        self.replies = replies or {}
        self.written: list[bytes] = []
        self.opened = 0
        self.closed = False

    async def open(self) -> None:
        self.opened += 1

    async def read(self) -> bytes:
        return await self.q.get()

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        for chunk in self.replies.get(data, []):
            self.q.put_nowait(chunk)
        for _ in range(5):
            await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True
        self.q.put_nowait(b"")


def sources(*made: ByteSource) -> Callable[[], ByteSource]:
    """A source factory handing out *made* in order, then fresh silent `QueueSource`s."""
    pending = list(made)
    return lambda: pending.pop(0) if pending else QueueSource()


async def test_controller_routes_frames_and_correlates_requests() -> None:
    bus = Bus()
    frames = bus.subscribe("raw.vn")
    events = bus.subscribe("receiver.*")
    src = ScriptedSource([VN_FRAME, b"garbage"])
    configured = asyncio.Event()

    async def configure(ctrl: InsController) -> None:
        reply = await ctrl.request(lambda f: f.proto is Proto.VN, b"PING", timeout_s=1.0)
        assert reply.raw[0] == 0xFA
        configured.set()

    ctrl = InsController(bus, lambda: src, OneByteFramer, configure, rx_timeout_s=0.5)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.wait_for(configured.wait(), 2)
    assert src.written == [b"PING"] and ctrl.connected
    topic, first = frames.queue.get_nowait()
    assert topic == "raw.vn" and first.identity == "VN-BIN"
    assert events.queue.get_nowait()[0] == "receiver.connected"
    assert ctrl.stats["frames"] >= 1 and ctrl.router is not None
    stop.set()
    await src.close()
    await asyncio.wait_for(task, 2)
    assert not ctrl.connected


async def test_request_returns_the_matching_reply_not_an_earlier_frame() -> None:
    """The matcher decides: an unsolicited frame that arrives first is not the reply, and a
    reply that lands while the write is still in progress is not missed."""
    bus = Bus()
    src = QueueSource(replies={b"PING": [UNSOLICITED, REPLY]})
    got: list[Frame] = []
    done = asyncio.Event()

    async def configure(ctrl: InsController) -> None:
        got.append(await ctrl.request(lambda f: f.raw[1] == 0x02, b"PING", timeout_s=0.5))
        done.set()

    ctrl = InsController(bus, sources(src), OneByteFramer, configure, rx_timeout_s=5.0)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.wait_for(done.wait(), 2)
    assert got[0].raw == REPLY
    assert ctrl.stats["frames"] == 2 and ctrl._waiters == []
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_controller_reconnects_after_silence() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    made: list[QueueSource] = []

    def factory() -> ByteSource:
        made.append(QueueSource())  # a fresh port each time: every disconnect is silence
        return made[-1]

    ctrl = InsController(bus, factory, OneByteFramer, None, rx_timeout_s=0.05)
    ctrl.backoff_s = (0.01, 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    reasons = [await next_event(events, "receiver.disconnected") for _ in range(2)]
    stop.set()
    await asyncio.wait_for(task, 2)
    assert all(r == "no data for 0.05s" for r in reasons)
    assert len(made) >= 2 and all(s.opened == 1 and s.closed for s in made)
    assert ctrl.stats["reconnects"] >= 1


async def test_controller_survives_open_failures_and_stops_promptly() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    src = FailingOpen([])
    ctrl = InsController(bus, lambda: src, OneByteFramer, None)
    ctrl.backoff_s = (0.01, 0.02)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert src.opened >= 2
    assert set(drain(events)) == {"receiver.error"}


async def test_configure_failure_keeps_the_connection() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    src = ScriptedSource([VN_FRAME])

    async def configure(ctrl: InsController) -> None:
        raise RuntimeError("unit refused the setting")

    errors = bus.subscribe("receiver.error")
    ctrl = InsController(bus, lambda: src, OneByteFramer, configure, rx_timeout_s=5.0)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    assert await next_event(errors, "receiver.error") == (
        "configuration failed: unit refused the setting"
    )
    assert ctrl.connected
    stop.set()
    await asyncio.wait_for(task, 2)
    topics = drain(events)
    assert topics[:2] == ["receiver.connected", "receiver.error"]
    assert topics[-1] == "receiver.disconnected"


async def test_request_times_out_and_write_needs_a_connection() -> None:
    bus = Bus()
    ctrl = InsController(bus, lambda: ScriptedSource([]), OneByteFramer, None)
    with pytest.raises(ConnectionError):
        await ctrl.write(b"x")
    src = ScriptedSource([])
    outcome: list[BaseException] = []

    async def configure(c: InsController) -> None:
        try:
            await c.request(lambda f: False, b"NOPE", timeout_s=0.05)
        except TimeoutError as exc:
            outcome.append(exc)

    ctrl = InsController(bus, lambda: src, OneByteFramer, configure)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if outcome:
            break
    stop.set()
    await asyncio.wait_for(task, 2)
    assert len(outcome) == 1 and src.written == [b"NOPE"]
    assert ctrl._waiters == []  # the timed-out waiter does not linger


async def test_eof_of_a_recording_ends_the_run() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    src = ScriptedSource([VN_FRAME])
    src.ends_at_eof = True
    src._closed.set()  # after the one chunk, read() returns b""
    ctrl = InsController(bus, lambda: src, OneByteFramer, None)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2)
    assert src.opened == 1 and ctrl.stats["frames"] == 1
    # The reason `ReceiverController` uses, which alerts treat as the expected end of a replay.
    assert drain_items(events)[-1] == ("receiver.disconnected", "source ended")


async def test_eof_of_a_live_port_reconnects() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    ctrl = InsController(bus, sources(QueueSource([b""])), OneByteFramer, None)
    ctrl.backoff_s = (0.01, 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    assert await next_event(events, "receiver.disconnected") == "eof"
    await next_event(events, "receiver.connected")
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_a_framer_exception_reconnects_instead_of_ending_the_run() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    calls = 0

    class FlakyFramer(OneByteFramer):
        def feed(self, data: bytes) -> list[Frame]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("bad frame")
            return super().feed(data)

    first, second = QueueSource([VN_FRAME]), QueueSource([VN_FRAME])
    ctrl = InsController(bus, sources(first, second), FlakyFramer, None)
    ctrl.backoff_s = (0.01, 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    assert (
        await next_event(events, "receiver.error") == "unexpected failure: ValueError('bad frame')"
    )
    assert await next_event(events, "receiver.disconnected") == (
        "unexpected failure: ValueError('bad frame')"
    )
    await next_event(events, "receiver.connected")
    for _ in range(100):
        if ctrl.stats["frames"]:
            break
        await asyncio.sleep(0.01)
    assert not task.done() and second.opened == 1 and ctrl.stats["frames"] == 1
    stop.set()
    await asyncio.wait_for(task, 2)


class UnpluggedRead(QueueSource):
    async def read(self) -> bytes:
        raise OSError("device unplugged")


async def test_a_read_failure_reconnects() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    second = QueueSource([VN_FRAME])
    ctrl = InsController(bus, sources(UnpluggedRead(), second), OneByteFramer, None)
    ctrl.backoff_s = (0.01, 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    assert await next_event(events, "receiver.error") == "link failure: device unplugged"
    assert await next_event(events, "receiver.disconnected") == "link failure: device unplugged"
    await next_event(events, "receiver.connected")
    assert second.opened == 1 and not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
    assert drain(events)[-1] == "receiver.disconnected"


async def test_stop_cuts_a_long_backoff_short() -> None:
    bus = Bus()
    errors = bus.subscribe("receiver.error")
    ctrl = InsController(bus, lambda: FailingOpen([]), OneByteFramer, None)
    ctrl.backoff_s = (10.0, 10.0)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await next_event(errors, "receiver.error")
    stop.set()
    await asyncio.wait_for(task, 0.5)


async def test_backoff_doubles_to_the_cap_and_resets_after_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []
    stop = asyncio.Event()

    async def record(self: InsController, delay: float, stop_: asyncio.Event) -> None:
        delays.append(delay)
        if len(delays) == 9:
            stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(InsController, "_sleep", record)
    made = [FailingOpen([]) for _ in range(7)] + [QueueSource([b""])] + [FailingOpen([])]
    ctrl = InsController(Bus(), sources(*made), OneByteFramer, None)
    await asyncio.wait_for(ctrl.run(stop), 2)
    assert delays == [1, 2, 4, 8, 16, 30, 30, 1, 2]


async def test_a_link_drop_fails_a_pending_request_with_connection_error() -> None:
    bus = Bus()
    connected = bus.subscribe("receiver.connected")
    src = QueueSource()
    ctrl = InsController(bus, sources(src), OneByteFramer, None)
    ctrl.backoff_s = (10.0, 10.0)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await next_event(connected, "receiver.connected")
    # A request from outside configure (e.g. the web API reading a register).
    req = asyncio.create_task(ctrl.request(lambda f: False, b"ASK", timeout_s=5.0))
    await asyncio.sleep(0.01)
    assert src.written == [b"ASK"] and not req.done()
    await src.close()  # the port goes away while the reply is awaited
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(req, 1)
    assert ctrl._waiters == []
    stop.set()
    await asyncio.wait_for(task, 2)


class StalledWrite(QueueSource):
    """A port whose writes block until `gate` opens (flow control, a wedged USB bridge)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.in_write = False
        self.closed_mid_write = False
        self.fail_read = asyncio.Event()

    async def read(self) -> bytes:
        get = asyncio.ensure_future(self.q.get())
        fail = asyncio.ensure_future(self.fail_read.wait())
        done, _ = await asyncio.wait({get, fail}, return_when=asyncio.FIRST_COMPLETED)
        get.cancel()
        fail.cancel()
        if fail in done:
            raise OSError("device unplugged")
        return get.result()

    async def write(self, data: bytes) -> None:
        self.written.append(data)
        self.in_write = True
        try:
            await self.gate.wait()
        finally:
            self.in_write = False

    async def close(self) -> None:
        self.closed_mid_write = self.in_write
        await super().close()


async def test_a_stalled_write_times_out_as_connection_error() -> None:
    bus = Bus()
    src = StalledWrite()
    outcome: list[BaseException] = []

    async def configure(c: InsController) -> None:
        try:
            await c.write(b"cfg")
        except ConnectionError as exc:
            outcome.append(exc)

    ctrl = InsController(bus, sources(src), OneByteFramer, configure)
    ctrl.write_timeout_s = 0.05
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    for _ in range(100):
        if outcome:
            break
        await asyncio.sleep(0.01)
    assert len(outcome) == 1 and "stalled" in str(outcome[0])
    assert ctrl.connected and not src.in_write
    stop.set()
    await asyncio.wait_for(task, 2)


async def test_teardown_waits_for_an_in_flight_write_before_closing() -> None:
    bus = Bus()
    connected = bus.subscribe("receiver.connected")
    src = StalledWrite()
    ctrl = InsController(bus, sources(src), OneByteFramer, None)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await next_event(connected, "receiver.connected")
    writer = asyncio.create_task(ctrl.write(b"rtcm"))  # e.g. the NTRIP injector
    await asyncio.sleep(0.01)
    assert src.in_write
    stop.set()
    await asyncio.sleep(0.05)
    assert not src.closed  # the port is not closed under the write
    src.gate.set()
    await asyncio.wait_for(task, 2)
    await writer
    assert src.closed and not src.closed_mid_write


async def test_a_request_failed_by_the_link_drop_leaves_no_unretrieved_exception() -> None:
    bus = Bus()
    connected = bus.subscribe("receiver.connected")
    src = StalledWrite()
    ctrl = InsController(bus, sources(src), OneByteFramer, None)
    ctrl.write_timeout_s = 0.1
    ctrl.backoff_s = (10.0, 10.0)
    loop = asyncio.get_running_loop()
    seen: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: seen.append(ctx))
    try:
        stop = asyncio.Event()
        task = asyncio.create_task(ctrl.run(stop))
        await next_event(connected, "receiver.connected")
        req = asyncio.create_task(ctrl.request(lambda f: False, b"ASK", timeout_s=5.0))
        await asyncio.sleep(0.01)
        assert src.in_write
        src.fail_read.set()  # unplugged while the request is still writing
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(req, 2)
        stop.set()
        await asyncio.wait_for(task, 2)
        del req
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert not [c for c in seen if "never retrieved" in str(c.get("message"))]


class DummyAdapter(StateAdapter):
    def apply(self, frame: Frame) -> set[str]:
        self.state.fix.num_sv += 1
        return {"fix"}


async def test_adapter_publishes_sections_and_decimated_epochs() -> None:
    bus = Bus()
    sections = bus.subscribe("state.fix")
    epochs = bus.subscribe("state.epoch")
    a = DummyAdapter(bus, nav_hz_cap=2.0)
    f = Frame(Proto.VN, VN_FRAME, 0.0, 0.0)
    for i in range(10):
        a.publish_sections(a.apply(f))
        a.end_epoch(now_mono=i * 0.1)  # 10 Hz input
    assert sections.queue.qsize() == 10
    assert epochs.queue.qsize() == 2  # 2 Hz over 0.0 .. 0.9 s: the epochs at 0.0 and 0.5
    assert a.state.epoch_count == 10
    assert a.state.last_epoch_mono == pytest.approx(0.9)
    _, snapshot = epochs.queue.get_nowait()
    assert snapshot is not a.state and snapshot.fix.num_sv == 1  # a copy, frozen at its epoch


async def test_adapter_handle_swallows_a_bad_frame() -> None:
    class Broken(StateAdapter):
        def apply(self, frame: Frame) -> set[str]:
            raise ValueError("truncated payload")

    bus = Bus()
    sub = bus.subscribe("state.*")
    Broken(bus).handle(Frame(Proto.VN, VN_FRAME, 0.0, 0.0))
    assert sub.queue.qsize() == 0


async def test_adapter_handle_publishes_the_changed_sections() -> None:
    bus = Bus()
    sub = bus.subscribe("state.*")
    a = DummyAdapter(bus)
    a.handle(Frame(Proto.VN, VN_FRAME, 0.0, 0.0))
    assert drain_items(sub) == [("state.fix", a.state.fix)]


def test_adapter_rejects_a_non_positive_rate_cap() -> None:
    for cap in (0.0, -1.0):
        with pytest.raises(ValueError, match="nav_hz_cap"):
            DummyAdapter(Bus(), nav_hz_cap=cap)


async def test_adapter_publishes_rtk_with_epochs_and_clamps_correction_age() -> None:
    bus = Bus()
    rtk = bus.subscribe("state.rtk")
    a = DummyAdapter(bus)
    a.end_epoch(now_mono=1.0)
    assert rtk.queue.qsize() == 0  # no RTCM yet: nothing to say about corrections
    a.note_rtcm_injected(now_mono=10.0)
    a.end_epoch(now_mono=9.0)  # an epoch stamped just before the injection
    assert a.state.rtk.corr_age_s == 0.0
    assert drain_items(rtk) == [("state.rtk", a.state.rtk)]


async def test_adapter_falling_edge_marks_update_the_list_but_not_the_event() -> None:
    bus = Bus()
    mark_events = bus.subscribe("state.time_mark")
    lists = bus.subscribe("state.time_marks")
    a = DummyAdapter(bus)
    a.push_time_mark(TimeMark(channel=0, count=1, new_rising=True))
    a.push_time_mark(TimeMark(channel=0, count=1, new_rising=False))
    assert mark_events.queue.qsize() == 1
    assert lists.queue.qsize() == 2 and len(a.state.time_marks) == 2


async def test_adapter_time_marks_and_rtcm_note() -> None:
    bus = Bus()
    marks = bus.subscribe("state.time_mark")
    a = DummyAdapter(bus)
    for i in range(MAX_TIME_MARKS + 5):
        a.push_time_mark(
            TimeMark(channel=0, count=i, rising_week=2400, rising_tow_s=float(i), new_rising=True)
        )
    assert len(a.state.time_marks) == MAX_TIME_MARKS
    assert marks.queue.qsize() == MAX_TIME_MARKS + 5
    assert a.state.time_marks[-1].count == MAX_TIME_MARKS + 4
    a.note_rtcm_injected(now_mono=100.0)
    assert a.state.rtk.last_rtcm_mono == 100.0
    a.end_epoch(now_mono=101.5)
    assert a.state.rtk.corr_age_s == pytest.approx(1.5)


def test_raw_capture_rotates_hourly(tmp_path: Path) -> None:
    bus = Bus()
    rotated = bus.subscribe("rawcapture.rotated")
    cap = RawCapture(tmp_path, "MTRK", "vnraw", bus, vendor="vectornav")
    cap.write(b"early")  # buffered until a clock exists
    cap.note_utc(datetime(2026, 9, 19, 10, 59, 59, tzinfo=UTC))
    cap.write(b"abc")
    cap.note_utc(datetime(2026, 9, 19, 11, 0, 1, tzinfo=UTC))
    cap.write(b"def")
    cap.close()
    p10 = tmp_path / "ins" / "2026" / "262" / "MTRK_20260919_10.vnraw"
    p11 = p10.with_name("MTRK_20260919_11.vnraw")
    assert p10.read_bytes() == b"earlyabc" and p11.read_bytes() == b"def"
    side = json.loads(p10.with_suffix(".json").read_text())
    assert side["vendor"] == "vectornav" and side["bytes"] == 8 and side["frames"] == 2
    assert side["start_utc"].startswith("2026-09-19T10:59:59")
    # As `RawLogWriter`: the hour ends at the reading that closed it.
    assert side["end_utc"].startswith("2026-09-19T11:00:01")
    assert json.loads(p11.with_suffix(".json").read_text())["bytes"] == 3
    assert rotated.queue.qsize() == 2
    assert cap.current_path is None


def test_raw_capture_resumes_an_hour_after_a_restart(tmp_path: Path) -> None:
    bus = Bus()
    t = datetime(2026, 9, 19, 10, 15, tzinfo=UTC)
    first = RawCapture(tmp_path, "MTRK", "sbgraw", bus, vendor="sbg")
    first.note_utc(t)
    first.write(b"one")
    first.close()
    second = RawCapture(tmp_path, "MTRK", "sbgraw", bus, vendor="sbg")
    second.note_utc(t.replace(minute=30))
    second.write(b"two")
    second.close()
    path = tmp_path / "ins" / "2026" / "262" / "MTRK_20260919_10.sbgraw"
    side = json.loads(path.with_suffix(".json").read_text())
    assert path.read_bytes() == b"onetwo"
    assert side["bytes"] == 6 and side["frames"] == 2
    assert side["start_utc"].startswith("2026-09-19T10:15")


def test_raw_capture_names_by_host_clock_when_the_unit_never_tells_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mtrtk.rover.drivers.ins_common as ic

    monkeypatch.setattr(ic, "PENDING_CAP_BYTES", 4)
    cap = RawCapture(tmp_path, "MTRK", "vnraw", Bus(), vendor="vectornav")
    cap.write(b"123")
    assert cap.current_path is None
    cap.write(b"456")
    assert cap.current_path is not None
    cap.close()
    side = json.loads(next(tmp_path.rglob("*.json")).read_text())
    assert side["time_source"] == "host" and side["bytes"] == 6


class Clock:
    """Stands in for the monotonic and host clocks of `ins_common`."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, host: datetime) -> None:
        import mtrtk.rover.drivers.ins_common as ic

        self.mono = 1000.0
        self.host = host
        monkeypatch.setattr(ic, "_mono", lambda: self.mono)
        monkeypatch.setattr(ic, "_host_now", lambda: self.host)


def test_raw_capture_ignores_a_brief_step_back_across_the_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Clock(monkeypatch, datetime(2026, 9, 19, 11, tzinfo=UTC))
    bus = Bus()
    rotated = bus.subscribe("rawcapture.rotated")
    cap = RawCapture(tmp_path, "MTRK", "vnraw", bus, vendor="vectornav")
    t = datetime(2026, 9, 19, 11, 0, 0, 100_000, tzinfo=UTC)
    cap.note_utc(t)
    cap.write(b"a")
    cap.note_utc(t - timedelta(seconds=0.2))  # 10:59:59.9: a correction, not a new hour
    cap.write(b"b")
    cap.note_utc(t + timedelta(seconds=0.1))
    cap.write(b"c")
    cap.close()
    assert rotated.queue.qsize() == 1
    assert [p.name for p in tmp_path.rglob("*.vnraw")] == ["MTRK_20260919_11.vnraw"]


def test_raw_capture_follows_a_step_back_that_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Clock(monkeypatch, datetime(2026, 9, 19, 11, tzinfo=UTC))
    cap = RawCapture(tmp_path, "MTRK", "vnraw", Bus(), vendor="vectornav")
    cap.note_utc(datetime(2026, 9, 19, 11, 0, 1, tzinfo=UTC))
    cap.write(b"late")
    cap.note_utc(datetime(2026, 9, 19, 10, 30, 0, tzinfo=UTC))
    cap.write(b"held")
    cap.note_utc(datetime(2026, 9, 19, 10, 30, 6, tzinfo=UTC))  # still there 6 s later
    cap.write(b"back")
    cap.close()
    day = tmp_path / "ins" / "2026" / "262"
    assert (day / "MTRK_20260919_11.vnraw").read_bytes() == b"lateheld"
    assert (day / "MTRK_20260919_10.vnraw").read_bytes() == b"back"


def test_raw_capture_rotates_on_elapsed_time_when_the_unit_goes_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock(monkeypatch, datetime(2026, 9, 19, 10, tzinfo=UTC))
    cap = RawCapture(tmp_path, "MTRK", "sbgraw", Bus(), vendor="sbg")
    cap.note_utc(datetime(2026, 9, 19, 10, 59, 58, tzinfo=UTC))
    cap.write(b"a")
    clock.mono += 3.0  # no UTC reading since; the projected clock is 11:00:01
    cap.write(b"b")
    cap.close()
    p10 = tmp_path / "ins" / "2026" / "262" / "MTRK_20260919_10.sbgraw"
    p11 = p10.with_name("MTRK_20260919_11.sbgraw")
    assert p10.read_bytes() == b"a" and p11.read_bytes() == b"b"
    assert json.loads(p10.with_suffix(".json").read_text())["end_utc"].startswith(
        "2026-09-19T11:00:01"
    )


def test_raw_capture_host_mode_rotates_on_the_host_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mtrtk.rover.drivers.ins_common as ic

    monkeypatch.setattr(ic, "PENDING_CAP_BYTES", 4)
    clock = Clock(monkeypatch, datetime(2026, 10, 1, 11, 59, 59, tzinfo=UTC))
    cap = RawCapture(tmp_path, "MTRK", "vnraw", Bus(), vendor="vectornav")
    cap.write(b"12345")
    first = cap.current_path
    clock.host = datetime(2026, 10, 1, 12, 0, 1, tzinfo=UTC)
    cap.write(b"678")
    assert first is not None and first.name == "MTRK_20261001_11.vnraw"
    assert cap.current_path is not None and cap.current_path.name == "MTRK_20261001_12.vnraw"
    cap.close()
    assert cap.current_path is None and first.read_bytes() == b"12345"


def test_raw_capture_refreshes_the_sidecar_while_the_hour_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mtrtk.rover.drivers.ins_common as ic

    clock = Clock(monkeypatch, datetime(2026, 9, 19, 10, tzinfo=UTC))
    cap = RawCapture(tmp_path, "MTRK", "sbgraw", Bus(), vendor="sbg")
    cap.note_utc(datetime(2026, 9, 19, 10, 15, tzinfo=UTC))
    cap.write(b"abc")
    assert cap.current_path is not None
    side_path = cap.current_path.with_suffix(".json")
    clock.mono += ic.SIDECAR_EVERY_S
    cap.write(b"de")  # no flush, no close: a crash here must not leave bytes=0 on disk
    side = json.loads(side_path.read_text())
    assert side["bytes"] == 5 and side["frames"] == 2
    assert cap.current_path.read_bytes() == b"abcde"
    cap.close()


def test_raw_capture_drops_an_implausible_unit_clock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cap = RawCapture(tmp_path, "MTRK", "sbgraw", Bus(), vendor="sbg")
    with caplog.at_level(logging.WARNING):
        cap.note_utc(datetime(1980, 1, 6, tzinfo=UTC))  # an unsynced GNSS epoch
        cap.note_utc(datetime(2000, 1, 1, tzinfo=UTC))
    cap.write(b"x")
    assert cap.current_path is None  # still buffering for a real time
    assert len([r for r in caplog.records if "implausible" in r.getMessage()]) == 1
    cap.note_utc(datetime(2026, 9, 19, 10, tzinfo=UTC))
    path = cap.current_path
    cap.close()
    assert path is not None and path.read_bytes() == b"x"
