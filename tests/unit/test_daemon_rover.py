import asyncio
import logging
import os
from pathlib import Path

import httpx
import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from mtrtk.daemon import Daemon, StatusPrinter
from mtrtk.rawlog.index import list_logs

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"
# A valid RTCM 1005; any CRC-valid frame works for the plumbing test.
RTCM_FRAME = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")


async def _wait_for(predicate: object, timeout_s: float = 5.0) -> None:
    assert callable(predicate)
    for _ in range(int(timeout_s / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)


def _rover_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "role": "rover",
        "mtrtk_source": f"file:{FIXTURE}",
        "replay_speed": 5,
        "data_dir": tmp_path,
        "nmea_tcp_port": 0,
        "web_bind": "127.0.0.1",
        "web_port": 0,
        "web_allow_insecure": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_rover_daemon_connects_ntrip_serves_nmea_and_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    caster_bus = Bus()
    caster = NtripCaster(
        caster_bus, CasterConfig("MTRK", "rover", "pw", "MTRK", "BGD"), host="127.0.0.1", port=0
    )
    await caster.start()
    settings = _rover_settings(
        tmp_path, replay_log=True, ntrip_url=f"ntrip://rover:pw@127.0.0.1:{caster.port}/MTRK"
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(
            lambda: (
                daemon.rover is not None
                and daemon.rover.ntrip_client is not None
                and daemon.rover.ntrip_client.status.connected
                and daemon.web is not None
                and daemon.web.started.is_set()
            )
        )
        assert daemon.rover is not None and daemon.rover.ntrip_client is not None
        assert daemon.rover.ntrip_client.status.connected
        for f in Framer().feed(RTCM_FRAME):
            caster_bus.publish("raw.rtcm", f)
        client = daemon.rover.ntrip_client
        await _wait_for(lambda: client.status.frames_injected >= 1)
        assert client.status.frames_injected == 1
        # The frame went down the receiver link: a replay's link accepts writes and ignores
        # them, so nothing is dropped and the store notes the injection (correction age).
        assert daemon.rover.driver.dropped_bytes == 0
        assert daemon.store.state.rtk.last_rtcm_mono is not None
        assert daemon.rover.nmea is not None
        tcp = next(s for s in daemon.rover.nmea.sinks if hasattr(s, "client_count"))
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp.port)
        line = await asyncio.wait_for(reader.readline(), 5.0)
        assert line.startswith((b"$GNGGA,", b"$GNRMC,"))
        writer.close()
        assert daemon.web is not None
        async with httpx.AsyncClient() as c:
            body = (await c.get(f"http://127.0.0.1:{daemon.web.port}/api/rover")).json()
        assert body["ntrip"]["connected"] is True
        assert body["outputs"]["nmea_tcp"]["port"] == tcp.port
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        await caster.stop()
    assert daemon.rover is None
    logs = list_logs(tmp_path)
    assert logs and logs[0].msg_counts.get("RXM-RAWX", 0) > 0


async def test_set_ntrip_url_replaces_the_running_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`set_ntrip_url` stops the old client and connects a new one to the new caster."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    casters = [
        NtripCaster(Bus(), CasterConfig(m, "", "", "MTRK", "BGD"), host="127.0.0.1", port=0)
        for m in ("ONE", "TWO")
    ]
    for caster in casters:
        await caster.start()
    first, second = (f"ntrip://127.0.0.1:{c.port}/{c.config.mountpoint}" for c in casters)
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, ntrip_url=first))
    run_task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(
            lambda: (
                daemon.rover is not None
                and daemon.rover.ntrip_client is not None
                and daemon.rover.ntrip_client.status.connected
            )
        )
        assert daemon.rover is not None
        old = daemon.rover.ntrip_client
        assert old is not None and old.status.mountpoint == "ONE"
        await daemon.set_ntrip_url(second)
        new = daemon.rover.ntrip_client
        assert new is not None and new is not old
        assert old.status.connected is False  # the old one is gone, not left running
        await _wait_for(lambda: new.status.connected)
        assert new.status.connected and new.status.mountpoint == "TWO"
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        for caster in casters:
            await caster.stop()


async def test_rover_without_ntrip_url_runs_without_a_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `NTRIP_URL`, no client; a URL set later through the API starts one."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, nmea_tcp_port=-1))
    run_task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(lambda: daemon.rover is not None)
        assert daemon.rover is not None
        assert daemon.rover.ntrip_client is None
        assert daemon.rover.nmea is None  # a negative port and no other sink: no publisher
        assert daemon.rover.json_udp is None
        await daemon.rover.set_ntrip_url("ntrip://127.0.0.1:9/NONE")
        assert daemon.rover.ntrip_client is not None
        assert daemon.rover.ntrip_client.status.mountpoint == "NONE"
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)


async def test_a_bad_ntrip_url_in_the_environment_does_not_take_the_rover_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed `NTRIP_URL` is logged; the rover keeps serving and the UI can fix it."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, ntrip_url="ntrip://host-only"))
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    run_task = asyncio.create_task(daemon.run())
    try:
        with caplog.at_level(logging.ERROR, logger="mtrtk.daemon"):
            await _wait_for(lambda: daemon.rover is not None and daemon.rover.nmea is not None)
            await asyncio.sleep(0.2)
        assert daemon.rover is not None and daemon.rover.ntrip_client is None
        assert failures.queue.empty()
        assert any("NTRIP_URL" in r.getMessage() for r in caplog.records)
        assert "host-only" not in caplog.text  # the URL may carry a password: never logged
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)


async def test_json_udp_feed_reaches_the_local_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    loop = asyncio.get_running_loop()
    received: asyncio.Queue[bytes] = asyncio.Queue()

    class Receiver(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: object) -> None:
            received.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, json_udp_port=port))
    run_task = asyncio.create_task(daemon.run())
    try:
        datagram = await asyncio.wait_for(received.get(), 10.0)
        assert datagram.startswith(b"{")
        assert daemon.rover is not None and daemon.rover.json_udp is not None
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        transport.close()


async def test_pty_nmea_sink_is_linked_into_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, nmea_serial="pty"))
    link = tmp_path / "ttyMTRTK"
    run_task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(link.is_symlink)
        assert link.is_symlink()
        target = os.readlink(link)
        assert target.startswith("/dev/pts/")
        assert daemon.rover is not None and daemon.rover.nmea is not None
        sink = next(s for s in daemon.rover.nmea.sinks if hasattr(s, "slave_path"))
        assert target == sink.slave_path
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
    assert not link.is_symlink()  # a dangling link to a closed pty is not left behind


def test_status_line_carries_rtk_age_and_baseline_once_corrections_flow() -> None:
    store = StateStore(Bus())
    printer = StatusPrinter(Bus(), store, echo=lambda _: None)
    assert " rtk " not in printer.format_line()

    store.state.rtk.carr_soln_name = "Fixed"
    store.state.rtk.corr_age_s = 1.26
    assert printer.format_line().endswith(" rtk Fixed age 1.3s base -")
    store.state.rtk.baseline_m = 1234.56
    assert printer.format_line().endswith(" rtk Fixed age 1.3s base 1234.6m")


def test_udp_targets_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROLE", "rover")
    monkeypatch.setenv("NMEA_UDP_TARGETS", "192.168.1.5:10110, 10.0.0.2:5000,bad,h:0,h:70000,:5")
    s = Settings(_env_file=None)
    assert s.udp_targets() == [("192.168.1.5", 10110), ("10.0.0.2", 5000)]
