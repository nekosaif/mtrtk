import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Proto
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
    q = sub.queue  # type: ignore[attr-defined]
    return [q.get_nowait()[0] for _ in range(q.qsize())]


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


async def test_controller_reconnects_after_silence() -> None:
    bus = Bus()
    events = bus.subscribe("receiver.*")
    src = ScriptedSource([])
    ctrl = InsController(bus, lambda: src, OneByteFramer, None, rx_timeout_s=0.05)
    ctrl.backoff_s = (0.01, 0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    await asyncio.sleep(0.3)
    stop.set()
    await src.close()
    await asyncio.wait_for(task, 2)
    topics = drain(events)
    assert topics.count("receiver.disconnected") >= 2 and src.opened >= 2
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

    ctrl = InsController(bus, lambda: src, OneByteFramer, configure, rx_timeout_s=5.0)
    stop = asyncio.Event()
    task = asyncio.create_task(ctrl.run(stop))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if "receiver.error" in [t for t, _ in list(events.queue._queue)]:  # type: ignore[attr-defined]
            break
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


async def test_eof_of_a_recording_ends_the_run() -> None:
    bus = Bus()
    src = ScriptedSource([VN_FRAME])
    src.ends_at_eof = True
    src._closed.set()  # after the one chunk, read() returns b""
    ctrl = InsController(bus, lambda: src, OneByteFramer, None)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2)
    assert src.opened == 1 and ctrl.stats["frames"] == 1


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
    assert 2 <= epochs.queue.qsize() <= 3  # capped near 2 Hz over 0.9 s
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
