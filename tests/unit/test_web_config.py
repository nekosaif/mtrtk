import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from webtest import client, make_ctx

from mtrtk.config import BaseMode, Settings
from mtrtk.store.models import Site
from mtrtk.store.repos import SitesRepo
from mtrtk.web.app import create_app
from mtrtk.web.envfile import read_env


class FakeBaseMode:
    """Stand-in for `BaseModeManager`: the attributes the live-apply path writes, plus a log."""

    def __init__(self, mode: BaseMode = BaseMode.SURVEY_IN) -> None:
        self.mode = mode
        self.svin_min_duration_s = 300
        self.svin_acc_limit_m = 2.0
        self.active_site_name: str | None = None
        self.calls: list[str] = []

    async def apply_mode(self) -> None:
        self.calls.append(f"{self.mode.value}:{self.svin_min_duration_s}:{self.svin_acc_limit_m}")


@pytest.fixture
async def ctx(tmp_path: Path):  # type: ignore[no-untyped-def]
    env = tmp_path / ".env"
    env.write_text("ROLE=base\nNTRIP_PASSWORD=pw\nSVIN_MIN_DURATION_S=300\n")
    c = await make_ctx(
        tmp_path,
        mtrtk_env_file=env,
        alert_webhook_url="https://ntfy.sh/secret",
        ntrip_url="ntrip://rover:s3cret@base.example:2101/MTRK",
    )
    try:
        yield c
    finally:
        await c.db.close()


async def test_get_config_masks_secrets(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/config")).json()
    assert body["values"]["ntrip_password"] == "***"
    assert body["values"]["alert_webhook_url"] == "***"
    assert body["values"]["web_password"] is None
    assert body["values"]["station_id"] == "MTRK" and body["values"]["base_mode"] == "survey-in"
    assert "ntrip_password" in body["secret_keys"] and "base_mode" in body["live_keys"]
    assert body["env_file"].endswith(".env")


async def test_put_config_writes_env_and_flags_restart(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config",
            json={"values": {"station_id": "BASE", "ntrip_password": "***", "rtcm_msm": 4}},
        )
    assert r.status_code == 200
    assert r.json() == {"changed": ["rtcm_msm", "station_id"], "restart_required": True}
    env = read_env(ctx.settings.mtrtk_env_file)
    assert env["STATION_ID"] == "BASE" and env["RTCM_MSM"] == "4"
    assert env["NTRIP_PASSWORD"] == "pw"  # secret untouched


async def test_put_invalid_config_is_422_and_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"station_id": "toolong"}})
    assert r.status_code == 422 and "station_id" in r.text
    assert read_env(ctx.settings.mtrtk_env_file).get("STATION_ID") is None


async def test_live_keys_apply_without_restart(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.basemode = FakeBaseMode(ctx.settings.base_mode)
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config", json={"values": {"svin_min_duration_s": 600, "svin_acc_limit_m": 1.0}}
        )
    assert r.json() == {
        "changed": ["svin_acc_limit_m", "svin_min_duration_s"],
        "restart_required": False,
    }
    assert ctx.daemon.basemode.calls == ["survey-in:600:1.0"]
    assert ctx.settings.svin_min_duration_s == 600
    assert read_env(ctx.settings.mtrtk_env_file)["SVIN_MIN_DURATION_S"] == "600"


async def test_restart_sets_stop_event(ctx) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    ctx.daemon = SimpleNamespace(controller=None, caster=None, basemode=None, stop=asyncio.Event())
    async with client(create_app(ctx)) as c:
        r = await c.post("/api/restart")
    assert r.status_code == 200 and ctx.daemon.stop.is_set()


async def test_restart_without_a_daemon_is_409(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        assert (await c.post("/api/restart")).status_code == 409


# --------------------------------------------------------------- ntrip_password is not optional


async def test_null_ntrip_password_is_rejected(ctx) -> None:  # type: ignore[no-untyped-def]
    """`None` means "undecided"; writing `NTRIP_PASSWORD=` would silently allow anonymous rovers."""
    before = ctx.settings.mtrtk_env_file.read_text()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"ntrip_password": None}})
    assert r.status_code == 422
    assert r.json()["detail"] == 'ntrip_password: use "" for anonymous or set a password'
    assert ctx.settings.mtrtk_env_file.read_text() == before


