"""The web API with an INS rover driver: status, receiver introspection and the INS actions."""

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from webtest import client, make_ctx

from mtrtk.core.bus import Bus
from mtrtk.core.state import Attitude, ImuSample, InsStatus, ReceiverState
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.drivers.factory import StoreFacade, build_ins
from mtrtk.rover.drivers.sbg.commands import REBOOT_ONLY, encode_output_conf
from mtrtk.rover.drivers.sbg.ids import CLASS, CMD, LOG
from mtrtk.web.app import create_app
from mtrtk.web.ws import epoch_message
from sbgdevice import FakeEllipse

VN_CAPS = DriverCapabilities(
    accepts_rtcm=False, raw_gnss_log=True, attitude=True, imu=True, sats=True, spectrum=False
)
REPORT = {"items": [{"name": "binary_output_1", "state": "pending"}], "pending": ["x"]}


class FakeIns:
    """What the API reads of an `InsBundle`."""

    vendor = "vectornav"

    def __init__(self, state: ReceiverState) -> None:
        self.controller = SimpleNamespace(connected=True)
        self.driver = SimpleNamespace(
            name="vectornav", capabilities=VN_CAPS, rtcm_unverified=False, dropped_bytes=0
        )
        self.adapter = SimpleNamespace(state=state)
        self.calls: list[str] = []
        self.raises: Exception | None = None
        self.reset_confirmed = True

    @property
    def connected(self) -> bool:
        return bool(self.controller.connected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "info": {"model": "VN-200T-CR", "serial": "0100012345", "firmware": "2.0.0.0"},
            "config_report": REPORT,
            "status": None,
        }

    def report_dict(self) -> dict[str, Any]:
        return REPORT

    async def configure(self, *, apply: bool) -> object:
        if self.raises is not None:
            raise self.raises
        self.calls.append(f"configure:{apply}")
        return object()

    async def reset(self) -> bool:
        if self.raises is not None:
            raise self.raises
        self.calls.append("reset")
        return self.reset_confirmed


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = await make_ctx(tmp_path, role="rover", rover_driver="vectornav", ins_port="/dev/ttyUSB9")
    c.daemon.ins = FakeIns(c.store.state)
    try:
        yield c
    finally:
        await c.db.close()


