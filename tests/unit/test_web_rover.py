"""Rover API: overview, NTRIP URL, sessions, survey points, collection and exports."""

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from webtest import client, make_ctx

import mtrtk.web.api.rover as rover_api
from mtrtk.core.state import ReceiverState
from mtrtk.rover.drivers.base import DriverCapabilities
from mtrtk.rover.ntrip_client import NtripClientStatus
from mtrtk.rover.points import PointCollector, PointsRepo
from mtrtk.rover.sessions import SessionsRepo
from mtrtk.web.api.config import mask_url_password, unmask_url_password
from mtrtk.web.app import create_app

URL = "ntrip://rover:pw@100.100.50.10:2101/MTRK"


def epoch(i: int) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = datetime(2026, 9, 18, 16, 0, i, tzinfo=UTC)
    s.position.lat, s.position.lon, s.position.height_m = 23.8373506, 90.2625502, -36.268
    s.fix.fix_type, s.fix.carr_soln, s.fix.gnss_fix_ok = 3, 2, True
    return s


def rover_services(c, **overrides):  # type: ignore[no-untyped-def]
    sessions, points = SessionsRepo(c.db), PointsRepo(c.db)
    collector = PointCollector(
        c.bus, c.store, points, sessions, default_epochs=2, default_fixed_only=True
    )
    urls: list[str] = []

    async def set_ntrip_url(url: str) -> None:
        urls.append(url)

    services = {
        "driver": SimpleNamespace(
            name="ublox", capabilities=DriverCapabilities(True, True, False, False, True, True)
        ),
        "ntrip_client": SimpleNamespace(
            status=NtripClientStatus(
                connected=True,
                host="base",
                port=2101,
                mountpoint="MTRK",
                version=2,
                bytes_received=5000,
                frames_injected=40,
            )
        ),
        "collector": collector,
        "sessions_repo": sessions,
        "points_repo": points,
        "nmea": SimpleNamespace(
            sinks=[SimpleNamespace(port=10110, client_count=1)], sentences={"GGA", "RMC"}
        ),
        "json_udp": None,
        "set_ntrip_url": set_ntrip_url,
        "urls": urls,
    }
    services.update(overrides)
    return SimpleNamespace(**services)


