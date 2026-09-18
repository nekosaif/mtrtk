import asyncio

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.link import UbxLink
from mtrtk.core.receiver import Capabilities, ProfileError, ReceiverController
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM, base_profile
from ubxtest import FakeReceiver, mon_ver_bytes, ubx_frame

MON_VER = (0x0A, 0x04)
CFG_VALGET = (0x06, 0x8B)
MON_SPAN_KEY = "CFG_MSGOUT_UBX_MON_SPAN_USB"
MON_COMMS_KEY = "CFG_MSGOUT_UBX_MON_COMMS_USB"
NAV_TIMELS_KEY = "CFG_MSGOUT_UBX_NAV_TIMELS_USB"


def polled(rx: FakeReceiver) -> list[tuple[int, int]]:
    """The (class, id) of every zero-payload UBX poll the controller wrote."""
    return [(w[2], w[3]) for w in rx.writes if len(w) == 8]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("NTRIP_PASSWORD", "x")
    return Settings(_env_file=None)


@pytest.fixture
async def env(settings: Settings):
    bus = Bus()
    rx = FakeReceiver(bus)
    link = UbxLink(rx, bus)
    await link.start()
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings))
    try:
        yield ctrl, link, rx, bus
    finally:
        await link.stop()


async def test_probe_reads_firmware_and_optional_support(env) -> None:
    ctrl, link, rx, _ = env
    # A key the firmware does not have is NAK'd by VALGET; the others read back.
    rx.config = {MON_COMMS_KEY: 5, NAV_TIMELS_KEY: 10}
    caps = await ctrl.probe(link)
    assert caps.protver == "27.12" and caps.fw_version == "HPG 1.13" and caps.module == "ZED-F9P"
    assert caps.unsupported == {"MON-SPAN"}
    assert caps.supported == {"MON-COMMS", "NAV-TIMELS"}
    # Feature detection must not poll the messages: a periodic frame would answer the poll.
    assert polled(rx) == [MON_VER]


async def test_probe_leaves_the_receiver_untouched(env) -> None:
    ctrl, link, rx, _ = env
    rx.config = {MON_COMMS_KEY: 5}
    await ctrl.probe(link)
    assert rx.valsets == [] and rx.config == {MON_COMMS_KEY: 5}


async def test_configure_applies_core_signals_optional_and_verifies(env) -> None:
    ctrl, link, rx, bus = env
    # MON-SPAN's key is unknown to this firmware; the other two read back.
    rx.config = {MON_COMMS_KEY: 0, NAV_TIMELS_KEY: 0}
    caps_sub = bus.subscribe("receiver.capabilities")
    caps = await ctrl.configure(link, first=True)
    assert rx.config["CFG_RATE_MEAS"] == 1000 and rx.config["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"] == 1
    assert rx.config["CFG_SIGNAL_SBAS_ENA"] == 0
    assert MON_SPAN_KEY not in rx.config  # skipped: probe said unsupported
    assert rx.config[MON_COMMS_KEY] == 5
    assert all(layers == LAYERS_ALL for layers, _ in rx.valsets)
    assert caps_sub.queue.qsize() == 1
    assert isinstance(caps, Capabilities)


async def test_reconnect_uses_ram_layer_only(env) -> None:
    ctrl, link, rx, _ = env
    await ctrl.configure(link, first=False)
    assert all(layers == LAYERS_RAM for layers, _ in rx.valsets)


async def test_signals_not_rewritten_when_already_correct(env) -> None:
    ctrl, link, rx, _ = env
    await ctrl.configure(link, first=True)
    n_before = len(rx.valsets)
    await ctrl.configure(link, first=False)
    signal_writes = [items for _, items in rx.valsets[n_before:] if "CFG_SIGNAL_GPS_ENA" in items]
    assert signal_writes == []


async def test_optional_nak_disables_feature_not_startup(env) -> None:
    ctrl, link, rx, _ = env
    # Both keys exist (the probe finds them), but writing NAV-TIMELS' is refused.
    rx.config = {MON_SPAN_KEY: 0, NAV_TIMELS_KEY: 0}
    rx.valset_nak_keys = {NAV_TIMELS_KEY}
    caps = await ctrl.configure(link, first=True)
    assert "NAV-TIMELS" in caps.unsupported and "MON-SPAN" in caps.supported


async def test_core_nak_raises_profile_error_naming_the_key(env) -> None:
    ctrl, link, rx, _ = env
    rx.valset_nak_keys = {"CFG_ITFM_ANTSETTING"}
    with pytest.raises(ProfileError, match="CFG_ITFM_ANTSETTING"):
        await ctrl.configure(link, first=True)


