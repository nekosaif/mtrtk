import asyncio
import time

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.link import LinkTimeout, UbxLink
from mtrtk.core.receiver import (
    BACKOFF_MAX_S,
    Capabilities,
    ProfileError,
    ReceiverController,
    ReceiverError,
)
from mtrtk.core.ubx_config import LAYERS_ALL, LAYERS_RAM, CfgItems, Profile, base_profile
from ubxtest import FakeReceiver, mon_ver_bytes, ubx_frame

MON_VER = (0x0A, 0x04)
CFG_VALGET = (0x06, 0x8B)
MON_SPAN_KEY = "CFG_MSGOUT_UBX_MON_SPAN_USB"
MON_COMMS_KEY = "CFG_MSGOUT_UBX_MON_COMMS_USB"
NAV_TIMELS_KEY = "CFG_MSGOUT_UBX_NAV_TIMELS_USB"


def polled(rx: FakeReceiver) -> list[tuple[int, int]]:
    """The (class, id) of every zero-payload UBX poll the controller wrote."""
    return [(w[2], w[3]) for w in rx.writes if len(w) == 8]


def drain(sub) -> list[tuple[str, object]]:
    return [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]


async def never_sleep(delay: float) -> None:
    """A backoff that costs no wall-clock time."""
    return None


def seed_profile(rx: FakeReceiver, profile: Profile) -> None:
    """Put the fake receiver exactly where the profile wants it."""
    rx.config = dict(profile.core)
    rx.config.update(profile.signals)
    for items in profile.optional.values():
        rx.config.update(items)


def stub_valset_timeouts(link: UbxLink, key: str, times: int) -> list[CfgItems]:
    """Make the first `times` VALSETs carrying `key` raise LinkTimeout; record every attempt.

    The timeout has to be injected at the link, not at the wire: `UbxLink.valset` retries a
    silent receiver three times at 2 s each, which no unit test can afford to wait out.
    """
    real = link.valset
    attempts: list[CfgItems] = []

    async def flaky(items: CfgItems, layers: int, **kwargs) -> bool:
        if any(k == key for k, _ in items):
            attempts.append(list(items))
            if len(attempts) <= times:
                raise LinkTimeout("no ACK for CFG-VALSET")
        return await real(items, layers, **kwargs)

    link.valset = flaky
    return attempts


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
    written = [items for _, items in rx.valsets if NAV_TIMELS_KEY in items]
    assert len(written) == 1  # a NAK is the firmware's final answer: it is not retried


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


async def test_run_reconnects_after_open_failure(settings: Settings) -> None:
    attempts = 0

    class Flaky:
        name = "flaky"
        ends_at_eof = True

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

    ctrl = ReceiverController(Bus(), Flaky, profile=None, passive=True, sleep=fake_sleep)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    assert attempts == 2 and sleeps[0] == 1.0


async def test_disconnect_event_survives_a_failing_close() -> None:
    """A yanked device makes close() raise - exactly when reconnect must still happen."""

    class BadClose:
        name = "badclose"
        ends_at_eof = True

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


async def test_unexpected_failure_reports_and_reconnects(settings: Settings) -> None:
    """A corrupt CFG-VALGET raises out of pyubx2; that must not end the supervisor."""
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

    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(bus, lambda: sources.pop(0), base_profile(settings), sleep=fake_sleep)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    topics = [t for t, _ in [events.queue.get_nowait() for _ in range(events.queue.qsize())]]
    assert "receiver.error" in topics
    assert topics.count("receiver.connected") == 2  # it reconnected instead of dying
    assert topics[-1] == "receiver.disconnected"
    assert sources == [] and sleeps == [1.0]


async def test_backoff_resets_once_a_session_is_established() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class Scripted:
        """Refuses to open twice, then connects and immediately drops the link."""

        name = "scripted"
        ends_at_eof = True  # the fourth attempt ends the run at EOF, like a replay file

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

    ctrl = ReceiverController(Bus(), Scripted, profile=None, passive=True, sleep=fake_sleep)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 2.0)
    # 1 s, 2 s while the port stays shut; the third attempt connects, so its later
    # failure starts the ladder over at BACKOFF_MIN_S instead of waiting 4 s.
    assert sleeps == [1.0, 2.0, 1.0]
    assert attempts == 4


