"""Tests for the rnx2rtkp option-file renderer.

The demo5 probe scans the executable for option names, so most tests hand it a small fake
"binary" holding exactly the keys a build would carry: stock RTKLIB 2.4.3 b34 (`apt install
rtklib`, this host and CI) lacks every `DEMO5_OPTIONS` key, demo5 v2.5.1 (the Docker image) has
them all. The last tests run against whatever real `rnx2rtkp` is on PATH, and skip without one.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mtrtk.ppk.rtkconf import (
    BASE_OPTIONS,
    DEMO5_OPTIONS,
    detect_build,
    parse_conf,
    render_conf,
    render_conf_with_notes,
    rnx2rtkp_available,
    rnx2rtkp_supports,
)

XYZ = (-26748.172, 5837156.618, 2561801.261)


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
    conf = parse_conf(render_conf(XYZ, binary=str(fake)))
    assert conf["pos2-arfilter"] == "on"
    assert conf["pos2-varholdamb"] == "0.1"
    assert conf["pos2-minfixsats"] == "4"
    assert set(DEMO5_OPTIONS) <= set(conf)


def test_overrides_qzss_and_glonass_mode(tmp_path: Path) -> None:
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(b"")
    conf = parse_conf(
        render_conf(
            XYZ,
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
    fake.write_bytes(b"\x00pos2-arthres1\x00xpos2-arfilter\x00-pos2-armode\x00")
    assert rnx2rtkp_supports("pos2-arthres1", binary=str(fake)) is True
    assert rnx2rtkp_supports("pos2-arthres", binary=str(fake)) is False
    assert rnx2rtkp_supports("pos2-arthres12", binary=str(fake)) is False
    assert rnx2rtkp_supports("pos2-arfilter", binary=str(fake)) is False
    assert rnx2rtkp_supports("pos2-armode", binary=str(fake)) is False


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


@pytest.mark.parametrize("contents", [STOCK_LIKE, DEMO5_LIKE, b'#!/bin/sh\nexec rnx2rtkp "$@"\n'])
def test_frequency_is_written_as_the_enum_every_build_accepts(
    tmp_path: Path, contents: bytes
) -> None:
    # demo5 rejects stock's `l1+2` and stock rejects demo5's `l1+l2`; both read `2` as L1+L2, so
    # even a build that is not recognised (here a wrapper script) keeps dual frequency.
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(contents)
    assert parse_conf(render_conf(XYZ, binary=str(fake)))["pos1-frequency"] == "2"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("l1", "1"),
        ("l1+l2", "2"),
        ("l1+2", "2"),
        ("l1+l2+l5", "3"),
        ("l1+2+3", "3"),
        ("l1+l2+l5+l6", "4"),
        ("l1+2+3+4", "4"),
        ("3", "3"),
    ],
)
@pytest.mark.parametrize("contents", [STOCK_LIKE, DEMO5_LIKE])
def test_frequency_override_in_either_spelling_becomes_the_enum(
    tmp_path: Path, contents: bytes, override: str, expected: str
) -> None:
    fake = tmp_path / "rnx2rtkp"
    fake.write_bytes(contents)
    text = render_conf(XYZ, overrides={"pos1-frequency": override}, binary=str(fake))
    assert parse_conf(text)["pos1-frequency"] == expected


@pytest.mark.parametrize("mode", ["autocal", "fix-and-hold"])
def test_stock_build_without_autocal_falls_back_to_off_and_says_so(
    tmp_path: Path, mode: str
) -> None:
    stock = tmp_path / "stock"
    stock.write_bytes(STOCK_LIKE)
    demo5 = tmp_path / "demo5"
    demo5.write_bytes(DEMO5_LIKE)
    text, notes = render_conf_with_notes(XYZ, glonass_ar=mode, binary=str(stock))
    assert parse_conf(text)["pos2-gloarmode"] == "off"
    assert f"# pos2-gloarmode={mode} needs RTKLIB demo5" in text
    assert notes == [f"pos2-gloarmode={mode} needs RTKLIB demo5; this build gets off"]
    assert render_conf(XYZ, glonass_ar=mode, binary=str(stock)) == text
    text, notes = render_conf_with_notes(XYZ, glonass_ar=mode, binary=str(demo5))
    assert parse_conf(text)["pos2-gloarmode"] == mode
    assert "needs RTKLIB demo5" not in text
    assert notes == []


def test_detect_build(tmp_path: Path) -> None:
    stock = tmp_path / "stock"
    stock.write_bytes(STOCK_LIKE)
    demo5 = tmp_path / "demo5"
    demo5.write_bytes(DEMO5_LIKE)
    wrapper = tmp_path / "wrapper"
    wrapper.write_bytes(b'#!/bin/sh\nexec /opt/rtklib/rnx2rtkp "$@"\n')
    assert detect_build(str(stock)) == "stock"
    assert detect_build(str(demo5)) == "demo5"
    assert detect_build(str(wrapper)) == "unknown"
    assert detect_build(str(tmp_path / "missing")) == "unknown"
    _, notes = render_conf_with_notes(XYZ, binary=str(wrapper))
    assert len(notes) == 1 and "not recognised" in notes[0]


def test_probe_does_not_remember_a_binary_that_was_missing(tmp_path: Path) -> None:
    # A daemon that probed before rtklib was installed must see the install without a restart.
    binary = tmp_path / "rnx2rtkp"
    assert detect_build(str(binary)) == "unknown"
    binary.write_bytes(STOCK_LIKE)
    assert detect_build(str(binary)) == "stock"
    assert (
        parse_conf(render_conf(XYZ, glonass_ar="autocal", binary=str(binary)))["pos2-gloarmode"]
        == "off"
    )


def test_probe_rereads_a_binary_replaced_in_place(tmp_path: Path) -> None:
    binary = tmp_path / "rnx2rtkp"
    binary.write_bytes(STOCK_LIKE)
    assert detect_build(str(binary)) == "stock"
    binary.write_bytes(DEMO5_LIKE)
    st = binary.stat()
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert detect_build(str(binary)) == "demo5"


def test_available_needs_an_executable_file(tmp_path: Path) -> None:
    f = tmp_path / "rnx2rtkp"
    f.write_bytes(STOCK_LIKE)
    f.chmod(0o644)
    assert rnx2rtkp_available(str(f)) is False
    f.chmod(0o755)
    assert rnx2rtkp_available(str(f)) is True


@pytest.mark.parametrize(
    "base",
    [
        (math.nan, 0.0, 0.0),
        (math.inf, 1.0, 1.0),
        (0.0, 0.0, 0.0),
        (23.8, 90.4, 10.0),  # latitude, longitude, height passed by mistake
        (1.0, 2.0, 3.0),
    ],
)
def test_base_position_must_be_finite_ecef(
    tmp_path: Path, base: tuple[float, float, float]
) -> None:
    with pytest.raises(ValueError, match="base position"):
        render_conf(base, binary=str(tmp_path / "rnx2rtkp"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"pos1-elmask": "10\nfile-satantfile=/etc/shadow"},
        {"pos1-elmask": "10\r"},
        {"pos1-elmask": "10\x00"},
        {"pos1-elmask": "10 # twenty"},
        {"pos1-elmask": "x" * 2000},
        {"pos1-elmask=1\nfoo": "1"},
        {"pos1 elmask": "1"},
        {"": "1"},
    ],
)
def test_malformed_override_raises(tmp_path: Path, overrides: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="override"):
        render_conf(XYZ, overrides=overrides, binary=str(tmp_path / "rnx2rtkp"))


def test_well_formed_overrides_pass(tmp_path: Path) -> None:
    overrides = {"pos1-snrmask_r": "on", "file-rcvantfile": "/data/igs20.atx", "ant2-pos1": "1.5"}
    conf = parse_conf(render_conf(XYZ, overrides=overrides, binary=str(tmp_path / "rnx2rtkp")))
    assert {k: conf[k] for k in overrides} == overrides


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
    out = done.stderr + done.stdout
    assert "invalid option" not in out
    # Printed only after loadopts() read the file, so the values above were really checked.
    assert "no input file" in out
