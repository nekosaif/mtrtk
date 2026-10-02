import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

import mtrtk
from mtrtk.cli import main


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert f"mtrtk, version {mtrtk.__version__}" in result.output


NOISY = ("aiosqlite", "asyncio", "httpx", "httpcore", "websockets")


@pytest.fixture
def _restore_log_levels():
    names = ("", "mtrtk", *NOISY)
    before = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in before.items():
        logging.getLogger(name).setLevel(level)


def test_verbose_keeps_the_noisy_libraries_at_info(_restore_log_levels) -> None:
    """`-v` is for mtrtk's own debug output; aiosqlite alone logs every statement it runs."""
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.NOTSET)
    assert CliRunner().invoke(main, ["sites", "list"]).exit_code == 0
    assert [logging.getLogger(n).level for n in NOISY] == [logging.NOTSET] * len(NOISY)
    result = CliRunner().invoke(main, ["-v", "sites", "list"])
    assert result.exit_code == 0, result.output
    for name in NOISY:
        assert logging.getLogger(name).level == logging.INFO, name


def test_load_settings_reads_the_env_file_named_by_mtrtk_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI reads the same file the config API writes, so a pinned MTRTK_ENV_FILE keeps the
    suite (and a probe) off the developer's own .env."""
    from mtrtk.cli import _load_settings

    env = tmp_path / "pinned.env"
    env.write_text("MARKER_NAME=TMPX\nNTRIP_PASSWORD=pw\n")
    monkeypatch.setenv("MTRTK_ENV_FILE", str(env))
    monkeypatch.delenv("MARKER_NAME", raising=False)
    settings = _load_settings()
    assert settings.marker_name == "TMPX"
    assert settings.mtrtk_env_file == env


def _stop_before_the_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: str) -> None:
    """Point the CLI at a temp .env and make `Daemon(...)` fail at once, so `run` gets as far
    as applying the settings and no further."""
    import mtrtk.daemon

    def no_daemon(_settings: object) -> None:
        raise RuntimeError("stopped by the test")

    env_file = tmp_path / ".env"
    env_file.write_text(f"NTRIP_PASSWORD=pw\nDATA_DIR={tmp_path}\n{env}")
    monkeypatch.setenv("MTRTK_ENV_FILE", str(env_file))
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setattr(mtrtk.daemon, "Daemon", no_daemon)


def test_run_applies_log_level(
    _restore_log_levels, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOG_LEVEL sets the daemon's root logger; without it the daemon logs at INFO."""
    _stop_before_the_daemon(monkeypatch, tmp_path, "LOG_LEVEL=ERROR\n")
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 1 and "stopped by the test" in result.output
    assert logging.getLogger().level == logging.ERROR


def test_run_logs_at_info_without_log_level(
    _restore_log_levels, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No LOG_LEVEL in .env: INFO. Under pytest the root logger starts at WARNING, so this only
    passes if the default is applied too, not just a level someone set."""
    logging.getLogger().setLevel(logging.WARNING)
    _stop_before_the_daemon(monkeypatch, tmp_path, "")
    assert CliRunner().invoke(main, ["run"]).exit_code == 1
    assert logging.getLogger().level == logging.INFO


def test_log_level_debug_keeps_the_noisy_libraries_at_info(
    _restore_log_levels, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOG_LEVEL=DEBUG is `-v` by configuration, so it quiets the same chatty libraries."""
    for name in NOISY:
        logging.getLogger(name).setLevel(logging.NOTSET)
    _stop_before_the_daemon(monkeypatch, tmp_path, "LOG_LEVEL=DEBUG\n")
    assert CliRunner().invoke(main, ["run"]).exit_code == 1
    assert logging.getLogger().level == logging.DEBUG
    for name in NOISY:
        assert logging.getLogger(name).level == logging.INFO, name


def test_verbose_flag_wins_over_log_level(
    _restore_log_levels, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mtrtk -v run` is a one-off debugging session: it beats whatever .env says."""
    _stop_before_the_daemon(monkeypatch, tmp_path, "LOG_LEVEL=ERROR\n")
    assert CliRunner().invoke(main, ["-v", "run"]).exit_code == 1
    assert logging.getLogger().level == logging.DEBUG


@pytest.mark.parametrize(
    ("unanswered", "hint", "not_hint"),
    [
        (True, "RECEIVER_ACK_TIMEOUT_S", "RECEIVER_STRICT=0"),
        (False, "RECEIVER_STRICT=0", "RECEIVER_ACK_TIMEOUT_S"),
    ],
)
def test_a_first_start_that_hears_nothing_points_at_the_link(
    _restore_log_levels,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unanswered: bool,
    hint: str,
    not_hint: str,
) -> None:
    """Found by review: a readback that got no answers said "set RECEIVER_STRICT=0", which only
    makes the controller reconnect for ever; the fix is the link or RECEIVER_ACK_TIMEOUT_S."""
    from types import SimpleNamespace

    import mtrtk.daemon
    from mtrtk.core.bus import Bus
    from mtrtk.core.receiver import ProfileError, ProfileUnanswered
    from mtrtk.core.statestore import StateStore

    _stop_before_the_daemon(monkeypatch, tmp_path, "")

    async def fail() -> None:
        if unanswered:
            raise ProfileUnanswered("configuration verification got no answers after 3 attempts")
        raise ProfileError("receiver rejected core config keys: ['CFG_X']")

    bus = Bus()
    monkeypatch.setattr(
        mtrtk.daemon,
        "Daemon",
        lambda _settings: SimpleNamespace(bus=bus, store=StateStore(bus), run=fail),
    )
    result = CliRunner().invoke(main, ["run"])
    assert result.exit_code == 1
    assert hint in result.output and not_hint not in result.output