async def test_empty_ntrip_password_is_accepted_as_anonymous(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"ntrip_password": ""}})
    assert r.json() == {"changed": ["ntrip_password"], "restart_required": True}
    assert read_env(ctx.settings.mtrtk_env_file)["NTRIP_PASSWORD"] == ""


async def test_null_clears_an_optional_setting(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"alert_webhook_url": None}})
    assert r.json() == {"changed": ["alert_webhook_url"], "restart_required": True}
    assert read_env(ctx.settings.mtrtk_env_file)["ALERT_WEBHOOK_URL"] == ""


# ------------------------------------------------------------------- fixed mode needs a real site


async def test_fixed_without_a_site_is_409_and_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    ctx.daemon.basemode = FakeBaseMode()
    before = ctx.settings.mtrtk_env_file.read_text()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"base_mode": "fixed"}})
    assert r.status_code == 409
    assert ctx.settings.mtrtk_env_file.read_text() == before
    assert ctx.settings.base_mode is BaseMode.SURVEY_IN
    assert ctx.daemon.basemode.calls == []


async def test_fixed_with_an_active_site_applies_live(ctx) -> None:  # type: ignore[no-untyped-def]
    sites = SitesRepo(ctx.db)
    await sites.add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    await sites.activate("roof")
    ctx.daemon.basemode = FakeBaseMode()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"base_mode": "fixed"}})
    assert r.json() == {"changed": ["base_mode"], "restart_required": False}
    assert ctx.daemon.basemode.calls == ["fixed:300:2.0"]
    assert read_env(ctx.settings.mtrtk_env_file)["BASE_MODE"] == "fixed"


async def test_fixed_with_a_named_site_applies_live(ctx) -> None:  # type: ignore[no-untyped-def]
    await SitesRepo(ctx.db).add(Site.from_ecef("roof", 1.0, 2.0, 3.0, source="manual"))
    ctx.daemon.basemode = FakeBaseMode()
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config", json={"values": {"base_mode": "fixed", "active_site": "roof"}}
        )
    assert r.json() == {"changed": ["active_site", "base_mode"], "restart_required": False}
    assert ctx.daemon.basemode.active_site_name == "roof"
    env = read_env(ctx.settings.mtrtk_env_file)
    assert env["BASE_MODE"] == "fixed" and env["ACTIVE_SITE"] == "roof"


async def test_fixed_with_an_unknown_site_and_no_active_row_is_409(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config", json={"values": {"base_mode": "fixed", "active_site": "nope"}}
        )
    assert r.status_code == 409
    assert read_env(ctx.settings.mtrtk_env_file).get("BASE_MODE") is None


# ---------------------------------------------------------------------------------------- misc


async def test_unknown_key_is_422(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"nope": 1}})
    assert r.status_code == 422 and "nope" in r.text


async def test_unchanged_values_write_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    before = ctx.settings.mtrtk_env_file.read_text()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"station_id": "MTRK"}})
    assert r.json() == {"changed": [], "restart_required": False}
    assert ctx.settings.mtrtk_env_file.read_text() == before


async def test_live_change_without_a_manager_needs_a_restart(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"svin_acc_limit_m": 1.5}})
    assert r.json() == {"changed": ["svin_acc_limit_m"], "restart_required": True}
    assert read_env(ctx.settings.mtrtk_env_file)["SVIN_ACC_LIMIT_M"] == "1.5"


async def test_secrets_are_never_logged(ctx, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]
    caplog.set_level(logging.DEBUG)
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"ntrip_password": "hunter2"}})
    assert r.status_code == 200
    assert "hunter2" not in caplog.text
    assert "ntrip_password" in caplog.text  # the key is named, the value is not


