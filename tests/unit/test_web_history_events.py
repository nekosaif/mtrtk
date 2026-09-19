"""Chart history over the 1 s samples and the 1 m rollups, and the event log with its ack."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webtest import client, make_ctx

from mtrtk.core.state import ReceiverState
from mtrtk.store.repos import EventsRepo
from mtrtk.store.sampler import SAMPLE_COLUMNS, Sampler
from mtrtk.web.app import create_app
from mtrtk.web.context import AppContext

T0 = datetime(2026, 9, 18, 16, 0, tzinfo=UTC)


def state_at(t: datetime, h_acc: float) -> ReceiverState:
    s = ReceiverState()
    s.time.utc = t
    s.accuracy.h_acc_m = h_acc
    s.sat_summary.used = 12
    s.fix.fix_type = 3
    return s


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[AppContext]:
    c = await make_ctx(tmp_path)
    sampler = Sampler(c.bus, c.db)
    for i in range(120):
        await sampler.insert(
            sampler.sample_row(state_at(T0 + timedelta(seconds=i), 1.0 + i / 100), None, 0)
        )
    await sampler.rollup_minute(T0.timestamp())
    await sampler.rollup_minute(T0.timestamp() + 60)
    # The seeding sampler is a writer, not the API's reader: release its bus subscription so the
    # subscriber-count assertions below are about the app and nothing else.
    sampler.stop()
    try:
        yield c
    finally:
        await c.db.close()


# --------------------------------------------------------------------------------- history


async def test_history_1s(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m,nsat_used",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(seconds=10)).isoformat(),
                "res": "1s",
            },
        )
    body = r.json()
    assert (
        r.status_code == 200
        and body["res"] == "1s"
        and body["columns"] == ["ts", "h_acc_m", "nsat_used"]
    )
    assert len(body["rows"]) == 10 and body["rows"][0][1] == 1.0 and body["rows"][0][2] == 12


async def test_history_1m_maps_avg_and_auto_res(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m,h_acc_max,fix_type_min",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(hours=7)).isoformat(),
            },
        )
    body = r.json()
    assert body["res"] == "1m" and body["columns"] == [
        "ts",
        "h_acc_avg",
        "h_acc_max",
        "fix_type_min",
    ]
    # Both to a tolerance: the seeded maximum is `1.0 + 59 / 100`, which is 1.5899999999999999,
    # so `== 1.59` would be asserting against a value the fixture never wrote.
    assert (
        len(body["rows"]) == 2
        and abs(body["rows"][0][1] - 1.295) < 1e-9
        and abs(body["rows"][0][2] - 1.59) < 1e-9
    )


async def test_history_validation(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        assert (
            await c.get(
                "/api/history",
                params={"metrics": "nope", "from": T0.isoformat(), "to": T0.isoformat()},
            )
        ).status_code == 422
        assert (
            await c.get(
                "/api/history",
                params={"metrics": "h_acc_m", "from": "yesterday", "to": T0.isoformat()},
            )
        ).status_code == 422
        metrics = (await c.get("/api/history/metrics")).json()
    assert "h_acc_m" in metrics["1s"] and "h_acc_avg" in metrics["1m"] and "ts" not in metrics["1s"]


async def test_auto_resolution_turns_over_at_six_hours(ctx: AppContext) -> None:
    """Six hours of 1 s rows is 21 600 points; the seventh hour is where the minutes take over."""
    async with client(create_app(ctx)) as c:
        six = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(hours=6)).isoformat(),
            },
        )
        over = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(hours=6, seconds=1)).isoformat(),
            },
        )
    assert six.json()["res"] == "1s" and six.json()["columns"] == ["ts", "h_acc_m"]
    assert over.json()["res"] == "1m" and over.json()["columns"] == ["ts", "h_acc_avg"]


async def test_window_is_capped_per_resolution(ctx: AppContext) -> None:
    """A mistyped year must be refused, not answered: 1 s is capped at the day the sampler keeps."""
    async with client(create_app(ctx)) as c:
        day = {"metrics": "h_acc_m", "from": T0.isoformat(), "res": "1s"}
        assert (
            await c.get(
                "/api/history", params={**day, "to": (T0 + timedelta(hours=24)).isoformat()}
            )
        ).status_code == 200
        long_1s = await c.get(
            "/api/history", params={**day, "to": (T0 + timedelta(hours=24, seconds=1)).isoformat()}
        )
        assert long_1s.status_code == 422 and "24 hours" in long_1s.json()["detail"]
        minutes = {"metrics": "h_acc_m", "from": T0.isoformat(), "res": "1m"}
        assert (
            await c.get(
                "/api/history", params={**minutes, "to": (T0 + timedelta(days=90)).isoformat()}
            )
        ).status_code == 200
        long_1m = await c.get(
            "/api/history", params={**minutes, "to": (T0 + timedelta(days=91)).isoformat()}
        )
        assert long_1m.status_code == 422 and "90 days" in long_1m.json()["detail"]
        # `res=auto` is capped by the resolution it picked, not by the one that was asked for.
        assert (
            await c.get(
                "/api/history",
                params={
                    "metrics": "h_acc_m",
                    "from": T0.isoformat(),
                    "to": (T0 + timedelta(days=91)).isoformat(),
                },
            )
        ).status_code == 422


async def test_rows_never_exceed_the_windows_natural_count(ctx: AppContext) -> None:
    """One second holds one sample. A row no writer could have made cannot widen the answer."""
    sampler = Sampler(ctx.bus, ctx.db)
    row = sampler.sample_row(state_at(T0, 5.0), None, 0)
    assert row is not None
    await sampler.insert({**row, "ts": T0.timestamp() + 0.5})
    sampler.stop()
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(seconds=1)).isoformat(),
                "res": "1s",
            },
        )
    assert r.json()["rows"] == [[T0.timestamp(), 1.0]]


async def test_from_must_precede_to(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        same = await c.get(
            "/api/history",
            params={"metrics": "h_acc_m", "from": T0.isoformat(), "to": T0.isoformat()},
        )
        backwards = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m",
                "from": T0.isoformat(),
                "to": (T0 - timedelta(hours=1)).isoformat(),
            },
        )
        naive = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m",
                "from": T0.isoformat()[:-6],
                "to": (T0 + timedelta(hours=1)).isoformat(),
            },
        )
    assert same.status_code == 422 and backwards.status_code == 422
    assert naive.status_code == 422 and "timezone" in naive.json()["detail"]


async def test_unknown_metrics_name_the_allowed_columns(ctx: AppContext) -> None:
    """A 1 s name with no rollup is unknown at `1m`, and the 422 says what may be asked for."""
    async with client(create_app(ctx)) as c:
        one_s = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m,nope",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(minutes=1)).isoformat(),
                "res": "1s",
            },
        )
        one_m = await c.get(
            "/api/history",
            params={
                "metrics": "hdop",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(hours=7)).isoformat(),
            },
        )
        empty = await c.get(
            "/api/history",
            params={
                "metrics": " , ",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(minutes=1)).isoformat(),
            },
        )
        bad_res = await c.get(
            "/api/history",
            params={
                "metrics": "h_acc_m",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(minutes=1)).isoformat(),
                "res": "5m",
            },
        )
    assert (
        one_s.status_code == 422
        and "nope" in one_s.json()["detail"]
        and "h_acc_m" in one_s.json()["detail"]
    )
    assert (
        one_m.status_code == 422
        and "hdop" in one_m.json()["detail"]
        and "h_acc_avg" in one_m.json()["detail"]
    )
    assert empty.status_code == 422 and bad_res.status_code == 422


async def test_a_metric_asked_for_twice_is_one_column(ctx: AppContext) -> None:
    """`ts` is always the first column, so asking for it again must not duplicate it either."""
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/history",
            params={
                "metrics": "ts,h_acc_m,h_acc_m,nsat_used",
                "from": T0.isoformat(),
                "to": (T0 + timedelta(seconds=3)).isoformat(),
                "res": "1s",
            },
        )
    body = r.json()
    assert body["columns"] == ["ts", "h_acc_m", "nsat_used"] and len(body["rows"][0]) == 3


async def test_metrics_lists_both_tables(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/history/metrics")).json()
    assert body["1s"] == [name for name in SAMPLE_COLUMNS if name != "ts"]
    assert "ts" not in body["1m"] and "n" in body["1m"] and "nsat_used_min" in body["1m"]


async def test_history_never_subscribes_to_the_bus(ctx: AppContext) -> None:
    """A chart refresh is a read. `Sampler` subscribes in its constructor; the reader does not."""
    async with client(create_app(ctx)) as c:
        before = ctx.bus.subscriber_count
        for _ in range(3):
            r = await c.get(
                "/api/history",
                params={
                    "metrics": "h_acc_m",
                    "from": T0.isoformat(),
                    "to": (T0 + timedelta(seconds=5)).isoformat(),
                },
            )
            assert r.status_code == 200
        assert (await c.get("/api/history/metrics")).status_code == 200
        assert ctx.bus.subscriber_count == before


# ---------------------------------------------------------------------------------- events


async def test_events_list_and_ack(ctx: AppContext) -> None:
    repo = EventsRepo(ctx.db)
    e = await repo.add("warning", "jamming", "jam_ind 210", {"jam_ind": 210})
    await repo.add("info", "survey_in_valid", "done")
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/events")).json()
        assert [x["kind"] for x in body] == ["survey_in_valid", "jamming"] and body[1]["meta"] == {
            "jam_ind": 210
        }
        assert [
            x["kind"] for x in (await c.get("/api/events", params={"level": "warning"})).json()
        ] == ["jamming"]
        assert (await c.post(f"/api/events/{e.id}/ack")).json() == {"ok": True}
        assert (await c.get("/api/events", params={"level": "warning"})).json()[0]["acked"] is True


async def test_acking_an_unknown_event_is_404(ctx: AppContext) -> None:
    """Silence would let a UI report success for an id that retention deleted a year ago."""
    e = await EventsRepo(ctx.db).add("error", "gnss_lost", "no fix")
    assert e.id is not None
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/events/9999/ack")).status_code == 404
        assert (await c.post(f"/api/events/{e.id}/ack")).status_code == 200
        # Idempotent: the second ack of an acked event is still that event, not a miss.
        assert (await c.post(f"/api/events/{e.id}/ack")).status_code == 200


async def test_events_limit_and_level_are_validated(ctx: AppContext) -> None:
    repo = EventsRepo(ctx.db)
    for i in range(5):
        await repo.add("info", f"kind{i}", "m")
    async with client(create_app(ctx)) as c:
        assert len((await c.get("/api/events", params={"limit": 2})).json()) == 2
        assert (await c.get("/api/events", params={"limit": 0})).status_code == 422
        assert (await c.get("/api/events", params={"limit": 1001})).status_code == 422
        assert (await c.get("/api/events", params={"level": "debug"})).status_code == 422
        assert (await c.get("/api/events", params={"level": "error"})).json() == []


async def test_openapi_documents_the_failure_codes(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        paths = (await c.get("/api/openapi.json")).json()["paths"]
    assert "422" in paths["/api/history"]["get"]["responses"]
    assert "404" in paths["/api/events/{event_id}/ack"]["post"]["responses"]


async def test_the_history_and_event_routes_are_gated_like_every_other_api_route(
    tmp_path: Path,
) -> None:
    c = await make_ctx(tmp_path, web_password="hunter2", web_bind="lan")
    try:
        async with client(create_app(c)) as http:
            for path in ("/api/history", "/api/history/metrics", "/api/events"):
                assert (await http.get(path)).status_code == 401
            assert (await http.post("/api/events/1/ack")).status_code == 401
    finally:
        await c.db.close()
