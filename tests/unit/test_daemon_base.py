import asyncio
import base64
import logging
import signal
from pathlib import Path

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer, Proto
from mtrtk.core.statestore import StateStore
from mtrtk.daemon import Daemon, StatusPrinter
from mtrtk.rawlog.index import list_logs
from mtrtk.store.db import Database

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"


async def test_base_daemon_on_replay_serves_rtcm_logs_and_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=f"file:{FIXTURE}",
        replay_speed=5,
        replay_log=True,
        data_dir=tmp_path,
        ntrip_bind="127.0.0.1",
        ntrip_port=0,
        ntrip_user="rover",
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    for _ in range(100):
        await asyncio.sleep(0.02)
        if daemon.caster is not None and daemon.caster._server is not None:
            break
    assert daemon.caster is not None
    auth = base64.b64encode(b"rover:pw").decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", daemon.caster.port)
    request = f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP test\r\nAuthorization: Basic {auth}\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    assert await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0) == b"ICY 200 OK\r\n\r\n"
    data = await asyncio.wait_for(reader.read(4096), 5.0)
    frames = Framer().feed(data)
    assert frames and all(f.proto is Proto.RTCM3 for f in frames)
    writer.close()
    await asyncio.wait_for(run_task, 30.0)  # replay ends -> daemon exits
    logs = list_logs(tmp_path)
    assert logs and logs[0].msg_counts.get("RXM-RAWX", 0) > 0 and logs[0].complete is True
    db = Database(tmp_path / "mtrtk.db")
    await db.open()
    n = (await db.fetchone("SELECT COUNT(*) FROM samples_1s"))[0]
    events = await db.fetchall("SELECT kind FROM events")
    await db.close()
    assert n >= 20
    assert {r["kind"] for r in events} >= set()  # table exists; replay produces no alerts by itself


async def test_replay_without_replay_log_writes_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=f"file:{FIXTURE}",
        replay_speed=0,
        data_dir=tmp_path,
        ntrip_bind="127.0.0.1",
        ntrip_port=0,
    )
    await asyncio.wait_for(Daemon(settings).run(), 30.0)
    assert list_logs(tmp_path) == []
    assert (tmp_path / "mtrtk.db").exists()


async def test_supervisor_restarts_failed_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(
        _env_file=None, mtrtk_source=f"file:{FIXTURE}", replay_speed=0, data_dir=tmp_path
    )
    daemon = Daemon(settings)
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    calls = 0

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        daemon.stop.set()

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("mtrtk.daemon.asyncio.sleep", fake_sleep)
    await daemon._supervise("flaky", flaky)
    assert calls == 2 and sleeps == [1.0]
    assert failures.queue.get_nowait()[1] == {"name": "flaky", "error": "RuntimeError: boom"}


async def test_status_line_carries_the_survey_in_while_one_is_running() -> None:
    """A base operator watches the survey-in on the same line as the fix, not a second stream."""
    store = StateStore(Bus())
    printer = StatusPrinter(Bus(), store, echo=lambda _: None)
    assert "svin" not in printer.format_line()

    store.state.survey_in.active = True
    store.state.survey_in.dur_s = 42
    store.state.survey_in.mean_acc_m = 1.234
    assert printer.format_line().endswith("svin 42s \u03c31.23m \u2026")

    store.state.survey_in.active = False
    store.state.survey_in.valid = True
    assert printer.format_line().endswith("svin 42s \u03c31.23m \u2713")


