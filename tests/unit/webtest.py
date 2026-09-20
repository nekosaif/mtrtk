"""Shared helpers for web API tests."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.core.statestore import StateStore
from mtrtk.rawlog.writer import Sidecar, log_path, sidecar_path
from mtrtk.store.db import Database
from mtrtk.web.context import AppContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


async def make_ctx(tmp_path: Path, **overrides) -> AppContext:  # type: ignore[no-untyped-def]
    overrides.setdefault("ntrip_password", "pw")
    overrides.setdefault("data_dir", tmp_path)
    settings = Settings(_env_file=None, **overrides)
    bus = Bus()
    store = StateStore(bus)
    db = Database(tmp_path / "mtrtk.db")
    await db.open()
    daemon = SimpleNamespace(controller=None, caster=None, basemode=None, stop=None)
    return AppContext(
        settings=settings,
        bus=bus,
        store=store,
        db=db,
        daemon=daemon,
        started_mono=time.monotonic() - 5,
    )


def load_fixture_into(store: StateStore, name: str = "f9p_hpg113_base_30s.ubx") -> int:
    frames = Framer().feed((FIXTURES / name).read_bytes())
    for f in frames:
        store.apply(f)
    return len(frames)


@asynccontextmanager
async def client(app: FastAPI, **kwargs) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    """An ASGI-transport client that also runs the app's lifespan.

    `httpx.ASGITransport` never sends the lifespan messages, so without this the startup hooks -
    which is where `create_app` builds the bus subscribers `/api/system` and `/ws` read - would
    never fire, and nothing would close them either. Used only as `async with client(app) as c`.
    """
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", **kwargs
        ) as http,
    ):
        yield http


def make_log(
    root: Path, hour: datetime, size: int = 1000, keep: bool = False, station: str = "MTRK"
) -> Path:
    """One finished hourly raw log plus its sidecar, as the writer would have left them.

    The payload is the hour repeated, so a test can tell the files apart byte by byte in a
    concatenated window export.
    """
    path = log_path(root, station, hour)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes([hour.hour]) * size)
    Sidecar(
        station,
        "base",
        hour.isoformat(),
        end_utc=(hour + timedelta(hours=1)).isoformat(),
        hour_utc=hour.isoformat(),
        bytes=size,
        keep=keep,
        complete=True,
        msg_counts={"RXM-RAWX": 3600},
    ).dump(sidecar_path(path))
    return path


H0 = datetime(2026, 9, 18, 10, tzinfo=UTC)
