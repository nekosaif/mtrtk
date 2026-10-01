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
from mtrtk.rover.drivers.sbg.ids import CMD
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

    async def reset(self) -> None:
        if self.raises is not None:
            raise self.raises
        self.calls.append("reset")


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
        assert names["output:EKF_NAV"] == "unsupported"  # the fake stores no output conf
        assert dev.sets == []  # read-only: nothing but GETs went to the unit
    finally:
        stop.set()
        await run
        await c.db.close()
