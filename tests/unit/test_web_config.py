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


async def test_a_stale_env_line_is_rewritten_even_when_the_process_agrees(ctx) -> None:  # type: ignore[no-untyped-def]
    """`.env` is the restart oracle: a value only the process holds has to be persisted.

    This is the shape of the base-mode flow - `POST /api/base/sites/{name}/activate` moves the
    running settings to match the manager, so the follow-up PUT that is supposed to make the
    change durable would find nothing different in memory and write nothing at all.
    """
    ctx.settings.svin_min_duration_s = 900  # the file still says 300
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"svin_min_duration_s": 900}})
        assert r.json() == {"changed": ["svin_min_duration_s"], "restart_required": True}
        assert (await c.get("/api/config")).json()["pending"] == {}
    assert read_env(ctx.settings.mtrtk_env_file)["SVIN_MIN_DURATION_S"] == "900"


async def test_a_key_the_file_never_had_is_not_a_change_on_its_own(ctx) -> None:  # type: ignore[no-untyped-def]
    """Only a *disagreeing* line counts: an absent key must not turn every PUT into a write.

    `.env` deliberately carries a handful of keys; everything else comes from the environment or
    the defaults, and a form posting its values back must stay a no-op.
    """
    before = ctx.settings.mtrtk_env_file.read_bytes()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"station_id": "MTRK", "rtcm_msm": 7}})
    assert r.json() == {"changed": [], "restart_required": False}
    assert ctx.settings.mtrtk_env_file.read_bytes() == before


async def test_a_changed_key_drags_in_the_values_the_file_is_missing(ctx) -> None:  # type: ignore[no-untyped-def]
    """Once something is being written, everything the request asked for is recorded with it."""
    async with client(create_app(ctx)) as c:
        await c.put(
            "/api/config", json={"values": {"svin_min_duration_s": 600, "station_id": "MTRK"}}
        )
    disk = read_env(ctx.settings.mtrtk_env_file)
    assert disk["SVIN_MIN_DURATION_S"] == "600" and disk["STATION_ID"] == "MTRK"


# --------------------------------------------------------------- bounded free-text settings


async def test_an_overlong_free_text_setting_is_422_and_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    """`.env` is re-read on every GET and on every restart: a 20 MB marker name is not a name."""
    before = read_env(ctx.settings.mtrtk_env_file)
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"marker_name": "x" * 65}})
        ok = await c.put("/api/config", json={"values": {"marker_name": "x" * 64}})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["marker_name"]
    assert "at most 64" in r.json()["detail"][0]["msg"]
    assert ok.status_code == 200
    assert read_env(ctx.settings.mtrtk_env_file)["MARKER_NAME"] == "x" * 64
    assert before.get("MARKER_NAME") is None


async def test_every_free_text_setting_is_bounded(ctx) -> None:  # type: ignore[no-untyped-def]
    """The whole list from the review, so a new unbounded field cannot slip back in."""
    long = "x" * 600
    async with client(create_app(ctx)) as c:
        for key in (
            "marker_name",
            "observer",
            "agency",
            "country",
            "antenna_type",
            "mountpoint",
            "ntrip_user",
            "active_site",
            "alert_webhook_url",
            "public_domain",
            "ntrip_url",
            "station_id",
        ):
            r = await c.put("/api/config", json={"values": {key: long}})
            assert r.status_code == 422, key
            assert any(e["loc"] == [key] for e in r.json()["detail"]), key
    assert read_env(ctx.settings.mtrtk_env_file).get("MARKER_NAME") is None


async def test_an_interpolating_value_is_422_and_writes_nothing(ctx) -> None:  # type: ignore[no-untyped-def]
    """`${` in a stored password would be resolved away at the next start - and lock the operator
    out of their own base station."""
    before = ctx.settings.mtrtk_env_file.read_text()
    async with client(create_app(ctx)) as c:
        r = await c.put("/api/config", json={"values": {"web_password": "hunter${HOME}"}})
    assert r.status_code == 422
    assert "environment interpolation is not supported in values" in r.json()["detail"]
    assert ctx.settings.mtrtk_env_file.read_text() == before


# ------------------------------------------------------------------ GET -> PUT round trip


async def test_the_whole_get_body_can_be_posted_straight_back(ctx) -> None:  # type: ignore[no-untyped-def]
    """What a form is shown is what a form submits: a GET body must PUT back as a no-op."""
    async with client(create_app(ctx)) as c:
        values = (await c.get("/api/config")).json()["values"]
        r = await c.put("/api/config", json={"values": values})
    assert r.status_code == 200
    assert r.json() == {"changed": [], "restart_required": False}


async def test_a_read_only_key_is_only_refused_when_it_would_change(ctx) -> None:  # type: ignore[no-untyped-def]
    current = str(ctx.settings.mtrtk_env_file)
    async with client(create_app(ctx)) as c:
        same = await c.put("/api/config", json={"values": {"mtrtk_env_file": current}})
        moved = await c.put("/api/config", json={"values": {"mtrtk_env_file": "/tmp/elsewhere"}})
    assert same.status_code == 200 and same.json()["changed"] == []
    assert moved.status_code == 422 and "read-only" in moved.json()["detail"]
    assert str(ctx.settings.mtrtk_env_file) == current