async def test_backoff_sleep_returns_as_soon_as_stop_is_set() -> None:
    """Ctrl-C during a 30 s reconnect backoff must not hold the daemon for 30 s."""
    ctrl = ReceiverController(Bus(), lambda: FakeReceiver(Bus()), profile=None, passive=True)
    ctrl._backoff = BACKOFF_MAX_S
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.02, stop.set)
    started = time.monotonic()
    await asyncio.wait_for(ctrl._backoff_sleep(stop), 2.0)
    assert time.monotonic() - started < 1.0  # returned on stop, not after BACKOFF_MAX_S
    assert ctrl._backoff == BACKOFF_MAX_S  # the ladder still doubles and stays capped


async def test_probe_uses_the_profiles_optional_features(settings: Settings) -> None:
    """A profile may carry its own optional set; the module default is only the fallback."""
    bus = Bus()
    rx = FakeReceiver(bus)
    rx.config = {"CFG_MSGOUT_UBX_NAV_ODO_USB": 0}
    link = UbxLink(rx, bus)
    await link.start()
    profile = base_profile(settings)
    profile.optional = {"NAV-ODO": [("CFG_MSGOUT_UBX_NAV_ODO_USB", 1)]}
    caps = await ReceiverController(bus, lambda: rx, profile).probe(link)
    await link.stop()
    assert caps.supported == {"NAV-ODO"} and caps.unsupported == set()


async def test_core_is_not_rewritten_when_the_receiver_already_matches(env) -> None:
    """The first apply of a session writes FLASH: a matching receiver must not be written."""
    ctrl, link, rx, _ = env
    seed_profile(rx, ctrl.profile)
    await ctrl.configure(link, first=True)
    assert rx.valsets == []


async def test_core_writes_only_the_keys_that_differ(env) -> None:
    ctrl, link, rx, _ = env
    seed_profile(rx, ctrl.profile)
    rx.config["CFG_RATE_MEAS"] = 250  # one key drifted out of the profile
    await ctrl.configure(link, first=True)
    assert [items for _, items in rx.valsets] == [{"CFG_RATE_MEAS": 1000}]


async def test_optional_valset_timeout_is_retried_before_the_feature_is_demoted(env) -> None:
    ctrl, link, rx, _ = env
    rx.config = {MON_SPAN_KEY: 0, MON_COMMS_KEY: 0, NAV_TIMELS_KEY: 0}
    attempts = stub_valset_timeouts(link, MON_SPAN_KEY, times=1)
    caps = await ctrl.configure(link, first=True)
    assert len(attempts) == 2  # silence is not a refusal: the write is tried again
    assert "MON-SPAN" in caps.supported and rx.config[MON_SPAN_KEY] == 5


async def test_optional_valset_that_keeps_timing_out_is_demoted_after_one_retry(env) -> None:
    ctrl, link, rx, _ = env
    rx.config = {MON_SPAN_KEY: 0, MON_COMMS_KEY: 0, NAV_TIMELS_KEY: 0}
    attempts = stub_valset_timeouts(link, MON_SPAN_KEY, times=99)
    caps = await ctrl.configure(link, first=True)
    assert len(attempts) == 2  # exactly one retry, then it gives up
    assert "MON-SPAN" in caps.unsupported and "MON-SPAN" not in caps.supported


async def test_apply_items_writes_with_the_given_layers_and_reports_a_nak(env) -> None:
    ctrl, link, rx, _ = env
    with pytest.raises(ReceiverError, match="not connected"):
        await ctrl.apply_items([("CFG_TMODE_MODE", 1)])
    ctrl.link = link
    assert await ctrl.apply_items([("CFG_TMODE_MODE", 1)], LAYERS_RAM) is True
    assert rx.valsets[-1] == (LAYERS_RAM, {"CFG_TMODE_MODE": 1})
    rx.valset_nak_keys = {"CFG_TMODE_MODE"}
    assert await ctrl.apply_items([("CFG_TMODE_MODE", 0)]) is False
    assert rx.valsets[-1][0] == LAYERS_ALL  # LAYERS_ALL is the default