@pytest.fixture
async def ctx(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("ROLE=rover\n")
    c = await make_ctx(tmp_path, role="rover", mtrtk_env_file=env)
    c.daemon.rover = rover_services(c)
    try:
        yield c
    finally:
        c.daemon.rover.collector.stop()
        await c.db.close()


async def test_get_rover_overview(ctx) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/rover")).json()
    assert body["role"] == "rover" and body["driver"]["name"] == "ublox"
    assert body["driver"]["capabilities"]["accepts_rtcm"] is True
    assert body["ntrip"]["connected"] is True and body["ntrip"]["frames_injected"] == 40
    assert body["outputs"]["nmea_tcp"] == {"port": 10110, "clients": 1}
    assert sorted(body["outputs"]["sentences"]) == ["GGA", "RMC"]
    assert body["outputs"]["json_udp"] is None and body["outputs"]["nmea_udp"] == []
    assert body["collect"]["state"] == "idle" and body["session"] is None
    assert body["rtk"]["carr_soln"] == 0 and body["ntrip_url"] is None


async def test_overview_without_ntrip_client_or_nmea(ctx) -> None:
    ctx.daemon.rover = rover_services(ctx, ntrip_client=None, nmea=None)
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/rover")).json()
    assert body["ntrip"] is None
    assert body["outputs"]["nmea_tcp"] is None and body["outputs"]["sentences"] == []


async def test_overview_masks_the_ntrip_password(ctx) -> None:
    ctx.settings.ntrip_url = URL
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/rover")).json()
    assert body["ntrip_url"] == "ntrip://rover:***@100.100.50.10:2101/MTRK"
    assert "pw@" not in str(body)


async def test_put_ntrip_url_validates_persists_and_applies(ctx) -> None:
    async with client(create_app(ctx)) as c:
        bad = await c.put("/api/rover/ntrip", json={"url": "nonsense"})
        assert bad.status_code == 422
        ok = await c.put("/api/rover/ntrip", json={"url": URL})
    assert ok.status_code == 200 and ctx.daemon.rover.urls == [URL]
    assert ok.json()["url"] == "ntrip://rover:***@100.100.50.10:2101/MTRK"
    assert f"NTRIP_URL={URL}" in ctx.settings.mtrtk_env_file.read_text()
    assert ctx.settings.ntrip_url == URL


async def test_put_ntrip_url_keeps_a_masked_password(ctx) -> None:
    """A form posting back the URL it was shown keeps the stored password, not `***`."""
    ctx.settings.ntrip_url = URL
    masked = "ntrip://rover:***@100.100.50.20:2101/MTRK"
    async with client(create_app(ctx)) as c:
        ok = await c.put("/api/rover/ntrip", json={"url": masked})
    assert ok.status_code == 200
    assert ctx.daemon.rover.urls == ["ntrip://rover:pw@100.100.50.20:2101/MTRK"]
    assert "***" not in ctx.settings.mtrtk_env_file.read_text()


async def test_overview_derives_the_ntrip_ages(ctx) -> None:
    """`*_mono` is this process's clock: the browser gets ages, clamped at zero."""
    now = time.monotonic()
    status = ctx.daemon.rover.ntrip_client.status
    status.since_mono, status.last_rtcm_mono = now - 30, now - 2
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/rover")).json()
    assert 29 <= body["ntrip"]["connected_for_s"] < 60
    assert 1 <= body["ntrip"]["last_rtcm_age_s"] < 30
    status.last_rtcm_mono = time.monotonic() + 100  # a clock read in the other order
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/rover")).json()
    assert body["ntrip"]["last_rtcm_age_s"] == 0.0


def test_url_masking_reads_a_schemeless_url_like_the_client_does() -> None:
    """`NtripClientConfig.from_url` takes `user:pass@host/MP` as `ntrip://user:pass@host/MP`."""
    assert mask_url_password("rover:pw@100.100.50.10:2101/MTRK") == (
        "rover:***@100.100.50.10:2101/MTRK"
    )
    assert unmask_url_password("rover:***@h:2101/MTRK", URL) == "rover:pw@h:2101/MTRK"
    assert unmask_url_password("rover:***@h:2101/MTRK", "rover:s3@x/MP") == "rover:s3@h:2101/MTRK"
    assert mask_url_password("host:2101/MTRK") == "host:2101/MTRK"


async def test_put_schemeless_ntrip_url_is_stored_canonical_and_masked(ctx) -> None:
    schemeless = "rover:pw@100.100.50.10:2101/MTRK"
    async with client(create_app(ctx)) as c:
        ok = await c.put("/api/rover/ntrip", json={"url": schemeless})
        shown = (await c.get("/api/rover")).json()["ntrip_url"]
        # The form posts back what it was shown: the stored password survives, not `***`.
        again = await c.put("/api/rover/ntrip", json={"url": "rover:***@100.100.50.20:2101/MTRK"})
    assert ok.status_code == 200 and ok.json()["url"] == "ntrip://rover:***@100.100.50.10:2101/MTRK"
    assert shown == "ntrip://rover:***@100.100.50.10:2101/MTRK"
    assert again.status_code == 200
    assert ctx.daemon.rover.urls == [URL, "ntrip://rover:pw@100.100.50.20:2101/MTRK"]
    env = ctx.settings.mtrtk_env_file.read_text()
    assert "NTRIP_URL=ntrip://rover:pw@100.100.50.20:2101/MTRK" in env and "***" not in env


async def test_put_ntrip_url_refuses_a_mask_with_nothing_stored(ctx) -> None:
    """`***` with no stored password is not a password to keep: refused, not made anonymous."""
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/rover/ntrip", json={"url": "ntrip://rover:***@host:2101/MTRK"})
        ctx.settings.ntrip_url = "ntrip://rover@host:2101/MTRK"  # a user, but no password
        r2 = await c.put("/api/rover/ntrip", json={"url": "ntrip://rover:***@host:2101/MTRK"})
    assert r.status_code == 422 and "masked" in r.json()["detail"]
    assert r2.status_code == 422
    assert ctx.daemon.rover.urls == []
    assert "NTRIP_URL" not in ctx.settings.mtrtk_env_file.read_text()


@pytest.mark.parametrize(
    "url",
    [
        "ntrip://rover:s3cret@host:notaport/MTRK",
        "ntrip://rover:s3cret/x@host/MTRK",  # an unencoded `/` ends the netloc mid-password
        "ftp://rover:s3cret@host/MTRK",
    ],
)
async def test_put_ntrip_url_422_never_echoes_the_password(ctx, url: str) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/rover/ntrip", json={"url": url})
    assert r.status_code == 422 and ctx.daemon.rover.urls == []
    assert "s3cret" not in r.text and r.json()["detail"].startswith("url: ")


async def test_put_ntrip_url_reports_a_restart_that_failed(ctx) -> None:
    """Saved, but not applied: the response says so and the running settings stay on the URL
    the client is still using, so a retry is a real change rather than a no-op."""
    ctx.settings.ntrip_url = URL

    async def broken(url: str) -> None:
        raise RuntimeError("client would not stop")

    ctx.daemon.rover = rover_services(ctx, set_ntrip_url=broken)
    new = "ntrip://rover:pw@100.100.50.30:2101/MTRK"
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/rover/ntrip", json={"url": new})
    assert r.status_code == 502
    assert "saved" in r.json()["detail"] and "RuntimeError" in r.json()["detail"]
    assert "pw" not in r.json()["detail"]
    assert f"NTRIP_URL={new}" in ctx.settings.mtrtk_env_file.read_text()
    assert ctx.settings.ntrip_url == URL


async def test_export_says_when_it_was_truncated(ctx, monkeypatch) -> None:
    monkeypatch.setattr(rover_api, "EXPORT_LIMIT", 1)
    async with client(create_app(ctx)) as c:
        for i, name in enumerate(("OLDEST", "NEWEST")):
            await c.post("/api/rover/collect", json={"name": name, "epochs": 1})
            await ctx.daemon.rover.collector.on_epoch(epoch(i))
        r = await c.get("/api/rover/points/export")
    assert r.status_code == 200 and r.headers["x-truncated"] == "1"
    assert "NEWEST" in r.text and "OLDEST" not in r.text


async def test_put_ntrip_url_refuses_what_env_cannot_hold(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/rover/ntrip", json={"url": "ntrip://u:${HOME}@host:2101/MTRK"})
    assert r.status_code == 422 and ctx.daemon.rover.urls == []
    assert "NTRIP_URL" not in ctx.settings.mtrtk_env_file.read_text()


async def test_sessions_and_collect_flow(ctx) -> None:
    async with client(create_app(ctx)) as c:
        s = await c.post("/api/rover/sessions", json={"name": "field-1", "notes": "test"})
        assert s.status_code == 200 and s.json()["name"] == "field-1"
        assert s.json()["role"] == "rover"
        assert (await c.get("/api/rover")).json()["session"]["id"] == s.json()["id"]
        body = {"name": "BM-1", "code": "BM", "epochs": 2, "fixed_only": True}
        r = await c.post("/api/rover/collect", json=body)
        assert r.status_code == 200 and r.json()["state"] == "collecting"
        assert r.json()["target"] == 2
        assert (await c.post("/api/rover/collect", json={"name": "again"})).status_code == 409
        await ctx.daemon.rover.collector.on_epoch(epoch(0))
        await ctx.daemon.rover.collector.on_epoch(epoch(1))
        assert (await c.get("/api/rover/collect")).json()["state"] == "done"
        points = (await c.get("/api/rover/points")).json()
        assert len(points) == 1 and points[0]["name"] == "BM-1"
        assert points[0]["session_id"] == s.json()["id"]
        pid = points[0]["id"]
        upd = await c.patch(f"/api/rover/points/{pid}", json={"note": "brass disk"})
        assert upd.json()["note"] == "brass disk" and upd.json()["code"] == "BM"
        csv_r = await c.get("/api/rover/points/export", params={"fmt": "csv"})
        assert csv_r.status_code == 200 and csv_r.text.startswith("id,name,code")
        assert "BM-1" in csv_r.text and "attachment" in csv_r.headers["content-disposition"]
        gj = await c.get(
            "/api/rover/points/export", params={"fmt": "geojson", "session_id": s.json()["id"]}
        )
        assert gj.json()["features"][0]["properties"]["name"] == "BM-1"
        for fmt in ("kml", "gpx"):
            x = await c.get("/api/rover/points/export", params={"fmt": fmt})
            assert x.status_code == 200 and "BM-1" in x.text and x.text.startswith("<?xml")
        # What desktop GIS tools key on: the media type and the download name's extension.
        for fmt, mime, ext in (
            ("csv", "text/csv", "csv"),
            ("geojson", "application/geo+json", "geojson"),
            ("kml", "application/vnd.google-earth.kml+xml", "kml"),
            ("gpx", "application/gpx+xml", "gpx"),
        ):
            x = await c.get("/api/rover/points/export", params={"fmt": fmt})
            assert x.headers["content-type"].startswith(mime), fmt
            assert x.headers["content-disposition"].endswith(f'-points.{ext}"'), fmt
            assert "x-truncated" not in x.headers
        bad = await c.get("/api/rover/points/export", params={"fmt": "xlsx"})
        assert bad.status_code == 422
        sessions = (await c.get("/api/rover/sessions")).json()
        assert [x["name"] for x in sessions] == ["field-1"]
        stop = await c.post("/api/rover/sessions/stop")
        assert stop.json()["end_utc"] is not None
        assert (await c.post("/api/rover/sessions/stop")).json() is None
        assert (await c.delete(f"/api/rover/points/{pid}")).json() == {"ok": True}
        assert (await c.get("/api/rover/points")).json() == []
        assert (await c.delete(f"/api/rover/points/{pid}")).status_code == 404


async def test_points_filter_by_session(ctx) -> None:
    async with client(create_app(ctx)) as c:
        s1 = (await c.post("/api/rover/sessions", json={"name": "a"})).json()
        await c.post("/api/rover/collect", json={"name": "P1", "epochs": 1})
        await ctx.daemon.rover.collector.on_epoch(epoch(0))
        s2 = (await c.post("/api/rover/sessions", json={"name": "b"})).json()
        await c.post("/api/rover/collect", json={"name": "P2", "epochs": 1})
        await ctx.daemon.rover.collector.on_epoch(epoch(1))
        only_1 = (await c.get("/api/rover/points", params={"session_id": s1["id"]})).json()
        everything = (await c.get("/api/rover/points")).json()
        export = await c.get("/api/rover/points/export", params={"session_id": s2["id"]})
    assert [p["name"] for p in only_1] == ["P1"]
    assert [p["name"] for p in everything] == ["P2", "P1"]  # newest first
    assert "P2" in export.text and "P1" not in export.text
    assert f"session-{s2['id']}" in export.headers["content-disposition"]


async def test_export_is_oldest_first(ctx) -> None:
    async with client(create_app(ctx)) as c:
        for i, name in enumerate(("FIRST", "SECOND")):
            await c.post("/api/rover/collect", json={"name": name, "epochs": 1})
            await ctx.daemon.rover.collector.on_epoch(epoch(i))
        text = (await c.get("/api/rover/points/export")).text
    assert text.index("FIRST") < text.index("SECOND")


async def test_point_edits_refuse_bad_input(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.patch("/api/rover/points/999", json={"note": "x"})).status_code == 404
        await c.post("/api/rover/collect", json={"name": "P", "epochs": 1})
        await ctx.daemon.rover.collector.on_epoch(epoch(0))
        pid = (await c.get("/api/rover/points")).json()[0]["id"]
        blank = await c.patch(f"/api/rover/points/{pid}", json={"name": "  "})
        assert blank.status_code == 422
        assert (await c.get("/api/rover/points")).json()[0]["name"] == "P"


async def test_collect_refuses_bad_parameters(ctx) -> None:
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/rover/collect", json={"name": " "})).status_code == 422
        too_many = await c.post("/api/rover/collect", json={"name": "x", "epochs": 100000})
        assert too_many.status_code == 422
        assert (await c.get("/api/rover/collect")).json()["state"] == "idle"


async def test_cancel_collect(ctx) -> None:
    async with client(create_app(ctx)) as c:
        await c.post("/api/rover/collect", json={"name": "x", "epochs": 5})
        r = await c.delete("/api/rover/collect")
    assert r.json()["state"] == "aborted" and r.json()["reason"] == "cancelled"


async def test_rover_endpoints_409_for_base_role(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/rover")).status_code == 409
            assert (await c.post("/api/rover/collect", json={"name": "x"})).status_code == 409
            assert (await c.put("/api/rover/ntrip", json={"url": URL})).status_code == 409
            assert (await c.get("/api/rover/points/export")).status_code == 409
    finally:
        await ctx.db.close()


async def test_sessions_are_stamped_from_receiver_utc_not_the_host_clock(ctx) -> None:
    """Raw-log hours and points use receiver UTC, so the session window that PPK reads must too:
    a fake-hwclock Pi with no network keeps a host clock far from the receiver's."""
    receiver_utc = datetime(2026, 9, 18, 21, 54, 43, tzinfo=UTC)  # host says 2026-10-xx
    s = ctx.store.state
    s.connected = True
    s.time.utc, s.time.valid_date, s.time.valid_time = receiver_utc, True, True
    s.last_epoch_mono = time.monotonic()
    async with client(create_app(ctx)) as c:
        started = (await c.post("/api/rover/sessions", json={"name": "f"})).json()
        start = datetime.fromisoformat(started["start_utc"])
        assert abs((start - receiver_utc).total_seconds()) < 2
        s.time.utc = receiver_utc.replace(minute=59)
        s.last_epoch_mono = time.monotonic()
        stopped = (await c.post("/api/rover/sessions/stop")).json()
        end = datetime.fromisoformat(stopped["end_utc"])
        assert abs((end - receiver_utc.replace(minute=59)).total_seconds()) < 2


async def test_sessions_fall_back_to_the_host_clock_without_valid_receiver_time(ctx) -> None:
    s = ctx.store.state
    s.connected = True
    s.time.utc = datetime(2026, 9, 18, 21, 54, 43, tzinfo=UTC)
    s.time.valid_date, s.time.valid_time = False, False  # not vouched for
    s.last_epoch_mono = time.monotonic()
    async with client(create_app(ctx)) as c:
        started = (await c.post("/api/rover/sessions", json={"name": "f"})).json()
    start = datetime.fromisoformat(started["start_utc"])
    assert abs((start - datetime.now(UTC)).total_seconds()) < 5
