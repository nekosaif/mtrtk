from pathlib import Path
from typing import Any

import pytest
from webtest import client, make_ctx

from mtrtk.core.link import LinkTimeout
from mtrtk.core.receiver import Capabilities, ReceiverError
from mtrtk.web.app import create_app


class FakeController:
    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.passive = False
        self.capabilities = Capabilities(
            protver="27.12",
            fw_version="HPG 1.13",
            module="ZED-F9P",
            supported={"MON-COMMS"},
            unsupported={"MON-SPAN"},
        )
        self.calls: list[str] = []
        self.raises: Exception | None = None

    def _check(self) -> None:
        if not self.connected:
            raise ReceiverError("receiver not connected")
        if self.raises is not None:
            raise self.raises

    async def reapply(self) -> Capabilities:
        self._check()
        self.calls.append("reapply")
        return self.capabilities

    async def reset(self, kind: str) -> None:
        self._check()
        if kind not in ("hot", "warm", "cold", "factory"):
            raise ValueError(kind)
        self.calls.append(f"reset:{kind}")

    async def poll(self, msg_class: str, msg_id: str) -> dict[str, Any]:
        self._check()
        self.calls.append(f"poll:{msg_id}")
        return {"identity": msg_id, "swVersion": "EXT CORE 1.00 (f10c36)"}


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = await make_ctx(tmp_path)
    c.daemon.controller = FakeController()
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_receiver(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/receiver")).json()
    assert body["connected"] is True and body["capabilities"]["unsupported"] == ["MON-SPAN"]
    assert body["source"] == "auto" and body["passive"] is False
    assert set(body["firmware"]) >= {"fw_version", "protver", "module"}


async def test_actions(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/receiver/reapply")).json()["capabilities"][
            "fw_version"
        ] == "HPG 1.13"
        assert (await c.post("/api/receiver/reset", json={"kind": "warm"})).json() == {
            "ok": True,
            "kind": "warm",
        }
        assert (await c.post("/api/receiver/reset", json={"kind": "bogus"})).status_code == 422
        r = await c.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "MON-VER"})
    assert r.json()["swVersion"].startswith("EXT CORE")
    assert ctx.daemon.controller.calls == ["reapply", "reset:warm", "poll:MON-VER"]


async def test_rejected_reset_kind_echoes_nothing_of_the_body(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.post("/api/receiver/reset", json={"kind": "bogus"})).json()
    assert all("input" not in err for err in body["detail"])


async def test_actions_when_disconnected_are_409(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.controller.connected = False
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/receiver/reapply")).status_code == 409
        assert (await c.post("/api/receiver/reset", json={"kind": "hot"})).status_code == 409
        poll = await c.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "MON-VER"})
    assert poll.status_code == 409 and ctx.daemon.controller.calls == []


async def test_a_silent_receiver_is_504_not_500(ctx) -> None:  # type: ignore[no-untyped-def]
    """`LinkTimeout` is a `TimeoutError`, hence an `OSError`: it has to be mapped before the
    link-failure 409, and an unanswered request is a gateway timeout, not a bad request."""
    ctx.daemon.controller.raises = LinkTimeout("no response for ['MON-VER'] within 2.0s")
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "MON-VER"})
        assert (await c.post("/api/receiver/reapply")).status_code == 504
    assert r.status_code == 504 and "did not answer" in r.json()["detail"]


async def test_a_vanished_handle_is_409_not_500(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.controller.raises = OSError("[Errno 5] Input/output error")
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/receiver/reset", json={"kind": "hot"})).status_code == 409


async def test_an_unknown_message_name_is_422(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.controller.raises = KeyError("MON-NOPE")
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "MON-NOPE"})
    assert r.status_code == 422 and "MON-NOPE" in r.json()["detail"]


async def test_no_controller_is_409(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path)
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.post("/api/receiver/reapply")).status_code == 409
            body = (await c.get("/api/receiver")).json()
        assert body["connected"] is False and body["capabilities"] is None
    finally:
        await ctx.db.close()


async def test_a_replay_context_reports_passive(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path, mtrtk_source="file:/tmp/replay.ubx")
    try:
        async with client(create_app(ctx)) as c:
            body = (await c.get("/api/receiver")).json()
        assert body["passive"] is True and body["source"] == "file:/tmp/replay.ubx"
    finally:
        await ctx.db.close()


async def test_routes_require_auth(tmp_path: Path) -> None:
    ctx = await make_ctx(tmp_path, web_password="secret")
    ctx.daemon.controller = FakeController()
    try:
        async with client(create_app(ctx)) as c:
            assert (await c.get("/api/receiver")).status_code == 401
            assert (await c.post("/api/receiver/reapply")).status_code == 401
            assert (await c.post("/api/receiver/reset", json={"kind": "hot"})).status_code == 401
            assert (
                await c.post("/api/receiver/poll", json={"msg_class": "MON", "msg_id": "MON-VER"})
            ).status_code == 401
        assert ctx.daemon.controller.calls == []
    finally:
        await ctx.db.close()
