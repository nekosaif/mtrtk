"""PPK request validation and pure helpers: no RTKLIB needed, so these always run."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from mtrtk.ppk.pipeline import BaseSource, PpkRequest, RoverSource, fetch_chunks
from mtrtk.rinex.rinexhdr import sniff_format


def test_sniff_format_refuses_what_it_cannot_convert(tmp_path: Path) -> None:
    crx = tmp_path / "x.crx"
    crx.write_text(f"{'1.0':<20}{'COMPACT RINEX FORMAT':<40}CRINEX VERS   / TYPE\n")
    assert sniff_format(crx) == "crinex"
    gz = tmp_path / "x.gz"
    gz.write_bytes(b"\x1f\x8b\x08\x00rest")
    assert sniff_format(gz) == "gzip"
    nav = tmp_path / "x.nav"
    nav.write_text(f"{'     3.04':<20}{'N: GNSS NAV DATA':<40}RINEX VERSION / TYPE\n")
    assert sniff_format(nav) == "rinex-nav"


def test_request_validation() -> None:

    naive = datetime(2026, 1, 1)
    with pytest.raises(ValidationError, match="timezone"):
        RoverSource(kind="window", start=naive, end=naive + timedelta(hours=1))
    aware = naive.replace(tzinfo=UTC)
    with pytest.raises(ValidationError, match="before"):
        RoverSource(kind="window", start=aware, end=aware)
    with pytest.raises(ValidationError, match="session_id"):
        RoverSource(kind="session")
    with pytest.raises(ValidationError, match="url"):
        BaseSource(kind="remote")
    with pytest.raises(ValidationError, match="url"):
        BaseSource(kind="remote", url="ftp://base")
    assert "password" not in BaseSource(kind="remote", url="http://b", password="x").model_dump()


def test_fetch_chunks_are_whole_hours_of_at_most_48() -> None:
    t0 = datetime(2026, 9, 18, 10, 30, tzinfo=UTC)
    chunks = fetch_chunks(t0, t0 + timedelta(hours=100))
    assert chunks[0][0] == datetime(2026, 9, 18, 10, tzinfo=UTC)
    assert chunks[-1][1] == t0 + timedelta(hours=100)
    assert all(b - a <= timedelta(hours=48) for a, b in chunks)
    assert all(a.minute == 0 for a, _ in chunks)
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))
    assert len(fetch_chunks(t0, t0 + timedelta(minutes=5))) == 1


@pytest.mark.parametrize(
    "key",
    [
        "out-solformat",
        "out-timesys",
        "out-timeform",
        "out-degform",
        "out-outhead",
        "out-height",
        "out-fieldsep",
    ],
)
def test_overrides_cannot_change_the_solution_layout(key: str) -> None:
    """The .pos parser and the camera-event times rely on these; an override would corrupt
    the track (ECEF read as lat/lon) or shift every event (UTC read as GPST)."""
    rover = RoverSource(kind="upload", path=Path("r.ubx"))
    with pytest.raises(ValidationError, match=key):
        PpkRequest(rover=rover, base=BaseSource(kind="local"), conf_overrides={key: "x"})
    ok = PpkRequest(
        rover=rover, base=BaseSource(kind="local"), conf_overrides={"pos1-elmask": "12"}
    )
    assert ok.conf_overrides == {"pos1-elmask": "12"}
