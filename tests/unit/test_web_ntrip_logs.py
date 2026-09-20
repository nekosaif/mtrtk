"""NTRIP status, clients and history; the raw-log list, download, keep, delete and export."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from webtest import H0, client, make_ctx, make_log

from mtrtk.base.ntrip_caster import ClientInfo
from mtrtk.rawlog.writer import Sidecar, sidecar_path
from mtrtk.store.repos import LogFilesRepo, NtripLogRepo
from mtrtk.web.api import logs as logs_api
from mtrtk.web.app import create_app
from mtrtk.web.context import AppContext


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[AppContext]:
    # `ntrip_port` / `ntrip_bind` are pinned here on purpose: the suite's autouse hermetic
    # fixture puts NTRIP_PORT=0 and NTRIP_BIND=127.0.0.1 in the environment, and
    # pydantic-settings reads the environment even with `_env_file=None`, so without these the
    # endpoint would be asked about a configuration no base ever runs.
    c = await make_ctx(tmp_path, ntrip_user="rover", ntrip_port=2101, ntrip_bind="tailscale")
    try:
        yield c
    finally:
        await c.db.close()


def fake_writer(path: Path | None) -> Any:
    """A stand-in for the `RawLogWriter` the daemon will publish as `Daemon.rawlog` (Task 10).

    `current_path` is a property on the real writer, so it is a plain attribute here; `set_keep`
    records what it was told, which is the whole point of the two branches it is used to pin.
    """
    calls: list[bool] = []
    return SimpleNamespace(current_path=path, set_keep=calls.append, calls=calls)


async def eventually_rows(repo: LogFilesRepo, count: int) -> list[dict[str, Any]]:
    """Poll `log_files` until the mirror has caught up (or give the last reading to the assert)."""
    deadline = asyncio.get_running_loop().time() + 1.0
    rows = await repo.list()
    while len(rows) != count and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        rows = await repo.list()
    return rows


# --------------------------------------------------------------------------------- NTRIP


async def test_ntrip_info_without_caster(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/ntrip")).json()
    assert body["running"] is False and body["mountpoint"] == "MTRK" and body["anonymous"] is False
    assert body["connection_url"] == "ntrip://rover:***@<bind-address>:2101/MTRK"
    # Live-only fields have no honest value without a caster; the configured cap still does.
    assert body["clients"] is None and body["rejected"] is None and body["sourcetable"] is None
    assert body["max_clients"] == 32 and body["bind_mode"] == "tailscale"
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/ntrip/clients")).json() == []


async def test_ntrip_info_with_caster_and_history(ctx: AppContext) -> None:
    info = ClientInfo(
        id=1,
        ip="100.100.50.12",
        port=5000,
        mountpoint="MTRK",
        user_agent="NTRIP SWMaps",
        username="rover",
        version=2,
        connected_utc=datetime.now(UTC),
        bytes_sent=123,
    )
    ctx.daemon.caster = SimpleNamespace(
        host="100.100.50.10",
        port=2101,
        clients={1: info},
        rejected=2,
        max_clients=8,
        sourcetable_body=lambda: b"STR;MTRK;...\r\nENDSOURCETABLE\r\n",
        config=SimpleNamespace(anonymous=False),
    )
    row = await NtripLogRepo(ctx.db).connected("100.100.50.12", "MTRK", "NTRIP SWMaps", "rover")
    await NtripLogRepo(ctx.db).disconnected(row, 555, 23.8, 90.2, "client closed")
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/ntrip")).json()
        assert body["running"] is True
        assert body["connection_url"] == "ntrip://rover:***@100.100.50.10:2101/MTRK"
        assert body["sourcetable"].startswith("STR;MTRK")
        assert body["clients"] == 1 and body["rejected"] == 2 and body["max_clients"] == 8
        clients = (await c.get("/api/ntrip/clients")).json()
        assert clients[0]["ip"] == "100.100.50.12" and clients[0]["bytes_sent"] == 123
        history = (await c.get("/api/ntrip/history")).json()
    assert history[0]["bytes_sent"] == 555 and history[0]["reason"] == "client closed"


async def test_the_ntrip_password_is_never_in_a_response(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, ntrip_user="rover", ntrip_password="s3cret", ntrip_port=2101)
    try:
        async with client(create_app(c)) as http:
            text = (await http.get("/api/ntrip")).text
    finally:
        await c.db.close()
    assert "s3cret" not in text and "rover:***@" in text


async def test_an_anonymous_mountpoint_has_no_credentials_in_the_url(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, ntrip_password="", ntrip_port=2101, ntrip_bind="100.64.0.7")
    try:
        async with client(create_app(c)) as http:
            body = (await http.get("/api/ntrip")).json()
    finally:
        await c.db.close()
    assert body["anonymous"] is True and body["username"] is None
    # A literal bind address is the host, and it is known before anything is listening.
    assert body["connection_url"] == "ntrip://100.64.0.7:2101/MTRK"


# ---------------------------------------------------------------------------------- logs


async def test_logs_list_availability_download_keep_delete(ctx: AppContext, tmp_path: Path) -> None:
    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i), size=100 * (i + 1))
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/logs")).json()
        assert [f["hour_utc"] for f in body["files"]] == [
            (H0 + timedelta(hours=i)).isoformat() for i in range(3)
        ]
        assert body["total_bytes"] == 600 and body["hours"] == 3 and body["disk_free_gb"] > 0
        avail = (
            await c.get(
                "/api/logs/availability",
                params={"from": H0.isoformat(), "to": (H0 + timedelta(hours=5)).isoformat()},
            )
        ).json()
        assert [a["available"] for a in avail] == [True, True, True, False, False]
        name = "MTRK_20260918_10.ubx"
        r = await c.get(f"/api/logs/{name}")
        assert r.status_code == 200
        assert r.content == bytes([10]) * 100 and "attachment" in r.headers["content-disposition"]
        # httpx resolves `..` out of the URL before it is sent, so the traversal has to be
        # percent-encoded to reach the route at all - and there it is a name, never a path.
        assert (await c.get("/api/logs/%2E%2E%2F%2E%2E%2Fetc%2Fpasswd")).status_code == 404
        # A real name behind a separator is the dangerous shape: `Path(name).name` parses, so
        # only the separator check stands between it and a file outside the tree.
        assert (await c.get("/api/logs/..%2FMTRK_20260918_10.ubx")).status_code == 404
        assert (await c.get("/api/logs/sub/MTRK_20260918_10.ubx")).status_code == 404
        assert (await c.get("/api/logs/notalog.ubx")).status_code == 404
        r = await c.patch(f"/api/logs/{name}", json={"keep": True})
        assert r.status_code == 200 and r.json()["keep"] is True
        assert Sidecar.load(sidecar_path(tmp_path / "ubx" / "2026" / "261" / name)).keep is True
        newest = "MTRK_20260918_12.ubx"
        refused = await c.delete(f"/api/logs/{newest}")  # the newest hour is protected
        assert refused.status_code == 409 and "force" in refused.json()["detail"]
        assert "still being written" in refused.json()["detail"]
        assert (await c.delete("/api/logs/MTRK_20260918_11.ubx")).status_code == 200  # older: fine
        assert (await c.delete(f"/api/logs/{newest}", params={"force": 1})).status_code == 200
        assert len((await c.get("/api/logs")).json()["files"]) == 1


async def test_logs_window_concatenates(ctx: AppContext, tmp_path: Path) -> None:
    for i in range(3):
        make_log(tmp_path, H0 + timedelta(hours=i), size=10)
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/logs/window",
            params={
                "from": (H0 + timedelta(minutes=30)).isoformat(),
                "to": (H0 + timedelta(hours=2, minutes=30)).isoformat(),
            },
        )
    assert r.status_code == 200
    assert r.content == bytes([10]) * 10 + bytes([11]) * 10 + bytes([12]) * 10
    disposition = r.headers["content-disposition"]
    assert disposition == 'attachment; filename="MTRK_2026091810_2026091812.ubx"'
    async with client(create_app(ctx)) as c:
        assert (
            await c.get(
                "/api/logs/window",
                params={
                    "from": (H0 + timedelta(days=5)).isoformat(),
                    "to": (H0 + timedelta(days=6)).isoformat(),
                },
            )
        ).status_code == 404


async def test_a_window_needs_two_aware_instants_in_order(ctx: AppContext, tmp_path: Path) -> None:
    make_log(tmp_path, H0)
    async with client(create_app(ctx)) as c:
        naive = await c.get("/api/logs/window", params={"from": "2026-09-18T10:00:00", "to": "x"})
        assert naive.status_code == 422
        backwards = await c.get(
            "/api/logs/window",
            params={"to": H0.isoformat(), "from": (H0 + timedelta(hours=2)).isoformat()},
        )
        assert backwards.status_code == 422
        assert (
            await c.get("/api/logs/availability", params={"from": "yesterday", "to": "now"})
        ).status_code == 422


async def test_keep_on_the_open_hour_goes_through_the_writer(
    ctx: AppContext, tmp_path: Path
) -> None:
    """Ruling 1: the writer owns the open sidecar and would overwrite a flag written behind it."""
    path = make_log(tmp_path, H0)
    ctx.daemon.rawlog = writer = fake_writer(path)
    async with client(create_app(ctx)) as c:
        r = await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": True})
    assert r.status_code == 200 and r.json()["keep"] is True
    assert writer.calls == [True]  # the in-memory sidecar, not just the file
    assert Sidecar.load(sidecar_path(path)).keep is True
    rows = await LogFilesRepo(ctx.db).list()
    assert [(r["path"], r["keep"]) for r in rows] == [(str(path), 1)]


async def test_keep_on_a_closed_hour_never_touches_the_writer(
    ctx: AppContext, tmp_path: Path
) -> None:
    open_path = make_log(tmp_path, H0 + timedelta(hours=1))
    closed = make_log(tmp_path, H0, keep=True)
    ctx.daemon.rawlog = writer = fake_writer(open_path)
    async with client(create_app(ctx)) as c:
        r = await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": False})
    assert r.status_code == 200 and r.json()["keep"] is False
    assert writer.calls == []
    assert Sidecar.load(sidecar_path(closed)).keep is False


async def test_delete_refuses_the_hour_the_writer_has_open_even_with_force(
    ctx: AppContext, tmp_path: Path
) -> None:
    make_log(tmp_path, H0)
    path = make_log(tmp_path, H0 + timedelta(hours=1))
    ctx.daemon.rawlog = fake_writer(path)
    async with client(create_app(ctx)) as c:
        assert (await c.delete("/api/logs/MTRK_20260918_11.ubx")).status_code == 409
        r = await c.delete("/api/logs/MTRK_20260918_11.ubx", params={"force": 1})
        assert r.status_code == 409
        # The writer names the open file, so the older hour needs no `force` to go.
        assert (await c.delete("/api/logs/MTRK_20260918_10.ubx")).status_code == 200
    assert path.exists()


async def test_delete_refuses_a_kept_file_until_the_mark_is_cleared(
    ctx: AppContext, tmp_path: Path
) -> None:
    kept = make_log(tmp_path, H0, keep=True)
    make_log(tmp_path, H0 + timedelta(hours=1))  # the newest, so `kept` is not the protected one
    async with client(create_app(ctx)) as c:
        r = await c.delete("/api/logs/MTRK_20260918_10.ubx")
        assert r.status_code == 409 and "keep" in r.json()["detail"]
        # `force` is about the newest hour; a keep mark is an operator's decision, so it is
        # cleared deliberately rather than overridden by a flag.
        forced = await c.delete("/api/logs/MTRK_20260918_10.ubx", params={"force": 1})
        assert forced.status_code == 409
        await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": False})
        assert (await c.delete("/api/logs/MTRK_20260918_10.ubx")).status_code == 200
    assert not kept.exists() and not sidecar_path(kept).exists()


async def test_delete_removes_the_sidecar_and_the_database_row(
    ctx: AppContext, tmp_path: Path
) -> None:
    path = make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))
    await LogFilesRepo(ctx.db).upsert(path, Sidecar.load(sidecar_path(path)))
    async with client(create_app(ctx)) as c:
        assert (await c.delete("/api/logs/MTRK_20260918_10.ubx")).status_code == 200
    assert not path.exists() and not sidecar_path(path).exists()
    assert await LogFilesRepo(ctx.db).list() == []


async def test_the_log_index_mirror_follows_the_rawlog_bus_topics(
    ctx: AppContext, tmp_path: Path
) -> None:
    """Ruling 2: one lifespan-owned subscriber fills `log_files`; nothing subscribes per request."""
    repo = LogFilesRepo(ctx.db)
    path = make_log(tmp_path, H0, size=64)
    before = ctx.bus.subscriber_count
    app = create_app(ctx)
    async with client(app):
        assert ctx.bus.subscriber_count == before + 3  # cache, hub, mirror
        ctx.bus.publish("rawlog.rotated", path)
        rows = await eventually_rows(repo, 1)
        assert rows[0]["path"] == str(path) and rows[0]["bytes"] == 64
        ctx.bus.publish("rawlog.pruned", path)
        assert await eventually_rows(repo, 0) == []
        mirror = app.state.log_index
        assert mirror.applied == 2 and mirror.failures == {}
    assert ctx.bus.subscriber_count == before
    assert getattr(app.state, "log_index", None) is None


async def test_the_mirror_survives_a_missing_sidecar(
    ctx: AppContext, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = make_log(tmp_path, H0)
    sidecar_path(path).unlink()
    good = make_log(tmp_path, H0 + timedelta(hours=1))
    repo = LogFilesRepo(ctx.db)
    with caplog.at_level(logging.WARNING, logger="mtrtk.web.api.logs"):
        async with client(create_app(ctx)):
            ctx.bus.publish("rawlog.closed", path)
            ctx.bus.publish("rawlog.closed", good)
            rows = await eventually_rows(repo, 1)
    assert [r["path"] for r in rows] == [str(good)]  # the bad one is skipped, the mirror lives
    assert sum("no usable sidecar" in r.message for r in caplog.records) == 1


async def test_keep_is_persisted_even_when_the_sidecar_is_gone(
    ctx: AppContext, tmp_path: Path
) -> None:
    """A 200 has to mean the mark survives: the sidecar is what retention reads."""
    path = make_log(tmp_path, H0)
    sidecar_path(path).unlink()
    make_log(tmp_path, H0 + timedelta(hours=1))
    async with client(create_app(ctx)) as c:
        r = await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": True})
        assert r.status_code == 200 and r.json()["keep"] is True
        listed = (await c.get("/api/logs")).json()["files"]
        assert (await c.delete("/api/logs/MTRK_20260918_10.ubx")).status_code == 409
    rebuilt = Sidecar.load(sidecar_path(path))
    assert rebuilt.keep is True and rebuilt.station_id == "MTRK" and rebuilt.recovered is True
    assert rebuilt.hour_utc == H0.isoformat() and rebuilt.bytes == 1000
    assert [(f["name"], f["keep"]) for f in listed][0] == ("MTRK_20260918_10.ubx", True)
    rows = await LogFilesRepo(ctx.db).list()
    assert [(r["path"], r["keep"]) for r in rows] == [(str(path), 1)]


async def test_an_unwritable_card_makes_keep_a_409_not_a_lie(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rebuilt sidecar cannot be written, the mark did not survive: say so."""
    path = make_log(tmp_path, H0)
    sidecar_path(path).unlink()
    make_log(tmp_path, H0 + timedelta(hours=1))

    def refuse(self: Sidecar, target: Path) -> None:
        raise OSError("Read-only file system")

    monkeypatch.setattr(Sidecar, "dump", refuse)
    async with client(create_app(ctx)) as c:
        r = await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": True})
    assert r.status_code == 409
    assert "Read-only file system" in r.json()["detail"]
    assert not sidecar_path(path).exists()
    assert await LogFilesRepo(ctx.db).list() == []


