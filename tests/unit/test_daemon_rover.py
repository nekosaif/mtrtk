import asyncio
import functools
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from mtrtk.daemon import Daemon, StatusPrinter
from mtrtk.rawlog.index import list_logs
from mtrtk.rover.sinks import SerialSink
from mtrtk.store.repos import EventsRepo

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"
# A valid RTCM 1005; any CRC-valid frame works for the plumbing test.
RTCM_FRAME = bytes.fromhex("d300133ed7fd0382dfdc1c403db34fe8fe0cef5e6b30bd2e23")


# A whole daemon test runs in a few seconds; one that wedges must fail, not hang the suite
# (there is no pytest-timeout).
TEST_TIMEOUT_S = 60.0


def bounded(
    test: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """Fail an async test that runs past `TEST_TIMEOUT_S` instead of letting it hang."""

    @functools.wraps(test)
    async def run(*args: Any, **kwargs: Any) -> None:
        async with asyncio.timeout(TEST_TIMEOUT_S):
            await test(*args, **kwargs)

    return run


async def _wait_for(predicate: Callable[[], object], timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not met in {timeout_s}s")


def _free_port() -> int:
    """A port nothing listens on (bound, read, released): never a fixed host port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


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


@bounded
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


@bounded
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
        # A URL that does not parse is refused before anything is stopped: the working
        # client keeps its connection.
        with pytest.raises(ValueError):
            await daemon.set_ntrip_url("ntrip://host-only")
        assert daemon.rover.ntrip_client is old and old.status.connected
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


@bounded
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
        await daemon.rover.set_ntrip_url(f"ntrip://127.0.0.1:{_free_port()}/NONE")
        assert daemon.rover.ntrip_client is not None
        assert daemon.rover.ntrip_client.status.mountpoint == "NONE"
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)


@bounded
async def test_a_bad_ntrip_url_in_the_environment_does_not_take_the_rover_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed `NTRIP_URL` is logged; the rover keeps serving and the UI can fix it."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    # Credentials but no mountpoint, so it still fails to parse.
    bad_url = "ntrip://user:s3cret@host-only"
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, ntrip_url=bad_url))
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    run_task = asyncio.create_task(daemon.run())
    try:
        with caplog.at_level(logging.ERROR, logger="mtrtk.daemon"):
            await _wait_for(lambda: daemon.rover is not None and daemon.rover.nmea is not None)
            await asyncio.sleep(0.2)
        assert daemon.rover is not None and daemon.rover.ntrip_client is None
        assert failures.queue.empty()
        assert any("NTRIP_URL" in r.getMessage() for r in caplog.records)
        # The URL carries the password: neither it nor any part of the URL is logged.
        assert "s3cret" not in caplog.text
        assert "host-only" not in caplog.text
        # ... and the event log says so too, also without the URL: the UI would otherwise
        # only show "No caster configured" while NTRIP_URL is set.
        assert daemon.db is not None
        events = await EventsRepo(daemon.db).list(limit=20)
        bad = [e for e in events if e.kind == "ntrip_url_invalid"]
        assert len(bad) == 1 and bad[0].level == "warning"
        assert "s3cret" not in bad[0].message and "host-only" not in bad[0].message
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)


@bounded
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


@bounded
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
    monkeypatch.setenv(
        "NMEA_UDP_TARGETS", "192.168.1.5:10110, 10.0.0.2:5000,bad,h:0,h:70000,:5,h:\u00b2"
    )
    s = Settings(_env_file=None)
    assert s.udp_targets() == [("192.168.1.5", 10110), ("10.0.0.2", 5000)]


def test_output_ports_are_bounded() -> None:
    """A port the OS would silently wrap (70000 -> 4464) is a settings error, not a feed sent
    somewhere else."""
    for bad in ({"json_udp_port": 70000}, {"json_udp_port": 0}, {"nmea_tcp_port": 70000}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, role="rover", **bad)  # type: ignore[arg-type]
    for good in ({"json_udp_port": 65535}, {"nmea_tcp_port": -1}, {"nmea_tcp_port": 0}):
        Settings(_env_file=None, role="rover", **good)  # type: ignore[arg-type]


def test_consumers_follow_the_role(tmp_path: Path) -> None:
    """Raw logging is for both roles; the caster is the base's, the rover consumer the rover's."""
    rover = Daemon(_rover_settings(tmp_path, replay_log=True))
    names = [name for name, _ in rover._consumers()]
    assert {"rawlog", "retention", "rover", "web"} <= set(names)
    assert "ntrip" not in names and "basemode" not in names
    base = Daemon(_rover_settings(tmp_path, role="base", replay_log=True, ntrip_password="pw"))
    names = [name for name, _ in base._consumers()]
    assert {"rawlog", "retention", "ntrip", "web"} <= set(names)
    assert "rover" not in names


def test_nmea_serial_runs_at_its_own_baud(tmp_path: Path) -> None:
    """An NMEA consumer's baud is its own: raising BAUD for the receiver link must not move it."""
    port = str(tmp_path / "ttyNMEA")  # never opened: building the sink does not touch it
    settings = _rover_settings(tmp_path, baud=460800, nmea_serial=port, nmea_tcp_port=-1)
    (sink,) = Daemon(settings)._nmea_sinks()
    assert isinstance(sink, SerialSink) and sink.path == port and sink.baud == 115200
    settings = _rover_settings(tmp_path, nmea_serial=port, nmea_serial_baud=9600, nmea_tcp_port=-1)
    (sink,) = Daemon(settings)._nmea_sinks()
    assert isinstance(sink, SerialSink) and sink.baud == 9600


@bounded
async def test_nmea_udp_targets_get_sentences_and_bad_entries_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    loop = asyncio.get_running_loop()
    received: asyncio.Queue[bytes] = asyncio.Queue()

    class Receiver(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: object) -> None:
            received.put_nowait(data)

    transport, _ = await loop.create_datagram_endpoint(Receiver, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    settings = _rover_settings(
        tmp_path, replay_loop=True, nmea_tcp_port=-1, nmea_udp_targets=[f"127.0.0.1:{port}", "bad"]
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    try:
        with caplog.at_level(logging.WARNING, logger="mtrtk.daemon"):
            datagram = await asyncio.wait_for(received.get(), 10.0)
        assert datagram.startswith(b"$G")
        assert "NMEA_UDP_TARGETS: ignoring 1 " in caplog.text
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        transport.close()


@bounded
async def test_the_rover_is_unpublished_before_its_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the rover winds down it is already gone to the API, and once it has stopped a new
    NTRIP URL is refused rather than starting a client nobody will stop."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, nmea_tcp_port=-1))
    seen_at_teardown: list[object] = []
    stop_consumers = daemon._stop_consumers

    async def recording(tasks: list[asyncio.Task[None]]) -> None:
        if any(task.get_name() == "points" for task in tasks):  # the rover's own children
            seen_at_teardown.append(daemon.rover)
        await stop_consumers(tasks)

    monkeypatch.setattr(daemon, "_stop_consumers", recording)
    run_task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(lambda: daemon.rover is not None)
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
    assert seen_at_teardown == [None]
    with pytest.raises(RuntimeError):
        await daemon.set_ntrip_url(f"ntrip://127.0.0.1:{_free_port()}/LATE")
    assert daemon._ntrip_task is None


@bounded
async def test_a_url_change_racing_the_teardown_starts_no_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `PUT /api/rover/ntrip` already waiting for the old client to end when the rover tears
    down must not then start a new one behind the teardown's back."""
    monkeypatch.setenv("NTRIP_PASSWORD", "")
    daemon = Daemon(_rover_settings(tmp_path, replay_loop=True, nmea_tcp_port=-1))
    release = asyncio.Event()

    async def slow_to_cancel() -> None:  # stands in for a client that takes a while to hang up
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    run_task = asyncio.create_task(daemon.run())
    restart: asyncio.Task[None] | None = None
    try:
        await _wait_for(lambda: daemon.rover is not None)
        old = asyncio.create_task(slow_to_cancel(), name="ntrip-client")
        daemon._ntrip_task = old
        url = f"ntrip://127.0.0.1:{_free_port()}/RACE"
        restart = asyncio.create_task(daemon.set_ntrip_url(url))
        await _wait_for(old.cancelling)  # the URL change is now waiting for the old client
        daemon.stop.set()
        await _wait_for(lambda: daemon.rover is None)  # and so is the teardown
        release.set()
        with pytest.raises(RuntimeError):
            await restart
    finally:
        release.set()
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        if restart is not None and not restart.done():
            restart.cancel()
    assert daemon._ntrip_task is None
    left = [t for t in asyncio.all_tasks() if t.get_name() == "ntrip-client" and not t.done()]
    assert left == []


def test_no_gga_goes_to_the_caster_before_there_is_a_fix() -> None:
    """Phase 6: GGA is uploaded "when the receiver has a fix"; an empty quality-0 GGA can get
    an odd answer from a nearest-base or VRS caster."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from mtrtk.core.state import ReceiverState
    from mtrtk.daemon import Daemon

    s = ReceiverState()
    s.time.utc = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    s.position.lat, s.position.lon, s.position.height_m = 23.8, 90.2, -36.0
    s.fix.fix_type, s.fix.gnss_fix_ok = 3, False  # outside the receiver's masks
    owner = SimpleNamespace(store=SimpleNamespace(state=s))
    assert Daemon._gga_for_caster(owner) is None  # type: ignore[arg-type]
    s.fix.gnss_fix_ok = True
    gga = Daemon._gga_for_caster(owner)  # type: ignore[arg-type]
    assert gga is not None and gga.startswith(b"$GNGGA,100000.00,")
