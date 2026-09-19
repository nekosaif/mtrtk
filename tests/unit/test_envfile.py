import stat
from pathlib import Path

from mtrtk.config import BaseMode, Settings
from mtrtk.web.envfile import read_env, to_env_value, update_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_read_env_parses_values_and_ignores_comments(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        "# header\nROLE=base   # trailing\nNTRIP_PASSWORD=\n"
        'LOG_MESSAGES=RXM-RAWX,RXM-SFRBX\nQUOTED="a # b"\n\n'
    )
    assert read_env(p) == {
        "ROLE": "base",
        "NTRIP_PASSWORD": "",
        "LOG_MESSAGES": "RXM-RAWX,RXM-SFRBX",
        "QUOTED": "a # b",
    }


def test_update_env_preserves_layout_and_appends(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# header\nROLE=base   # role comment\nBAUD=115200\n")
    update_env(p, {"ROLE": "rover", "NEW_KEY": "x y"})
    text = p.read_text()
    assert text.splitlines() == [
        "# header",
        "ROLE=rover   # role comment",
        "BAUD=115200",
        "NEW_KEY=x y",
    ]
    assert not (tmp_path / ".env.tmp").exists()


def test_update_env_creates_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "sub" / ".env"
    update_env(p, {"A": "1"})
    assert read_env(p) == {"A": "1"}


def test_to_env_value() -> None:
    assert to_env_value(BaseMode.FIXED) == "fixed"
    assert to_env_value(["a", "b"]) == "a,b"
    assert to_env_value(True) == "1" and to_env_value(False) == "0"
    assert to_env_value(None) == "" and to_env_value(5.5) == "5.5"
    assert to_env_value(Path("/data")) == "/data"


def test_update_env_moves_a_trailing_comment_above_an_emptied_value(tmp_path: Path) -> None:
    """`.env.example`'s rule: an empty value must not carry a trailing comment."""
    p = tmp_path / ".env"
    p.write_text("# header\nACTIVE_SITE=roof   # name of a saved site\nBAUD=115200\n")
    update_env(p, {"ACTIVE_SITE": ""})
    assert p.read_text().splitlines() == [
        "# header",
        "# name of a saved site",
        "ACTIVE_SITE=",
        "BAUD=115200",
    ]
    assert read_env(p)["ACTIVE_SITE"] == ""


def test_update_env_keeps_the_file_mode_and_leaves_no_temp_behind(tmp_path: Path) -> None:
    """`.env` holds the NTRIP and web passwords: the atomic replace must not widen it to 0644."""
    p = tmp_path / ".env"
    p.write_text("NTRIP_PASSWORD=old\n")
    p.chmod(0o600)
    update_env(p, {"NTRIP_PASSWORD": "new"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [f.name for f in tmp_path.iterdir()] == [".env"]


def test_update_env_creates_a_private_file(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    update_env(p, {"NTRIP_PASSWORD": "new"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_update_env_keeps_env_example_loadable(tmp_path: Path) -> None:
    """A rewritten `.env` must still parse: layout preservation is the point of this module."""
    p = tmp_path / ".env"
    p.write_text((REPO_ROOT / ".env.example").read_text())
    update_env(p, {"STATION_ID": "BASE", "ACTIVE_SITE": "roof", "NTRIP_PASSWORD": ""})
    s = Settings(_env_file=p)
    assert s.station_id == "BASE"
    assert s.active_site == "roof"
    assert s.ntrip_anonymous is True
    assert s.log_messages[0] == "RXM-RAWX"  # untouched keys still read back