async def test_the_open_hour_is_streamed_within_the_size_it_was_stated_to_have(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file the writer is appending to must not overrun the length the response promised."""
    path = make_log(tmp_path, H0, size=100)
    ctx.daemon.rawlog = fake_writer(path)
    real = logs_api.list_logs

    def grows(root: Path) -> Any:
        files = real(root)  # the stat the response is bounded by...
        with path.open("ab") as fh:
            fh.write(b"\xb5" * 50)  # ...and the writer appending right after it
        return files

    monkeypatch.setattr(logs_api, "list_logs", grows)
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/logs/MTRK_20260918_10.ubx")
    assert r.status_code == 200 and len(r.content) == 100
    assert "content-length" not in r.headers  # chunked: nothing to overrun
    assert "attachment" in r.headers["content-disposition"]


async def test_a_finished_hour_is_still_served_with_a_length(
    ctx: AppContext, tmp_path: Path
) -> None:
    make_log(tmp_path, H0, size=100)
    async with client(create_app(ctx)) as c:
        r = await c.get("/api/logs/MTRK_20260918_10.ubx")
    assert r.status_code == 200 and r.headers["content-length"] == "100"


async def test_a_range_no_operator_meant_is_refused(ctx: AppContext, tmp_path: Path) -> None:
    """A mistyped year is a 422, not 600 MB of slots and an OOM kill."""
    make_log(tmp_path, H0)
    async with client(create_app(ctx)) as c:
        span = {"from": H0.isoformat(), "to": (H0 + timedelta(days=400)).isoformat()}
        r = await c.get("/api/logs/availability", params=span)
        assert r.status_code == 422 and "366 days" in r.json()["detail"]
        ok = {"from": H0.isoformat(), "to": (H0 + timedelta(days=366)).isoformat()}
        assert (await c.get("/api/logs/availability", params=ok)).status_code == 200
        long_ = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=49)).isoformat()}
        r = await c.get("/api/logs/window", params=long_)
        assert r.status_code == 422 and "48 hours" in r.json()["detail"]
        fits = {"from": H0.isoformat(), "to": (H0 + timedelta(hours=48)).isoformat()}
        assert (await c.get("/api/logs/window", params=fits)).status_code == 200


async def test_a_window_spanning_two_stations_says_so_in_its_name(
    ctx: AppContext, tmp_path: Path
) -> None:
    make_log(tmp_path, H0, size=10)
    make_log(tmp_path, H0 + timedelta(hours=1), size=10, station="ABCD")
    async with client(create_app(ctx)) as c:
        r = await c.get(
            "/api/logs/window",
            params={"from": H0.isoformat(), "to": (H0 + timedelta(hours=2)).isoformat()},
        )
    assert r.status_code == 200 and len(r.content) == 20
    assert r.headers["content-disposition"].endswith('filename="MIXED_2026091810_2026091811.ubx"')


async def test_delete_sweeps_a_stray_sidecar_temporary(ctx: AppContext, tmp_path: Path) -> None:
    """A crash inside `Sidecar.dump` leaves a `.json.tmp` nothing else would ever remove."""
    path = make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))
    stray = sidecar_path(path).with_suffix(".json.tmp")
    stray.write_text("{half a sidecar")
    async with client(create_app(ctx)) as c:
        assert (await c.delete("/api/logs/MTRK_20260918_10.ubx")).status_code == 200
    assert not path.exists() and not sidecar_path(path).exists() and not stray.exists()


async def test_the_mirror_budgets_its_complaints_by_reason(
    ctx: AppContext,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database fault must not be swallowed by the budget an unreadable sidecar just spent."""
    first = make_log(tmp_path, H0)
    second = make_log(tmp_path, H0 + timedelta(hours=1))
    sidecar_path(first).unlink()
    sidecar_path(second).unlink()
    good = make_log(tmp_path, H0 + timedelta(hours=2))

    class Wedged:
        def __init__(self, db: Any) -> None: ...

        async def upsert(self, path: Path, sidecar: Sidecar) -> None:
            raise RuntimeError("database is locked")

    monkeypatch.setattr(logs_api, "LogFilesRepo", Wedged)
    app = create_app(ctx)
    with caplog.at_level(logging.WARNING, logger="mtrtk.web.api.logs"):
        async with client(app):
            mirror = app.state.log_index
            for path in (first, second, good):
                ctx.bus.publish("rawlog.closed", path)
            deadline = asyncio.get_running_loop().time() + 1.0
            while sum(mirror.failures.values()) < 3:
                assert asyncio.get_running_loop().time() < deadline, mirror.failures
                await asyncio.sleep(0.01)
    assert mirror.failures == {"sidecar": 2, "database": 1} and mirror.applied == 0
    # The second sidecar complaint is inside the minute; the database one has its own budget.
    assert sum("no usable sidecar" in r.getMessage() for r in caplog.records) == 1
    assert sum("could not mirror" in r.getMessage() for r in caplog.records) == 1


async def test_openapi_documents_the_failure_codes(ctx: AppContext) -> None:
    async with client(create_app(ctx)) as c:
        paths = (await c.get("/api/openapi.json")).json()["paths"]
    assert "404" in paths["/api/logs/{name}"]["get"]["responses"]
    assert {"404", "422"} <= set(paths["/api/logs/{name}"]["patch"]["responses"])
    assert {"404", "409"} <= set(paths["/api/logs/{name}"]["delete"]["responses"])
    assert {"404", "422"} <= set(paths["/api/logs/window"]["get"]["responses"])
    assert "422" in paths["/api/logs/availability"]["get"]["responses"]


async def test_the_log_endpoints_are_gated_like_every_other_api_route(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, web_password="hunter2", web_bind="lan")
    try:
        async with client(create_app(c)) as http:
            for path in ("/api/logs", "/api/ntrip", "/api/ntrip/clients", "/api/ntrip/history"):
                assert (await http.get(path)).status_code == 401
    finally:
        await c.db.close()


# ----------------------------------------------------------- the final fix wave (group D)


async def test_keep_loads_and_dumps_the_sidecar_once_each_off_the_event_loop(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two loads and two dumps used to run on the loop: the route's, then the repo's again.

    Every one of them stats, reads, writes, fsyncs and renames on the SD card, with the caster
    and the receiver reader stopped behind them.
    """
    import threading

    from mtrtk.store import repos as repos_module

    make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))
    loads: list[bool] = []  # True when it ran on the event-loop thread
    dumps: list[bool] = []

    class Spy(Sidecar):
        @classmethod
        def load(cls, path: Path) -> Sidecar:
            loads.append(threading.current_thread() is threading.main_thread())
            return super().load(path)

        def dump(self, path: Path) -> None:
            dumps.append(threading.current_thread() is threading.main_thread())
            super().dump(path)

    monkeypatch.setattr(logs_api, "Sidecar", Spy)
    monkeypatch.setattr(repos_module, "Sidecar", Spy)
    async with client(create_app(ctx)) as c:
        r = await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": True})
    assert r.status_code == 200 and r.json()["keep"] is True
    assert loads == [False] and dumps == [False]
    rows = await LogFilesRepo(ctx.db).list()
    kept = [(Path(row["path"]).name, row["keep"]) for row in rows]
    assert kept == [("MTRK_20260918_10.ubx", 1)]


async def test_an_unwritable_card_is_a_409_for_a_sidecar_that_loaded_too(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only the rebuilt-sidecar path: any dump the card refuses means the mark did not land."""
    path = make_log(tmp_path, H0)
    make_log(tmp_path, H0 + timedelta(hours=1))

    def refuse(self: Sidecar, target: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(Sidecar, "dump", refuse)
    async with client(create_app(ctx)) as c:
        r = await c.patch("/api/logs/MTRK_20260918_10.ubx", json={"keep": True})
    assert r.status_code == 409 and "No space left on device" in r.json()["detail"]
    assert Sidecar.load(sidecar_path(path)).keep is False  # untouched on disk


async def test_the_log_list_sorts_msg_counts(ctx: AppContext, tmp_path: Path) -> None:
    """A dict in insertion order makes two identical listings look different in a diff."""
    path = make_log(tmp_path, H0)
    sidecar = Sidecar.load(sidecar_path(path))
    sidecar.msg_counts = {"RXM-SFRBX": 1, "NAV-PVT": 2, "MON-VER": 3}
    sidecar.dump(sidecar_path(path))
    async with client(create_app(ctx)) as c:
        listed = (await c.get("/api/logs")).json()["files"]
    assert list(listed[0]["msg_counts"]) == ["MON-VER", "NAV-PVT", "RXM-SFRBX"]


async def test_set_keep_skips_its_own_dump_when_the_caller_already_wrote_one(
    ctx: AppContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo still writes the sidecar for a caller that has not - the CLI, a later phase."""
    from mtrtk.store import repos as repos_module

    path = make_log(tmp_path, H0)
    repo = LogFilesRepo(ctx.db)
    await repo.upsert(path, Sidecar.load(sidecar_path(path)))
    dumps: list[Path] = []
    real_dump = Sidecar.dump
    monkeypatch.setattr(
        repos_module.Sidecar,
        "dump",
        lambda self, target: (dumps.append(target), real_dump(self, target))[1],
    )
    already = Sidecar.load(sidecar_path(path))
    already.keep = True
    already.dump(sidecar_path(path))
    dumps.clear()
    await repo.set_keep(path, True, sidecar=already)
    assert dumps == []  # the caller's write is the one that counts
    await repo.set_keep(path, False)
    assert dumps == [sidecar_path(path)]  # and without one, the repo still writes it
    assert Sidecar.load(sidecar_path(path)).keep is False
