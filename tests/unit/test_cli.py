import logging

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
