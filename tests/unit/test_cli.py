from click.testing import CliRunner

from mtrtk.cli import main


def test_version_flag() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "mtrtk, version 0.1.0" in result.output
