"""Startup and address robustness found on the real Pi (2026-10-03).

Bug A: a tailnet address change after boot left the web UI and the caster listening on the old,
dead address. Bug B: `MTRTK_SOURCE=auto` with no receiver plugged in exited at startup instead
of serving the UI. Both are reproduced with fakes: a fake tailscale0 address provider, a fake
USB scanner and a pseudo-terminal standing in for the receiver. No real device is opened.
"""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from mtrtk import daemon as daemon_mod
from mtrtk.config import Settings
from mtrtk.core import exposure
from mtrtk.core import receiver as receiver_mod
from mtrtk.core.source import NO_UBLOX_RECEIVER, NoReceiverSource
from mtrtk.daemon import Daemon

OLD_IP = "127.0.0.1"  # stand-ins for 100.93.95.104 and 100.100.10.100: both loopback on Linux
NEW_IP = "127.0.0.2"
# All of 127.0.0.0/8 is loopback on Linux; macOS has only 127.0.0.1 unless an alias is added.
needs_second_loopback = pytest.mark.skipif(
    sys.platform != "linux", reason="needs 127.0.0.2 on the loopback interface"
)


class FakeTailnet:
    """`tailscale_ipv4` with an answer the test moves, as tailscaled does on a new netmap."""

    def __init__(self, address: str | None) -> None:
        self.address = address

    def __call__(self) -> str | None:
        return self.address


class HeldOpen:
    """A live receiver that sends nothing: the session stays up until it is closed."""

    name = "held"
    ends_at_eof = False

    def __init__(self) -> None:
        self._closed = asyncio.Event()

    async def open(self) -> None:
        return None

    async def read(self) -> bytes:
        await self._closed.wait()
        return b""

    async def write(self, data: bytes) -> None:
        return None

    async def close(self) -> None:
        self._closed.set()


