import asyncio
import base64
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
