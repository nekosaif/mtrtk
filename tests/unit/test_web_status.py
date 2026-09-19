import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from webtest import client, load_fixture_into, make_ctx

from mtrtk.core.receiver import Capabilities
from mtrtk.store.models import SystemStats
from mtrtk.web.app import create_app


@pytest.fixture
async def ctx(tmp_path: Path):
    c = await make_ctx(tmp_path)
    load_fixture_into(c.store)
    try:
        yield c
    finally:
        await c.db.close()


def stats(**overrides) -> SystemStats:  # type: ignore[no-untyped-def]
    fields = {"cpu_pct": 1.5, "mem_pct": 2, "disk_free_gb": 3, "disk_used_pct": 4, "uptime_s": 5}
    return SystemStats(**{**fields, **overrides})


async def test_status_summary(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "base" and body["connected"] is False and body["version"]
    assert body["fix"]["fix_type_name"] == "3D" and body["fix"]["num_sv"] > 10
    assert 23 < body["position"]["lat"] < 24.5
    assert body["firmware"]["fw_version"] in ("", "HPG 1.13")  # MON-VER is only there if polled
    assert body["ntrip_clients"] == 0 and body["epoch_count"] >= 25
    assert set(body["survey_in"]) == {"active", "valid", "dur_s", "mean_acc_m"}


async def test_full_state(ctx) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/state")
    body = r.json()
    assert body["fix"]["fix_type"] == 3 and len(body["sats"]) > 20
    assert body["time"]["utc"].startswith("2026-")
    assert body["rtcm_out"]["total_count"] > 0


async def test_system_endpoint_reflects_latest_stats(ctx, monkeypatch) -> None:
    monkeypatch.setattr("mtrtk.web.api.system.tailscale_ipv4", lambda: "100.100.50.10")
    app = create_app(ctx)
    async with client(app) as c:
        r = await c.get("/api/system")
        assert r.status_code == 200
        assert r.json()["stats"] is None and r.json()["tailscale_ip"] == "100.100.50.10"
        ctx.bus.publish("system.stats", stats())
        await asyncio.sleep(0.01)
        r = await c.get("/api/system")
    assert r.json()["stats"]["cpu_pct"] == 1.5
    assert r.json()["versions"]["mtrtk"] and r.json()["versions"]["pyubx2"]


async def test_status_counts_ntrip_clients_and_the_ones_turned_away(ctx) -> None:
    """Ruling 3: a caster that refused callers has to say so next to the live count."""
    ctx.daemon.caster = SimpleNamespace(clients={1: object(), 4: object()}, rejected=7)
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/status")).json()
    assert body["ntrip_clients"] == 2 and body["ntrip_rejected"] == 7


async def test_status_reports_receiver_capabilities_sorted(ctx) -> None:
    ctx.daemon.controller = SimpleNamespace(
        connected=True,
        capabilities=Capabilities(supported={"nav-pvt", "esf-status"}, unsupported={"esf-ins"}),
    )
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/status")).json()
    assert body["connected"] is True
    assert body["capabilities"] == {
        "supported": ["esf-status", "nav-pvt"],
        "unsupported": ["esf-ins"],
    }


async def test_system_cache_is_created_and_released_by_the_app_lifespan(ctx) -> None:
    """Ruling 2: the cache is a lifespan resource, and ruling 1: shutdown *unsubscribes* it."""
    app = create_app(ctx)
    before = ctx.bus.subscriber_count
    assert getattr(app.state, "system_cache", None) is None  # nothing runs before startup

    async with app.router.lifespan_context(app):
        cache = app.state.system_cache
        assert cache is not None
        assert ctx.bus.subscriber_count == before + 1
        ctx.bus.publish("system.stats", stats(cpu_pct=9.25))
        await asyncio.sleep(0.01)
        assert cache.latest is not None and cache.latest.cpu_pct == 9.25

    # `sub.close()` alone would leave the subscription registered on the bus for ever.
    assert ctx.bus.subscriber_count == before
    ctx.bus.publish("system.stats", stats(cpu_pct=99.0))
    await asyncio.sleep(0.01)
    assert cache.latest.cpu_pct == 9.25  # the reader is gone, not merely idle


async def test_system_endpoint_is_gated_like_every_other_api_route(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, web_password="hunter2", web_bind="lan")
    try:
        async with client(create_app(c)) as http:
            for path in ("/api/status", "/api/state", "/api/system"):
                assert (await http.get(path)).status_code == 401, path
    finally:
        await c.db.close()