async def test_core_nak_is_a_warning_when_not_strict(settings: Settings) -> None:
    bus = Bus()
    rx = FakeReceiver(bus)
    rx.valset_nak_keys = {"CFG_ITFM_ANTSETTING"}
    link = UbxLink(rx, bus)
    await link.start()
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings), strict=False)
    await ctrl.configure(link, first=True)
    assert rx.config["CFG_RATE_MEAS"] == 1000
    await link.stop()


async def test_verify_reports_mismatch(env) -> None:
    ctrl, link, rx, _ = env
    profile = base_profile(Settings(_env_file=None, ntrip_password="x"))
    await ctrl.configure(link, first=True)
    rx.config["CFG_RATE_MEAS"] = 250
    assert await ctrl.verify(link, profile) == {"CFG_RATE_MEAS": (1000, 250)}


async def test_run_ends_when_replay_source_ends(settings: Settings, tmp_path) -> None:
    from pyubx2 import GET, UBXMessage

    from mtrtk.core.source import FileReplaySource

    path = tmp_path / "r.ubx"
    path.write_bytes(UBXMessage("NAV", "NAV-PVT", GET, iTOW=1).serialize() + mon_ver_bytes())
    bus = Bus()
    pvt_sub = bus.subscribe("ubx.NAV-PVT")
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(
        bus, lambda: FileReplaySource(path, speed=0), profile=None, passive=True
    )
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    assert pvt_sub.queue.qsize() == 1
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics[0] == "receiver.connected" and topics[-1] == "receiver.disconnected"


async def test_run_reconnects_after_open_failure(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mtrtk.core import receiver as receiver_mod

    attempts = 0

    class Flaky:
        name = "flaky"

        async def open(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("device busy")

        async def read(self) -> bytes:
            return b""

        async def write(self, data: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(receiver_mod.asyncio, "sleep", fake_sleep)
    ctrl = ReceiverController(Bus(), Flaky, profile=None, passive=True)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    assert attempts == 2 and sleeps[0] == 1.0


async def test_disconnect_event_survives_a_failing_close() -> None:
    """A yanked device makes close() raise - exactly when reconnect must still happen."""

    class BadClose:
        name = "badclose"

        async def open(self) -> None:
            return None

        async def read(self) -> bytes:
            return b""

        async def write(self, data: bytes) -> None:
            return None

        async def close(self) -> None:
            raise OSError("device disappeared")

    bus = Bus()
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(bus, BadClose, profile=None, passive=True)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert topics == ["receiver.connected", "receiver.disconnected"]
    assert ctrl.connected is False and ctrl.link is None


async def test_unexpected_failure_reports_and_reconnects(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt CFG-VALGET raises out of pyubx2; that must not end the supervisor."""
    from mtrtk.core import receiver as receiver_mod

    bus = Bus()

    class GarbledValget(FakeReceiver):
        async def write(self, data: bytes) -> None:
            if (data[2], data[3]) == CFG_VALGET:
                self.writes.append(data)
                # checksum-valid CFG-VALGET carrying an unknown key id: pyubx2 raises
                self.inject(ubx_frame(*CFG_VALGET, b"\x01\x00\x00\x00\xff\xff\xff\xff\x2a"))
                return
            await super().write(data)

    sources = [GarbledValget(bus), FakeReceiver(bus)]
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(receiver_mod.asyncio, "sleep", fake_sleep)
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(bus, lambda: sources.pop(0), base_profile(settings))
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert "receiver.error" in topics
    assert topics.count("receiver.connected") == 2  # it reconnected instead of dying
    assert topics[-1] == "receiver.disconnected"
    assert sources == [] and sleeps == [1.0]


async def test_backoff_resets_once_a_session_is_established(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mtrtk.core import receiver as receiver_mod

    attempts = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class Scripted:
        """Refuses to open twice, then connects and immediately drops the link."""

        name = "scripted"

        def __init__(self) -> None:
            self.attempt = 0

        async def open(self) -> None:
            nonlocal attempts
            attempts += 1
            self.attempt = attempts
            if attempts <= 2:
                raise OSError("device busy")

        async def read(self) -> bytes:
            if self.attempt == 3:
                raise OSError("device disappeared")
            return b""  # fourth attempt: EOF ends run()

        async def write(self, data: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(receiver_mod.asyncio, "sleep", fake_sleep)
    ctrl = ReceiverController(Bus(), Scripted, profile=None, passive=True)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    # 1 s, 2 s while the port stays shut; the third attempt connects, so its later
    # failure starts the ladder over at BACKOFF_MIN_S instead of waiting 4 s.
    assert sleeps == [1.0, 2.0, 1.0]
    assert attempts == 4