async def test_supervisor_restarts_a_raw_logger_that_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`asyncio.wait` never raises what its awaitables raised.

    Without re-raising the gathered result, a writer that dies on a full disk looks to the
    supervisor like a clean return: raw logging would be silently dead for the rest of the run.
    """
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=f"file:{FIXTURE}",
        replay_speed=0,
        replay_log=True,
        data_dir=tmp_path,
    )
    daemon = Daemon(settings)
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    writers: list[object] = []

    class FlakyWriter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.firmware = ""
            self.site: str | None = None
            writers.append(self)

        def stop(self) -> None:
            pass

        async def run(self, stop: asyncio.Event) -> None:
            if len(writers) == 1:
                raise OSError("no space left on device")
            daemon.stop.set()  # the restarted writer ends the run cleanly

    async def fake_sleep(d: float) -> None:
        pass

    monkeypatch.setattr("mtrtk.daemon.RawLogWriter", FlakyWriter)
    monkeypatch.setattr("mtrtk.daemon.asyncio.sleep", fake_sleep)
    await daemon._supervise("rawlog", daemon._run_rawlog)
    assert len(writers) == 2  # the supervisor built a second writer
    assert failures.queue.get_nowait()[1] == {
        "name": "rawlog",
        "error": "OSError: no space left on device",
    }


async def test_the_daemon_publishes_the_writer_while_the_raw_logger_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`DELETE /api/logs/{name}` asks the writer which hour is open instead of guessing."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_base_settings(tmp_path, replay_speed=0, replay_log=True))
    assert daemon.rawlog is None
    task = asyncio.create_task(daemon._run_rawlog())
    await _wait_for(lambda: daemon.rawlog is not None)
    writer = daemon.rawlog
    daemon.stop.set()
    await asyncio.wait_for(task, 5.0)
    # Cleared before the supervisor can build a replacement, so the API never reads a dead one.
    assert daemon.rawlog is None and writer is not None


async def test_raw_logger_returns_quietly_when_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop path stays exception-free, so a clean shutdown never looks like a failure."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=f"file:{FIXTURE}",
        replay_speed=0,
        replay_log=True,
        data_dir=tmp_path,
    )
    daemon = Daemon(settings)
    daemon.stop.set()
    await daemon._run_rawlog()
    assert list_logs(tmp_path) == []  # nothing was written, and nothing raised


def _base_settings(tmp_path: Path, **extra: object) -> Settings:
    kwargs: dict[str, object] = {
        "_env_file": None,
        "role": "base",
        "mtrtk_source": f"file:{FIXTURE}",
        "data_dir": tmp_path,
        "ntrip_bind": "127.0.0.1",
        "ntrip_port": 0,
        **extra,
    }
    return Settings(**kwargs)  # type: ignore[arg-type]


async def _wait_for(predicate, timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


async def test_restarted_consumers_leave_no_subscriptions_on_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor restarts a failed consumer for the life of the process.

    Every restart re-subscribes, so a consumer that only *closes* its subscription leaves the
    dead one in `Bus._subs` for ever: publishing gets slower with every restart and the queued
    items are never read.
    """
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_base_settings(tmp_path, replay_speed=0, replay_log=True))
    baseline = daemon.bus.subscriber_count
    for _ in range(3):
        daemon.stop = asyncio.Event()
        caster_task = asyncio.create_task(daemon._run_caster())
        await _wait_for(lambda: daemon.caster is not None)
        daemon.stop.set()
        await asyncio.wait_for(caster_task, 5.0)
        await daemon._run_rawlog()  # stop is set: the writer opens nothing and returns
    assert daemon.bus.subscriber_count == baseline


async def test_a_caster_that_cannot_bind_leaves_no_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NtripCaster` subscribes in its constructor, so a failed `start()` has to be undone."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    taken = int(blocker.sockets[0].getsockname()[1])
    try:
        daemon = Daemon(_base_settings(tmp_path, replay_speed=0, ntrip_port=taken))
        baseline = daemon.bus.subscriber_count
        for _ in range(3):
            with pytest.raises(OSError):
                await daemon._run_caster()
        assert daemon.bus.subscriber_count == baseline
        assert daemon.caster is None
    finally:
        blocker.close()
        await blocker.wait_closed()


async def test_a_cancelled_run_is_not_recorded_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled daemon was shut down from outside; `error` is for what actually broke."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_base_settings(tmp_path, replay_speed=1))
    task = asyncio.create_task(daemon.run())
    await _wait_for(lambda: daemon.store.state.connected)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 30.0)
    assert daemon.error is None


async def test_the_shutdown_names_its_trigger_and_logs_the_last_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator reading the log has to see that the daemon was asked to stop, by what, and
    that it got all the way through its own teardown."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_base_settings(tmp_path, replay_speed=1))
    with caplog.at_level(logging.INFO, logger="mtrtk.daemon"):
        task = asyncio.create_task(daemon.run())
        await _wait_for(lambda: daemon.store.state.connected)
        daemon._request_stop(signal.Signals.SIGTERM.name)
        await asyncio.wait_for(task, 30.0)
    assert "shutting down (SIGTERM)" in caplog.text
    assert caplog.text.index("shutting down") < caplog.text.index("mtrtk stopped")
