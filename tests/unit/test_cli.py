import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from mtrtk.cli import main


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "mtrtk, version 0.1.0" in result.output


NOISY = ("aiosqlite", "asyncio", "httpx", "httpcore")


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