async def test_config_requires_the_password(tmp_path: Path) -> None:
    c = await make_ctx(tmp_path, mtrtk_env_file=tmp_path / ".env", web_password="s3cret")
    try:
        async with client(create_app(c)) as http:
            assert (await http.get("/api/config")).status_code == 401
            assert (await http.put("/api/config", json={"values": {}})).status_code == 401
            assert (await http.post("/api/restart")).status_code == 401
    finally:
        await c.db.close()


# ------------------------------------------------------------------------ the env-file pointer


async def test_the_env_file_pointer_is_read_only(ctx) -> None:  # type: ignore[no-untyped-def]
    """`Settings` always reads `.env`: moving the pointer would write a file nobody reads."""
    was = ctx.settings.mtrtk_env_file
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/config")).json()
        r = await c.put("/api/config", json={"values": {"mtrtk_env_file": "/tmp/elsewhere.env"}})
    assert body["read_only_keys"] == ["mtrtk_env_file"]
    assert body["url_secret_keys"] == ["ntrip_url"]
    assert r.status_code == 422 and "mtrtk_env_file" in r.text
    assert ctx.settings.mtrtk_env_file == was
    assert read_env(was).get("MTRTK_ENV_FILE") is None


# ------------------------------------------------------------------- credentials inside a URL


async def test_ntrip_url_password_is_masked(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/config")).json()
    assert body["values"]["ntrip_url"] == "ntrip://rover:***@base.example:2101/MTRK"


async def test_an_unedited_masked_url_is_not_a_change(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config",
            json={"values": {"ntrip_url": "ntrip://rover:***@base.example:2101/MTRK"}},
        )
    assert r.json() == {"changed": [], "restart_required": False}


async def test_a_masked_url_keeps_the_stored_password(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config",
            json={"values": {"ntrip_url": "ntrip://rover:***@base.example:2101/OTHER"}},
        )
    assert r.json() == {"changed": ["ntrip_url"], "restart_required": True}
    assert (
        read_env(ctx.settings.mtrtk_env_file)["NTRIP_URL"]
        == "ntrip://rover:s3cret@base.example:2101/OTHER"
    )


async def test_a_new_url_password_replaces_the_stored_one(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put(
            "/api/config",
            json={"values": {"ntrip_url": "ntrip://rover:fresh@base.example:2101/MTRK"}},
        )
    assert r.json() == {"changed": ["ntrip_url"], "restart_required": True}
    assert (
        read_env(ctx.settings.mtrtk_env_file)["NTRIP_URL"]
        == "ntrip://rover:fresh@base.example:2101/MTRK"
    )


# ------------------------------------------------------------------------- values a line would eat


async def test_a_newline_in_a_value_is_422(ctx) -> None:  # type: ignore[no-untyped-def]
    before = ctx.settings.mtrtk_env_file.read_text()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"marker_name": "X\nROLE=rover"}})
    assert r.status_code == 422 and "marker_name" in r.text
    assert ctx.settings.mtrtk_env_file.read_text() == before


async def test_a_password_with_a_hash_survives_the_round_trip(ctx) -> None:  # type: ignore[no-untyped-def]
    """The operator must not be locked out by the password the API said it saved."""
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"web_password": "hunter2 #1"}})
    assert r.status_code == 200
    assert Settings(_env_file=ctx.settings.mtrtk_env_file).web_password == "hunter2 #1"


async def test_a_malformed_body_does_not_echo_the_payload(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": "hunter2"})
    assert r.status_code == 422 and "hunter2" not in r.text


# ----------------------------------------------------------------- what a restart would change


async def test_pending_shows_what_the_running_process_has_not_picked_up(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/config")).json()["pending"] == {}
        await c.put("/api/config", json={"values": {"station_id": "BASE"}})
        body = (await c.get("/api/config")).json()
    assert body["values"]["station_id"] == "MTRK"  # still what the process is running on
    assert body["pending"] == {"station_id": "BASE"}


async def test_pending_masks_secrets(ctx) -> None:  # type: ignore[no-untyped-def]
    async with client(create_app(ctx)) as c:
        await c.put("/api/config", json={"values": {"ntrip_password": "newpw"}})
        body = (await c.get("/api/config")).json()
    assert body["pending"] == {"ntrip_password": "***"}
