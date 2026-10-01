"""Tests for the rnx2rtkp option-file renderer.

The demo5 probe scans the executable for option names, so most tests hand it a small fake
"binary" holding exactly the keys a build would carry: stock RTKLIB 2.4.3 b34 (`apt install
rtklib`, this host and CI) lacks every `DEMO5_OPTIONS` key, demo5 v2.5.1 (the Docker image) has
them all. The last tests run against whatever real `rnx2rtkp` is on PATH, and skip without one.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from mtrtk.ppk.rtkconf import (
    BASE_OPTIONS,
    DEMO5_OPTIONS,
    parse_conf,
    render_conf,
    rnx2rtkp_available,
    rnx2rtkp_supports,
)


def test_render_contains_base_position_and_core_options(tmp_path: Path) -> None:
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(b"\x00pos1-posmode\x00out-solformat\x00")  # a stock-like binary: no demo5 keys
    text = render_conf((-26748.172, 5837156.618, 2561801.261), binary=str(fake))
    conf = parse_conf(text)
    assert conf["pos1-posmode"] == "kinematic"
    assert conf["pos1-soltype"] == "combined"
    assert conf["pos1-navsys"] == "45"
    assert conf["ant2-postype"] == "xyz"
    assert conf["ant2-pos1"] == "-26748.1720"
    assert conf["ant2-pos2"] == "5837156.6180"
    assert conf["ant2-pos3"] == "2561801.2610"
    assert conf["pos2-gloarmode"] == "on"
    assert conf["out-solformat"] == "llh"
    assert conf["out-timesys"] == "gpst"
    assert "pos2-arfilter" not in conf  # demo5-only key omitted for a stock binary


def test_render_includes_demo5_keys_when_binary_knows_them(tmp_path: Path) -> None:
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(b"\x00pos2-arfilter\x00pos2-varholdamb\x00")
    conf = parse_conf(render_conf((1.0, 2.0, 3.0), binary=str(fake)))
    assert conf["pos2-arfilter"] == "on"
    assert conf["pos2-varholdamb"] == "0.1"
    assert conf["pos2-minfixsats"] == "4"
    assert set(DEMO5_OPTIONS) <= set(conf)


def test_overrides_qzss_and_glonass_mode(tmp_path: Path) -> None:
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(b"")
    conf = parse_conf(
        render_conf(
            (1.0, 2.0, 3.0),
            overrides={"pos1-elmask": "20", "custom-key": "x"},
            glonass_ar="autocal",
            include_qzss=True,
            binary=str(fake),
        )
    )
    assert conf["pos1-elmask"] == "20"
    assert conf["custom-key"] == "x"
    assert conf["pos2-gloarmode"] == "autocal"
    assert conf["pos1-navsys"] == "61"


def test_parse_conf_ignores_comments_and_blank_lines() -> None:
    text = "# comment\npos1-posmode =kinematic # inline\n\nout-solformat= llh\n"
    assert parse_conf(text) == {"pos1-posmode": "kinematic", "out-solformat": "llh"}


def test_supports_matches_whole_option_names_only(tmp_path: Path) -> None:
    # demo5 carries `pos2-arthres1`; that must not make `pos2-arthres` look present, nor the
    # other way round.
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(b"\x00pos2-arthres1\x00xpos2-arfilter\x00")
    assert rnx2rtkp_supports("pos2-arthres1", binary=str(fake)) is True
    assert rnx2rtkp_supports("pos2-arthres", binary=str(fake)) is False
    assert rnx2rtkp_supports("pos2-arthres12", binary=str(fake)) is False


def test_available_and_supports_real_binary() -> None:
    assert rnx2rtkp_available() == (shutil.which("rnx2rtkp") is not None)
    if rnx2rtkp_available():
        assert rnx2rtkp_supports("pos1-posmode") is True
    assert rnx2rtkp_supports("pos1-posmode", binary="/nonexistent/rnx2rtkp") is False
    assert rnx2rtkp_available("/nonexistent/rnx2rtkp") is False


def test_base_options_are_complete() -> None:
    for key in (
        "pos1-posmode",
        "pos1-frequency",
        "pos2-armode",
        "pos2-maxage",
        "out-outstat",
        "stats-errphase",
        "ant2-maxaveep",
        "misc-timeinterp",
    ):
        assert key in BASE_OPTIONS


STOCK_LIKE = b"\x00pos1-posmode\x00pos1-frequency\x00pos2-gloarmode\x00"
DEMO5_LIKE = STOCK_LIKE + b"pos2-arfilter\x00"


def test_stock_build_gets_its_own_frequency_spelling(tmp_path: Path) -> None:
    stock = tmp_path / "stock"
    stock.write_bytes(STOCK_LIKE)
    demo5 = tmp_path / "demo5"
    demo5.write_bytes(DEMO5_LIKE)
    assert parse_conf(render_conf((1.0, 2.0, 3.0), binary=str(stock)))["pos1-frequency"] == "l1+2"
    assert parse_conf(render_conf((1.0, 2.0, 3.0), binary=str(demo5)))["pos1-frequency"] == "l1+l2"
    # An override in demo5 spelling is translated too.
    text = render_conf((1.0, 2.0, 3.0), overrides={"pos1-frequency": "l1+l2+l5"}, binary=str(stock))
    assert parse_conf(text)["pos1-frequency"] == "l1+2+3"


def test_stock_build_without_autocal_falls_back_to_off_and_says_so(tmp_path: Path) -> None:
    stock = tmp_path / "stock"
    stock.write_bytes(STOCK_LIKE)
    demo5 = tmp_path / "demo5"
    demo5.write_bytes(DEMO5_LIKE)
    text = render_conf((1.0, 2.0, 3.0), glonass_ar="autocal", binary=str(stock))
    assert parse_conf(text)["pos2-gloarmode"] == "off"
    assert "# pos2-gloarmode=autocal needs RTKLIB demo5" in text
    text = render_conf((1.0, 2.0, 3.0), glonass_ar="autocal", binary=str(demo5))
    assert parse_conf(text)["pos2-gloarmode"] == "autocal"
    assert "needs RTKLIB demo5" not in text


@pytest.mark.skipif(shutil.which("rnx2rtkp") is None, reason="no rnx2rtkp on PATH")
@pytest.mark.parametrize(("glonass_ar", "include_qzss"), [("on", False), ("autocal", True)])
def test_real_binary_accepts_every_rendered_value(
    tmp_path: Path, glonass_ar: str, include_qzss: bool
) -> None:
    # rnx2rtkp's loadopts() ignores unknown keys but prints `invalid option value KEY (file:line)`
    # for a value it cannot parse. With no input files it then exits without processing.
    conf_path = tmp_path / "f9p_ppk.conf"
    conf_path.write_text(
        render_conf(
            (-26748.172, 5837156.618, 2561801.261),
            glonass_ar=glonass_ar,
            include_qzss=include_qzss,
        )
    )
    done = subprocess.run(
        ["rnx2rtkp", "-k", str(conf_path), "-o", str(tmp_path / "out.pos")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "invalid option" not in done.stderr + done.stdout
