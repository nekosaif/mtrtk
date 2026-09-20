import stat
from pathlib import Path

import pytest

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


# ------------------------------------------------------------------ values a bare line would eat

# Every one of these is mangled by python-dotenv - which is what pydantic-settings reads `.env`
# with - unless the writer quotes it: a bare value ends at the first whitespace-then-`#` and is
# stripped, and a leading quote starts a quoted value.
NASTY = ["hunter2 #1", " padded ", 'a"b', "x=y", "#lead", r"C:\path\to", "it's", 'say "hi" # no']


def test_values_round_trip_through_read_env(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    for i, value in enumerate(NASTY):
        update_env(p, {f"K{i}": value})
    got = read_env(p)
    assert [got[f"K{i}"] for i in range(len(NASTY))] == NASTY


def test_values_round_trip_through_settings(tmp_path: Path) -> None:
    """The reader that matters is pydantic-settings', not this module's."""
    p = tmp_path / ".env"
    p.write_text("NTRIP_PASSWORD=seed\n")
    for value in NASTY:
        update_env(p, {"WEB_PASSWORD": value, "MARKER_NAME": value})
        loaded = Settings(_env_file=p)
        assert loaded.web_password == value
        assert loaded.marker_name == value


def test_a_newline_in_a_value_is_refused(tmp_path: Path) -> None:
    """A `.env` file is line-oriented: a newline would smuggle in a second assignment."""
    p = tmp_path / ".env"
    p.write_text("ROLE=base\n")
    with pytest.raises(ValueError, match="newline"):
        to_env_value("X\nROLE=rover")
    with pytest.raises(ValueError, match="newline"):
        update_env(p, {"MARKER_NAME": "X\rROLE=rover"})
    assert p.read_text() == "ROLE=base\n"  # refused before anything was written


def test_updating_a_quoted_value_leaves_no_dangling_quote(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text('QUOTED="a # b"   # note\n')
    update_env(p, {"QUOTED": "zzz"})
    assert p.read_text().splitlines() == ["QUOTED=zzz   # note"]
    assert read_env(p) == {"QUOTED": "zzz"}


def test_update_env_rewrites_the_last_duplicate_and_drops_earlier_ones(tmp_path: Path) -> None:
    """python-dotenv is last-wins, so only the last occurrence is the one in force."""
    p = tmp_path / ".env"
    p.write_text("ROLE=rover\nBAUD=9600\nROLE=base   # the live one\nBAUD=115200\n")
    update_env(p, {"ROLE": "rover"})
    assert p.read_text().splitlines() == [
        "BAUD=9600",
        "ROLE=rover   # the live one",
        "BAUD=115200",  # a duplicate of a key we did not touch is left alone
    ]
    assert read_env(p)["ROLE"] == "rover"


def test_update_env_writes_through_a_symlinked_env(tmp_path: Path) -> None:
    """`os.replace` over a symlink would replace the link itself and orphan the real file."""
    real = tmp_path / "real.env"
    real.write_text("ROLE=base\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    update_env(link, {"ROLE": "rover"})
    assert link.is_symlink()
    assert read_env(real)["ROLE"] == "rover"
    assert sorted(f.name for f in tmp_path.iterdir()) == [".env", "real.env"]


# --------------------------------------------------------- python-dotenv's own interpolation

# python-dotenv resolves `${VAR}` in *every* quoting form - bare, "double", 'single', and even
# behind a backslash - so no amount of quoting can carry these through. Written once, they read
# back as whatever the environment happened to hold at the next start.
INTERPOLATING = ["${HOME}", "a${X}b", '"${HOME}"', r"\${HOME}", "${}", "${NOTHING_DEFINES_THIS}"]
# `${` with no closing brace is not interpolated, but it is refused all the same: "no `${` in a
# value" is a rule an operator can hold in their head, and dotenv's grammar is not.
CONSERVATIVE = ["pass${word", "${"]
# A bare `$` is not interpolation at all: only the braced form is resolved, so these must survive.
SAFE_DOLLARS = ["$HOME", "100$", "a$b", "$", "$$"]


def test_values_match_dotenv_values_over_the_whole_corpus(tmp_path: Path) -> None:
    """The writer is only correct if the reader pydantic-settings actually uses agrees with it."""
    from dotenv import dotenv_values

    p = tmp_path / ".env"
    corpus = [*NASTY, *SAFE_DOLLARS]
    for i, value in enumerate(corpus):
        update_env(p, {f"K{i}": value})
    loaded = dotenv_values(p)
    assert [loaded[f"K{i}"] for i in range(len(corpus))] == corpus
    assert loaded == read_env(p)  # and our own reader agrees with it, key for key


def test_an_interpolating_value_is_refused_rather_than_written(tmp_path: Path) -> None:
    """The one divergence in the corpus: refused outright instead of written and corrupted."""
    p = tmp_path / ".env"
    p.write_text("ROLE=base\n")
    for value in [*INTERPOLATING, *CONSERVATIVE]:
        with pytest.raises(ValueError, match="interpolation"):
            to_env_value(value)
        with pytest.raises(ValueError, match="interpolation"):
            update_env(p, {"WEB_PASSWORD": value})
    assert p.read_text() == "ROLE=base\n"  # refused before anything was written


def test_what_is_refused_is_what_dotenv_would_have_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every refused value really does come back as something else, however it is quoted."""
    from dotenv import dotenv_values

    monkeypatch.setenv("HOME", "/home/somebody-else")
    monkeypatch.setenv("X", "!")
    p = tmp_path / ".env"
    for value in INTERPOLATING:
        p.write_text(f"K={_raw_encode(value)}\n")
        assert dotenv_values(p)["K"] != value, value


def _raw_encode(value: str) -> str:
    """What `update_env` *would* have written, with the interpolation guard taken back out."""
    from mtrtk.web.envfile import _needs_quotes

    if not _needs_quotes(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