async def _wait_for(predicate: Callable[[], object], timeout_s: float = 5.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


def _drain(sub: Any) -> list[Any]:
    return [sub.queue.get_nowait()[1] for _ in range(sub.queue.qsize())]


async def _ntrip_stream(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    auth = base64.b64encode(b"rover:pw").decode()
    reader, writer = await asyncio.open_connection(host, port)
    request = f"GET /MTRK HTTP/1.0\r\nUser-Agent: NTRIP test\r\nAuthorization: Basic {auth}\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    assert await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0) == b"ICY 200 OK\r\n\r\n"
    return reader, writer


async def _healthz(host: str, port: int) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(f"http://{host}:{port}/healthz", timeout=2.0)


# --------------------------------------------------------------------- Bug A


@needs_second_loopback
async def test_web_and_caster_follow_a_tailnet_address_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tailnet = FakeTailnet(OLD_IP)
    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(
        _env_file=None,
        role="base",
        data_dir=tmp_path,
        web_bind="tailscale",
        web_port=0,
        ntrip_bind="tailscale",
        ntrip_port=0,
        ntrip_user="rover",
    )
    daemon = Daemon(settings, source_factory=HeldOpen, passive=True)
    daemon.rebind_check_s = 0.02
    events = daemon.bus.subscribe("events.new")
    seen_hosts: set[str] = set()

    def bound() -> bool:
        web, caster = daemon.web, daemon.caster
        for host in (getattr(web, "host", None), getattr(caster, "host", None)):
            if host is not None:
                seen_hosts.add(host)
        return web is not None and web.started.is_set() and caster is not None

    task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(bound)
        assert daemon.web is not None and daemon.caster is not None
        assert (daemon.web.host, daemon.caster.host) == (OLD_IP, OLD_IP)
        old_web_port, old_caster_port = daemon.web.port, daemon.caster.port
        assert (await _healthz(OLD_IP, old_web_port)).status_code == 200
        # A rebind builds a fresh app and caster: the old ones' bus subscribers must go with them.
        subscribers = daemon.bus.subscriber_count
        old_reader, old_writer = await _ntrip_stream(OLD_IP, old_caster_port)

        # tailscale0 briefly without an address: nothing to move to, and never 0.0.0.0.
        tailnet.address = None
        await asyncio.sleep(0.1)
        bound()
        assert (daemon.web.host, daemon.caster.host) == (OLD_IP, OLD_IP)

        tailnet.address = NEW_IP
        await _wait_for(
            lambda: (
                bound()
                and daemon.web is not None
                and daemon.web.host == NEW_IP
                and daemon.caster is not None
                and daemon.caster.host == NEW_IP
            )
        )
        assert exposure.BIND_ANY not in seen_hosts
        assert seen_hosts == {OLD_IP, NEW_IP}

        assert daemon.web is not None and daemon.caster is not None
        response = await _healthz(NEW_IP, daemon.web.port)
        assert response.status_code == 200 and response.json()["status"] == "ok"
        # New rovers connect on the new address ...
        new_reader, new_writer = await _ntrip_stream(NEW_IP, daemon.caster.port)
        new_writer.close()
        # ... and one still on the old one is hung up rather than left on a dead socket.
        await asyncio.wait_for(old_reader.read(), 5.0)  # reads to EOF
        assert old_reader.at_eof()
        old_writer.close()
        with pytest.raises(OSError):
            await asyncio.open_connection(OLD_IP, old_caster_port)
        await _wait_for(lambda: daemon.bus.subscriber_count == subscribers)

        moved = [e for e in _drain(events) if e.kind == "bind_changed"]
        assert len(moved) == 2, [e.message for e in moved]
        assert all(e.level == "info" for e in moved)
        assert all(OLD_IP in e.message and NEW_IP in e.message for e in moved)
        assert {"web UI" in e.message for e in moved} == {True, False}
    finally:
        daemon._request_stop("test")
        await asyncio.wait_for(task, 30.0)
    assert daemon.error is None


async def test_a_fixed_bind_is_never_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WEB_BIND/NTRIP_BIND set to an address: a tailnet change is not theirs to follow."""
    tailnet = FakeTailnet(NEW_IP)
    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(_env_file=None, role="base", data_dir=tmp_path)  # 127.0.0.1 (conftest)
    daemon = Daemon(settings, source_factory=HeldOpen, passive=True)
    daemon.rebind_check_s = 0.02
    task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(
            lambda: daemon.web is not None and daemon.web.started.is_set() and daemon.caster
        )
        web, caster = daemon.web, daemon.caster
        await asyncio.sleep(0.15)
        assert daemon.web is web and daemon.caster is caster
        assert web is not None and web.host == OLD_IP
    finally:
        daemon._request_stop("test")
        await asyncio.wait_for(task, 30.0)


async def test_a_failing_rebind_watcher_restarts_the_server_rather_than_ending_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watcher that raises (anything but the OSError it skips) must not end the web UI for
    good with nothing logged: the supervisor reports it and serves again."""
    reads = 0

    def tailnet() -> str | None:
        nonlocal reads
        reads += 1
        if reads == 2:  # the watcher's first check
            raise RuntimeError("getifaddrs blew up")
        return OLD_IP

    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    monkeypatch.setattr(daemon_mod, "SUPERVISE_BACKOFF_START_S", 0.02)
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(
        _env_file=None, role="base", data_dir=tmp_path, web_bind="tailscale", web_port=0
    )
    daemon = Daemon(settings, source_factory=HeldOpen, passive=True)
    daemon.rebind_check_s = 0.02
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(lambda: daemon.web is not None and daemon.web.started.is_set())
        first = daemon.web
        await _wait_for(lambda: failures.queue.qsize() > 0)
        failure = _drain(failures)[0]
        assert failure["name"] == "web" and "RuntimeError" in failure["error"]
        await _wait_for(
            lambda: (
                daemon.web is not None and daemon.web is not first and daemon.web.started.is_set()
            )
        )
        assert daemon.web is not None and daemon.web.host == OLD_IP
        assert (await _healthz(OLD_IP, daemon.web.port)).status_code == 200
        assert not daemon.stop.is_set()
    finally:
        daemon._request_stop("test")
        await asyncio.wait_for(task, 30.0)
    assert daemon.error is None


async def test_a_rebind_noted_while_shutdown_begins_starts_no_new_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final fix wave (2026-10-03): stop set during the bind_changed write (one SQLite insert)
    must end the loop there, not build a fresh app, bind the new address and run its lifespan
    only to shut it down again."""

    async def bind_now(mode: str, stop: asyncio.Event) -> str:
        return OLD_IP

    monkeypatch.setattr(daemon_mod, "wait_for_bind", bind_now)
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(_env_file=None, role="base", data_dir=tmp_path)
    daemon = Daemon(settings, source_factory=HeldOpen, passive=True)

    async def moved(mode: str, host: str, until: asyncio.Event) -> str:
        until.set()
        return NEW_IP

    async def note_while_stopping(level: str, kind: str, message: str) -> None:
        daemon.stop.set()  # shutdown begins while the event is being written

    monkeypatch.setattr(daemon, "_watch_bind", moved)
    monkeypatch.setattr(daemon, "_note_event", note_while_stopping)
    served: list[str] = []

    async def serve(host: str, until: asyncio.Event) -> None:
        served.append(host)
        await until.wait()

    await asyncio.wait_for(daemon._serve_on_bind("web UI", "tailscale", serve), 2.0)
    assert served == [OLD_IP]


def _held_port_on_new_ip() -> socket.socket:
    """A listener on NEW_IP:P, P being a port OLD_IP has free: the moved-to address is taken."""
    for _ in range(50):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind((NEW_IP, 0))
        blocker.listen(1)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind((OLD_IP, blocker.getsockname()[1]))
        except OSError:
            blocker.close()
            continue
        finally:
            probe.close()
        return blocker
    raise AssertionError("no port free on both loopback addresses")


@needs_second_loopback
async def test_a_rebind_whose_new_address_is_taken_keeps_trying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new address cannot be bound yet (EADDRINUSE): the supervisor reports it, backs off
    and binds there once it is free, with no subscriber left behind by the failed tries."""
    tailnet = FakeTailnet(OLD_IP)
    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    monkeypatch.setattr(daemon_mod, "SUPERVISE_BACKOFF_START_S", 0.02)
    monkeypatch.setattr(daemon_mod, "SUPERVISE_BACKOFF_MAX_S", 0.05)
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    blocker = _held_port_on_new_ip()
    port = blocker.getsockname()[1]
    settings = Settings(
        _env_file=None, role="base", data_dir=tmp_path, web_bind="tailscale", web_port=port
    )
    daemon = Daemon(settings, source_factory=HeldOpen, passive=True)
    daemon.rebind_check_s = 0.02
    failures = daemon.bus.subscribe("daemon.consumer_failed")
    events = daemon.bus.subscribe("events.new")
    task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(lambda: daemon.web is not None and daemon.web.started.is_set())
        subscribers = daemon.bus.subscriber_count
        tailnet.address = NEW_IP
        await _wait_for(lambda: failures.queue.qsize() >= 2)  # tried, backed off, tried again
        assert all(f["name"] == "web" for f in _drain(failures))
        assert daemon.web is None or not daemon.web.started.is_set()
        moved = [e for e in _drain(events) if e.kind == "bind_changed"]
        # Said before the new bind is tried, so it must not claim a listener that never came up.
        assert len(moved) == 1 and "listens on the new" not in moved[0].message
        blocker.close()
        await _wait_for(lambda: daemon.web is not None and daemon.web.started.is_set())
        assert daemon.web is not None and (daemon.web.host, daemon.web.port) == (NEW_IP, port)
        assert (await _healthz(NEW_IP, port)).status_code == 200
        assert daemon.bus.subscriber_count == subscribers
    finally:
        blocker.close()
        daemon._request_stop("test")
        await asyncio.wait_for(task, 30.0)
    assert daemon.error is None


# --------------------------------------------------------------------- Bug B


async def test_auto_with_no_receiver_serves_and_starts_the_receiver_once_it_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanned: dict[str, str | None] = {"port": None}
    scans = 0

    def scanner() -> str | None:
        nonlocal scans
        scans += 1
        return scanned["port"]

    monkeypatch.setattr("mtrtk.daemon.find_ublox_port", scanner)
    monkeypatch.setattr(receiver_mod, "BACKOFF_MIN_S", 0.02)
    monkeypatch.setattr(receiver_mod, "BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(NoReceiverSource, "retry_s", 0.02)  # SCAN_RETRY_S, at test speed
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(
        _env_file=None, role="base", data_dir=tmp_path, mtrtk_source="auto", ntrip_user="rover"
    )
    # Passive: the pty below is no F9P, and a profile apply would wait for answers it never gets.
    daemon = Daemon(settings, passive=True)  # used to raise "no u-blox receiver found"
    events = daemon.bus.subscribe("events.new")
    downs = daemon.bus.subscribe("receiver.disconnected")
    master, slave = os.openpty()
    task = asyncio.create_task(daemon.run())
    try:
        await _wait_for(
            lambda: daemon.web is not None and daemon.web.started.is_set() and daemon.caster
        )
        assert daemon.web is not None and daemon.caster is not None
        response = await _healthz(daemon.web.host, daemon.web.port)
        assert response.status_code == 200
        assert response.json()["status"] == "ok" and response.json()["connected"] is False
        reader, writer = await _ntrip_stream(daemon.caster.host, daemon.caster.port)
        writer.close()

        seen: list[Any] = []
        await _wait_for(
            lambda: (
                seen.extend(_drain(events)) or any(e.kind == "receiver_disconnected" for e in seen)
            )
        )
        down = [e for e in seen if e.kind == "receiver_disconnected"]
        assert len(down) == 1 and NO_UBLOX_RECEIVER in down[0].message
        before = scans
        await _wait_for(lambda: scans >= before + 3)  # it keeps scanning
        assert daemon.store.state.connected is False

        port = os.ttyname(slave)
        scanned["port"] = port
        await _wait_for(lambda: daemon.store.state.connected)
        assert daemon.store.state.source == f"serial:{port}"
        await _wait_for(
            lambda: (
                seen.extend(_drain(events))
                or any(e.kind == "receiver_disconnected_cleared" for e in seen)
            )
        )
        # One report for the whole outage, however many scans it took: from the controller
        # (the AlertEngine would also fold repeats into one active alert, so check both).
        assert [e.kind for e in seen].count("receiver_disconnected") == 1
        assert _drain(downs) == [f"cannot open auto (USB scan): {NO_UBLOX_RECEIVER}"]
        response = await _healthz(daemon.web.host, daemon.web.port)
        assert response.status_code == 200 and response.json()["connected"] is True
    finally:
        daemon._request_stop("test")
        await asyncio.wait_for(task, 30.0)
        os.close(master)
        os.close(slave)
    assert daemon.error is None