async def test_status_names_the_driver(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/status")).json()
    assert body["driver"]["name"] == "vectornav"
    assert body["driver"]["capabilities"]["accepts_rtcm"] is False
    assert body["connected"] is True and body["source"] == "/dev/ttyUSB9"


async def test_status_of_a_ublox_daemon_names_the_ublox_driver(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path)
    try:
        async with client(create_app(c)) as http:
            body = (await http.get("/api/status")).json()
        assert body["driver"]["name"] == "ublox"
        assert body["driver"]["capabilities"]["spectrum"] is True
    finally:
        await c.db.close()


async def test_receiver_carries_the_ins_block(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/receiver")).json()
    assert body["ins"]["vendor"] == "vectornav"
    assert body["ins"]["info"]["model"] == "VN-200T-CR"
    assert body["driver"]["capabilities"]["spectrum"] is False
    assert body["capabilities"] is None  # no u-blox capabilities to report
    assert body["connected"] is True and body["passive"] is False


async def test_ins_reset(ctx) -> None:  # type: ignore[no-untyped-def]
    events = ctx.bus.subscribe("receiver.reset")
    async with client(create_app(ctx)) as c:
        factory = await c.post("/api/receiver/reset", json={"kind": "factory"})
        warm = await c.post("/api/receiver/reset", json={"kind": "warm"})
        ctx.daemon.ins.controller.connected = False
        offline = await c.post("/api/receiver/reset", json={"kind": "hot"})
    assert factory.status_code == 409 and "factory" in factory.json()["detail"]
    assert warm.json() == {"ok": True, "kind": "warm"}
    assert offline.status_code == 409
    assert ctx.daemon.ins.calls == ["reset"]
    assert events.queue.get_nowait() == ("receiver.reset", {"kind": "warm"})


async def test_ins_reset_failures_map_to_status_codes(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        ctx.daemon.ins.raises = TimeoutError("no reply to $VNRST")
        timeout = await c.post("/api/receiver/reset", json={"kind": "warm"})
        ctx.daemon.ins.raises = ConnectionError("INS not connected")
        dropped = await c.post("/api/receiver/reset", json={"kind": "warm"})
    assert timeout.status_code == 504 and "VNRST" in timeout.json()["detail"]
    assert dropped.status_code == 409


async def test_ins_reset_whose_reply_the_reboot_ate_is_reported_sent(ctx) -> None:  # type: ignore[no-untyped-def]
    """SBG REBOOT_ONLY with no ACK: the unit is restarting, so the UI shows it restarting."""
    events = ctx.bus.subscribe("receiver.reset")
    ctx.daemon.ins.reset_confirmed = False
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/receiver/reset", json={"kind": "hot"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "kind": "hot", "unconfirmed": True}
    assert events.queue.get_nowait() == ("receiver.reset", {"kind": "hot"})


async def test_profile_failures_map_to_status_codes(ctx) -> None:  # type: ignore[no-untyped-def]
    from mtrtk.rover.drivers.sbg.commands import SbgCommandError
    from mtrtk.rover.drivers.sbg.ids import CMD as SBG_CMD

    async with client(create_app(ctx)) as c:
        ctx.daemon.ins.raises = TimeoutError("no reply")
        timeout = await c.post("/api/receiver/profile", json={"force": True})
        ctx.daemon.ins.raises = SbgCommandError(SBG_CMD["INFO"], None)  # a NACK-less timeout
        unanswered = await c.post("/api/receiver/profile", json={"apply": False})
        ctx.daemon.ins.raises = SbgCommandError(SBG_CMD["INFO"], 9)  # a refusal
        refused = await c.post("/api/receiver/profile", json={"apply": False})
        ctx.daemon.ins.raises = None
        ctx.daemon.ins.controller.connected = False
        offline = await c.post("/api/receiver/profile", json={"force": True})
    assert timeout.status_code == 504 and unanswered.status_code == 504
    assert refused.status_code == 409
    assert offline.status_code == 409 and "connected" in offline.json()["detail"]
    assert ctx.daemon.ins.calls == []  # nothing reached a disconnected unit


async def test_profile_apply_needs_apply_config_or_force(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        refused = await c.post("/api/receiver/profile", json={})
        forced = await c.post("/api/receiver/profile", json={"force": True})
        reread = await c.post("/api/receiver/profile", json={"apply": False})
        ctx.settings.ins_apply_config = True
        allowed = await c.post("/api/receiver/profile", json={})
    assert refused.status_code == 409 and "force" in refused.json()["detail"]
    assert forced.status_code == 200 and forced.json()["report"] == REPORT
    assert forced.json()["applied"] is True
    assert reread.json()["applied"] is False
    assert allowed.status_code == 200
    assert ctx.daemon.ins.calls == ["configure:True", "configure:False", "configure:True"]


async def test_poll_rereads_and_reapply_points_at_profile(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        polled = await c.post("/api/receiver/poll", json={"msg_class": "INS", "msg_id": "CONFIG"})
        reapply = await c.post("/api/receiver/reapply")
    assert polled.json()["report"] == REPORT
    assert reapply.status_code == 409 and "/api/receiver/profile" in reapply.json()["detail"]
    assert ctx.daemon.ins.calls == ["configure:False"]


async def test_profile_on_a_ublox_daemon_is_409(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path)
    try:
        async with client(create_app(c)) as http:
            r = await http.post("/api/receiver/profile", json={"force": True})
        assert r.status_code == 409 and "reapply" in r.json()["detail"]
    finally:
        await c.db.close()


async def test_set_ntrip_refused_when_the_driver_takes_no_rtcm(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.rover = SimpleNamespace(driver=ctx.daemon.ins.driver)
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/rover/ntrip", json={"url": "ntrip://u:p@caster:2101/MTRK"})
    assert r.status_code == 409 and "corrections not supported" in r.json()["detail"]
    assert ctx.settings.ntrip_url is None


def test_epoch_bundle_carries_ins_imu_and_attitude() -> None:
    state = ReceiverState(
        ins=InsStatus(vendor="sbg", mode=4, mode_name="Nav position"),
        imu=ImuSample(temperature_c=38.0),
        attitude=Attitude(heading_deg=12.5),
    )
    msg = epoch_message(state, {"ins"})
    assert msg["ins"]["ins"]["mode_name"] == "Nav position"
    assert msg["ins"]["imu"]["temperature_c"] == 38.0
    assert msg["ins"]["attitude"]["heading_deg"] == 12.5
    assert "ins" not in epoch_message(state, {"pvt"})


def info_payload() -> bytes:
    return b"ELLIPSE-D-G4A3-B1".ljust(32, b"\0") + struct.pack(
        "<IIHBBII", 12345, 3, 2025, 6, 1, 0x02000000, 0x03010000
    )


async def test_real_sbg_bundle_through_the_api(tmp_path: Path) -> None:
    """A real `InsBundle` on a scripted Ellipse: the on-connect read fills `ins`, a re-read
    goes through `configure(apply=False)` and writes nothing but GETs."""
    c = await make_ctx(tmp_path, role="rover", rover_driver="sbg_ellipse", ins_port="/dev/x")
    bus: Bus = c.bus
    dev = FakeEllipse()
    dev.put(CMD["INFO"], info_payload())
    # EKF_NAV is off on Port A: the profile wants it, so an apply that leaked would write it.
    dev.put(CMD["OUTPUT_CONF"], encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 0))
    reports = bus.subscribe("ins.config")
    bundle = build_ins(c.settings, bus, source_factory=lambda: dev, capture=False)
    c.store = StoreFacade(bundle.adapter)
    c.daemon.ins = bundle
    stop = asyncio.Event()
    run = asyncio.create_task(bundle.controller.run(stop))
    try:
        await asyncio.wait_for(reports.queue.get(), 10)  # the on-connect read finished
        async with client(create_app(c)) as http:
            body = (await http.get("/api/receiver")).json()
            reread = (await http.post("/api/receiver/profile", json={"apply": False})).json()
        assert body["ins"]["vendor"] == "sbg"
        assert body["ins"]["info"]["model"] == "ELLIPSE-D-G4A3-B1"
        assert body["ins"]["info"]["serial"] == "12345"
        assert body["ins"]["connected"] is True
        names = {i["name"]: i["state"] for i in reread["report"]["items"]}
        assert names["output:EKF_NAV"] == "pending"  # differs, and INS_APPLY_CONFIG=0
        assert "output:EKF_NAV" in body["ins"]["config_report"]["pending"]
        assert dev.sets == []  # read-only: nothing but GETs went to the unit
    finally:
        stop.set()
        await run
        await c.db.close()


async def _run_bundle(bundle, reports):  # type: ignore[no-untyped-def]
    stop = asyncio.Event()
    run = asyncio.create_task(bundle.controller.run(stop))
    await asyncio.wait_for(reports.queue.get(), 10)  # the on-connect configure finished
    return stop, run


async def test_on_connect_applies_only_with_ins_apply_config(tmp_path: Path) -> None:
    """INS_APPLY_CONFIG=1: the on-connect pass writes the differing output and reads it back."""
    c = await make_ctx(
        tmp_path, role="rover", rover_driver="sbg_ellipse", ins_port="/dev/x", ins_apply_config=True
    )
    dev = FakeEllipse()
    dev.put(CMD["INFO"], info_payload())
    dev.put(CMD["OUTPUT_CONF"], encode_output_conf(0, CLASS["LOG_ECOM_0"], LOG["EKF_NAV"], 0))
    reports = c.bus.subscribe("ins.config")
    bundle = build_ins(c.settings, c.bus, source_factory=lambda: dev, capture=False)
    stop, run = await _run_bundle(bundle, reports)
    try:
        assert CMD["OUTPUT_CONF"] in [cmd for cmd, _ in dev.sets]
        assert "output:EKF_NAV" in bundle.driver.config_report.applied  # type: ignore[union-attr]
    finally:
        stop.set()
        await run
        await c.db.close()


async def test_real_sbg_bundle_reset_sends_reboot_only(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, role="rover", rover_driver="sbg_ellipse", ins_port="/dev/x")
    dev = FakeEllipse()
    dev.put(CMD["INFO"], info_payload())
    reports = c.bus.subscribe("ins.config")
    bundle = build_ins(c.settings, c.bus, source_factory=lambda: dev, capture=False)
    stop, run = await _run_bundle(bundle, reports)
    try:
        assert await bundle.reset() is True
        assert dev.sets == [(CMD["SETTINGS_ACTION"], bytes([REBOOT_ONLY]))]
        dev.ack_lost.add(CMD["SETTINGS_ACTION"])  # the reboot swallows the ACK
        assert await bundle.reset() is False
        assert dev.set_payloads(CMD["SETTINGS_ACTION"]) == [bytes([REBOOT_ONLY])] * 2  # no resend
    finally:
        stop.set()
        await run
        await c.db.close()


async def _vn_bundle(tmp_path: Path, **kw: Any):  # type: ignore[no-untyped-def]
    from vectornav.device import VnDevice

    c = await make_ctx(tmp_path, role="rover", rover_driver="vectornav", ins_port="/dev/x", **kw)
    dev = VnDevice()
    reports = c.bus.subscribe("ins.config")
    bundle = build_ins(c.settings, c.bus, source_factory=lambda: dev, capture=False)
    c.store = StoreFacade(bundle.adapter)
    c.daemon.ins = bundle
    stop, run = await _run_bundle(bundle, reports)
    return c, dev, bundle, stop, run


async def test_real_vn_bundle_reset_sends_one_rst(tmp_path: Path) -> None:
    from mtrtk.rover.drivers.vectornav.checksum import finalize_ascii

    c, dev, bundle, stop, run = await _vn_bundle(tmp_path)
    try:
        dev.written.clear()
        assert await bundle.reset() is True
        assert dev.written[0] == finalize_ascii("VNRST")
        assert dev.commands[-1] == "VNRST"
        dev.hooks.append(lambda cmd, args: "" if cmd == "VNRST" else None)  # reply lost
        before = dev.commands.count("VNRST")
        with pytest.raises(TimeoutError):
            await bundle.reset()
        assert dev.commands.count("VNRST") == before + 1  # sent once, never resent
    finally:
        stop.set()
        await run
        await c.db.close()


async def test_forced_vn_apply_without_ins_apply_config_is_not_saved(tmp_path: Path) -> None:
    """The confirm dialog says a forced apply with INS_APPLY_CONFIG=0 is not saved to flash:
    on a VN-200 that means no `$VNWNV`."""
    c, dev, bundle, stop, run = await _vn_bundle(tmp_path)
    try:
        assert dev.commands.count("VNWNV") == 0 and not any(
            cmd.startswith("VNWRG") for cmd in dev.commands
        )  # on connect: read-only
        async with client(create_app(c)) as http:
            r = await http.post("/api/receiver/profile", json={"apply": True, "force": True})
            block = (await http.get("/api/receiver")).json()["ins"]
        assert r.status_code == 200
        assert "binary_output_1" in r.json()["report"]["applied"]
        assert r.json()["report"]["saved"] is False
        assert dev.commands.count("VNWNV") == 0
        assert block["saved_this_run"] is False
        info = block["info"]
        assert (info["model"], info["serial"], info["firmware"], info["hardware"]) == (
            "VN-200T-CR",
            "0100012345",
            "2.0.0.0",
            "4",
        )
    finally:
        stop.set()
        await run
        await c.db.close()


async def test_lever_arms_show_what_the_unit_read_back(tmp_path: Path) -> None:
    c, dev, bundle, stop, run = await _vn_bundle(tmp_path, ins_lever_arm_gnss1="0.1,0.2,-1.0")
    try:
        dev.regs[57] = ["+0.500", "-0.250", "+1.000"]
        await bundle.configure(apply=False)
        assert bundle.lever_arms() == [
            {"name": "gnss1", "configured": [0.1, 0.2, -1.0], "read_back": [0.5, -0.25, 1.0]}
        ]
    finally:
        stop.set()
        await run
        await c.db.close()


async def test_sbg_lever_arms_and_identity_from_the_unit(tmp_path: Path) -> None:
    from mtrtk.rover.drivers.sbg.commands import (
        GnssInstallation,
        ImuAlignment,
        encode_gnss_installation,
        encode_imu_alignment,
    )

    c = await make_ctx(
        tmp_path,
        role="rover",
        rover_driver="sbg_ellipse",
        ins_port="/dev/x",
        ins_lever_arm_gnss1="0.1,0.2,-0.3",
    )
    dev = FakeEllipse()
    dev.put(CMD["INFO"], info_payload())
    inst = GnssInstallation((1.0, 2.0, 3.0), True, (4.0, 5.0, 6.0), 1)
    dev.put(CMD["GNSS_1_INSTALLATION"], encode_gnss_installation(inst))
    dev.put(
        CMD["IMU_ALIGNMENT_LEVER_ARM"],
        encode_imu_alignment(ImuAlignment(0, 2, 0.0, 0.0, 0.0, (7.0, 8.0, 9.0))),
    )
    reports = c.bus.subscribe("ins.config")
    bundle = build_ins(c.settings, c.bus, source_factory=lambda: dev, capture=False)
    stop, run = await _run_bundle(bundle, reports)
    try:
        arms = {a["name"]: (a["configured"], a["read_back"]) for a in bundle.lever_arms()}
        assert arms["gnss1"] == ([0.1, 0.2, -0.3], [1.0, 2.0, 3.0])
        assert arms["gnss2"] == (None, [4.0, 5.0, 6.0])
        assert arms["imu"] == (None, [7.0, 8.0, 9.0])
        info = bundle.info_dict()
        assert info is not None
        assert (info["model"], info["serial"]) == ("ELLIPSE-D-G4A3-B1", "12345")
        assert info["firmware"] and info["hardware"]
        assert dev.sets == []
    finally:
        stop.set()
        await run
        await c.db.close()


async def test_healthz_reports_the_ins_link(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        up = (await c.get("/healthz")).json()
        ctx.daemon.ins.controller.connected = False
        down = (await c.get("/healthz")).json()
    assert up["connected"] is True and up["passive"] is False
    assert down["connected"] is False and down["status"] == "ok"


async def test_an_ins_replay_is_passive_and_refuses_unit_actions(tmp_path: Path) -> None:
    """A replay swallows every write: /api/receiver says so, and the actions answer 409."""
    replay = "file:tests/fixtures/ins/sbg_frames.bin"
    c = await make_ctx(tmp_path, role="rover", rover_driver="sbg_ellipse", mtrtk_source=replay)
    c.daemon.ins = FakeIns(c.store.state)
    try:
        async with client(create_app(c)) as http:
            body = (await http.get("/api/receiver")).json()
            status = (await http.get("/api/status")).json()
            reset = await http.post("/api/receiver/reset", json={"kind": "warm"})
            poll = await http.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "VER"})
            apply = await http.post("/api/receiver/profile", json={"apply": True, "force": True})
        assert body["passive"] is True and body["source"] == replay == status["source"]
        for refused in (reset, poll, apply):
            assert refused.status_code == 409 and "passive" in refused.json()["detail"]
        assert c.daemon.ins.calls == []
    finally:
        await c.db.close()
