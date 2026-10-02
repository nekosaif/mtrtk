"""`obs_span` when the header gives no TIME OF LAST OBS: the epoch records decide."""

from datetime import datetime
from pathlib import Path

from mtrtk.rinex.rinexhdr import obs_span


def _hdr(label: str, body: str = "") -> str:
    return f"{body:<60}{label}\n"


def test_a_rinex2_rover_without_time_of_last_obs(tmp_path: Path) -> None:
    path = tmp_path / "rover.26o"
    path.write_text(
        _hdr("RINEX VERSION / TYPE", "     2.11           OBSERVATION DATA    G (GPS)")
        + _hdr("# / TYPES OF OBSERV", "     2    C1    L1")
        + _hdr("TIME OF FIRST OBS", "  2026     9    18    10     0    0.0000000     GPS")
        + _hdr("END OF HEADER")
        + " 26  9 18 10  0  0.0000000  0  2G05G13\n"
        + "  21234567.123 7 111587654.12345\n"
        + "  22234567.123 7 116587654.12345\n"
        + " 26  9 18 10  0  1.0000000  0  2G05G13\n"
        + "  21234567.123 7 111587654.12345\n"
        + "  22234567.123 7 116587654.12345\n"
        + " 26  9 18 10  0  2.5000000  0  1G05\n"
        + "  21234567.123 7 111587654.12345\n"
    )
    assert obs_span(path) == (
        datetime(2026, 9, 18, 10, 0, 0),
        datetime(2026, 9, 18, 10, 0, 2, 500000),
    )


def test_a_rinex2_file_with_no_first_obs_either(tmp_path: Path) -> None:
    path = tmp_path / "rover.99o"
    path.write_text(
        _hdr("RINEX VERSION / TYPE", "     2.11           OBSERVATION DATA    G (GPS)")
        + _hdr("END OF HEADER")
        + " 99 12 31 23 59 59.0000000  0  1G05\n"
        + "  21234567.123 7\n"
        + " 00  1  1  0  0  0.0000000  0  1G05\n"
        + "  21234567.123 7\n"
    )
    assert obs_span(path) == (datetime(1999, 12, 31, 23, 59, 59), datetime(2000, 1, 1))