async def test_link_failure_publishes_receiver_error_and_reconnects(settings: Settings) -> None:
    """A write that fails mid-configure is a link failure: report it, then reconnect."""
    bus = Bus()

    class Unplugged(FakeReceiver):
        async def write(self, data: bytes) -> None:
            raise OSError("device disappeared")

    sources = [Unplugged(bus), FakeReceiver(bus)]
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(
        bus, lambda: sources.pop(0), base_profile(settings), sleep=never_sleep
    )
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 5.0)
    seen = drain(events)
    errors = [item for topic, item in seen if topic == "receiver.error"]
    assert errors and "device disappeared" in str(errors[0])
    assert [t for t, _ in seen].count("receiver.connected") == 2
    assert sources == []


async def test_stop_during_configure_returns_promptly(settings: Settings) -> None:
    """Ctrl-C against a receiver that never answers must not wait out the link timeouts."""
    bus = Bus()
    rx = FakeReceiver(bus)
    rx.silent = True  # every poll and VALGET runs to its 2 s deadline
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings))
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    started = time.monotonic()
    await asyncio.wait_for(ctrl.run(stop), 5.0)
    assert time.monotonic() - started < 1.0


async def test_strict_profile_error_ends_the_run(settings: Settings) -> None:
    """RECEIVER_STRICT=1: a rejected core key is a startup failure, not a reconnect loop."""
    bus = Bus()
    rx = FakeReceiver(bus)
    rx.valset_nak_keys = {"CFG_ITFM_ANTSETTING"}
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings), strict=True)
    with pytest.raises(ProfileError, match="CFG_ITFM_ANTSETTING"):
        await asyncio.wait_for(ctrl.run(asyncio.Event()), 5.0)


async def test_non_strict_core_nak_does_not_end_the_run(settings: Settings) -> None:
    bus = Bus()
    rx = FakeReceiver(bus)
    rx.valset_nak_keys = {"CFG_ITFM_ANTSETTING"}
    ctrl = ReceiverController(bus, lambda: rx, base_profile(settings), strict=False)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 5.0)  # warns, configures, then EOF
    assert rx.config["CFG_RATE_MEAS"] == 1000


async def test_eof_from_a_live_source_reconnects_instead_of_ending_the_run() -> None:
    """A yanked serial port just returns b"": that is a disconnect, not a clean end of stream."""
    opened: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class Vanishing:
        name = "vanishing"

        def __init__(self) -> None:
            opened.append(1)
            self.ends_at_eof = len(opened) > 1  # first a live device, then a file that ends

        async def open(self) -> None:
            return None

        async def read(self) -> bytes:
            return b""

        async def write(self, data: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    bus = Bus()
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(bus, Vanishing, profile=None, passive=True, sleep=fake_sleep)
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 5.0)
    seen = drain(events)
    reasons = [item for topic, item in seen if topic == "receiver.disconnected"]
    assert [t for t, _ in seen].count("receiver.connected") == 2
    assert reasons[0] == "eof" and reasons[1] == "source ended"
    assert sleeps == [1.0]


async def test_watchdog_reconnects_when_the_receiver_goes_quiet() -> None:
    """A receiver that stops sending without closing the port must still be noticed."""
    opened: list[int] = []
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    class GoesQuiet:
        """One chunk, then silence on a port that stays open. The retry ends at EOF."""

        name = "quiet"
        ends_at_eof = True

        def __init__(self) -> None:
            opened.append(1)
            self.first_session = len(opened) == 1
            self.chunks = 0

        async def open(self) -> None:
            return None

        async def read(self) -> bytes:
            if not self.first_session:
                return b""
            self.chunks += 1
            if self.chunks == 1:
                return mon_ver_bytes()
            await asyncio.sleep(3600)
            return b""

        async def write(self, data: bytes) -> None:
            return None

        async def close(self) -> None:
            return None

    bus = Bus()
    events = bus.subscribe("receiver.*")
    ctrl = ReceiverController(
        bus, GoesQuiet, profile=None, passive=True, rx_timeout_s=0.05, sleep=fake_sleep
    )
    await asyncio.wait_for(ctrl.run(asyncio.Event()), 5.0)
    seen = drain(events)
    reasons = [item for topic, item in seen if topic == "receiver.disconnected"]
    assert reasons[0] == "no data from receiver for 0.05s"
    assert [t for t, _ in seen].count("receiver.connected") == 2  # it reconnected
    assert sleeps == [1.0]