async def test_secret_keys_names_only_real_settings_fields(ctx) -> None:  # type: ignore[no-untyped-def]
    """`tunnel_token` is read by the compose profile, not by `Settings`: advertising it as a
    settings key made the GET body un-postable."""
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/config")).json()
    for name in ("secret_keys", "live_keys", "read_only_keys", "url_secret_keys"):
        assert set(body[name]) <= set(body["values"]), name
    assert "tunnel_token" not in body["secret_keys"]


# ------------------------------------------------------- the write path: off the loop, serialised


async def test_the_env_file_is_read_and_written_off_the_event_loop(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A read-modify-write with an fsync in it would stall the caster and the receiver reader."""
    import threading

    from mtrtk.web.api import config as config_api

    on_loop: dict[str, bool] = {}

    def watch(label: str, fn):  # type: ignore[no-untyped-def]
        def inner(*args, **kwargs):  # type: ignore[no-untyped-def]
            on_loop[label] = threading.current_thread() is threading.main_thread()
            return fn(*args, **kwargs)

        return inner

    monkeypatch.setattr(config_api, "read_env", watch("read", read_env))
    monkeypatch.setattr(config_api, "update_env", watch("write", config_api.update_env))
    async with client(create_app(ctx)) as c:
        assert (await c.get("/api/config")).status_code == 200
        put = await c.put("/api/config", json={"values": {"marker_name": "offloop"}})
    assert put.status_code == 200
    assert on_loop == {"read": False, "write": False}


async def test_two_concurrent_puts_cannot_lose_an_update(ctx, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`update_env` is a read-modify-write: two of them at once drop one of the two changes."""
    import asyncio
    import time

    from mtrtk.web.api import config as config_api

    real = config_api.update_env
    live = 0
    peak = 0

    def slow(path, updates):  # type: ignore[no-untyped-def]
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        try:
            time.sleep(0.05)  # wide enough for a second writer to climb in beside this one
            real(path, updates)
        finally:
            live -= 1

    monkeypatch.setattr(config_api, "update_env", slow)
    async with client(create_app(ctx)) as c:
        first, second = await asyncio.gather(
            c.put("/api/config", json={"values": {"marker_name": "aaa"}}),
            c.put("/api/config", json={"values": {"observer": "bbb"}}),
        )
    assert first.status_code == 200 and second.status_code == 200
    assert peak == 1  # one settings write at a time
    disk = read_env(ctx.settings.mtrtk_env_file)
    assert disk["MARKER_NAME"] == "aaa" and disk["OBSERVER"] == "bbb"


async def test_pending_never_reports_a_key_a_put_could_not_take_back(ctx) -> None:  # type: ignore[no-untyped-def]
    """`pending` is a to-do list for the UI: everything on it must be postable, and a value that
    parses to what the process is already running is not pending at all."""
    env = ctx.settings.mtrtk_env_file
    env.write_text(
        "ROLE=base\n"
        "NTRIP_PASSWORD=pw\n"
        "MTRTK_ENV_FILE=/etc/mtrtk/.env\n"  # read-only: a PUT of it would be a 422
        f"DATA_DIR={ctx.settings.data_dir}/\n"  # same path, spelt with a trailing slash
        "WEB_ALLOW_INSECURE=1\n"  # same bool, spelt as the environment spells it
        "RTCM_MSM=7\n"  # same int, as a string
        "SVIN_MIN_DURATION_S=600\n"  # the one real difference
    )
    async with client(create_app(ctx)) as c:
        body = (await c.get("/api/config")).json()
        echoed = await c.put("/api/config", json={"values": body["pending"]})
    assert body["pending"] == {"svin_min_duration_s": 600}
    assert echoed.status_code == 200 and echoed.json()["changed"] == ["svin_min_duration_s"]


async def test_the_survey_settings_are_bounded(ctx) -> None:  # type: ignore[no-untyped-def]
    """A negative or absurd survey-in is a typo, and `.env` would carry it into every restart."""
    before = read_env(ctx.settings.mtrtk_env_file)
    async with client(create_app(ctx)) as c:
        for values in (
            {"svin_min_duration_s": -5},
            {"svin_min_duration_s": 0},
            {"svin_min_duration_s": 86401},
            {"svin_acc_limit_m": 0},
            {"svin_acc_limit_m": -1.0},
            {"svin_acc_limit_m": 100.5},
        ):
            r = await c.put("/api/config", json={"values": values})
            assert r.status_code == 422, values
            assert r.json()["detail"][0]["loc"] == [next(iter(values))], values
        ok = await c.put(
            "/api/config", json={"values": {"svin_min_duration_s": 86400, "svin_acc_limit_m": 100}}
        )
    assert ok.status_code == 200
    assert read_env(ctx.settings.mtrtk_env_file)["SVIN_MIN_DURATION_S"] == "86400"
    assert before.get("SVIN_ACC_LIMIT_M") is None
