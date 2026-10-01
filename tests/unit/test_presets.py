import pytest

from mtrtk.rinex.presets import PRESETS, resolve_options


def test_preset_catalogue() -> None:
    assert set(PRESETS) == {"csrs-ppp", "auspos", "opus", "generic"}
    assert all(p.id == key for key, p in PRESETS.items())
    csrs = PRESETS["csrs-ppp"]
    assert csrs.version == "3.04" and csrs.interval_s == 30
    assert csrs.hatanaka and csrs.gzip and csrs.exclude_systems == ()
    auspos = PRESETS["auspos"]
    assert auspos.version == "3.04" and auspos.interval_s == 30
    assert auspos.exclude_systems == () and auspos.gzip and not auspos.hatanaka
    opus = PRESETS["opus"]
    assert opus.version == "2.11" and opus.interval_s == 30
    assert opus.exclude_systems == ("R", "E", "J", "C", "S", "I")
    assert not opus.hatanaka and not opus.gzip
    generic = PRESETS["generic"]
    assert generic.version == "3.04" and generic.interval_s is None
    assert generic.exclude_systems == () and not generic.hatanaka and not generic.gzip
    assert PRESETS["generic"].adjustable is True and PRESETS["csrs-ppp"].adjustable is False
    assert all(p.service_url.startswith("https://") or p.id == "generic" for p in PRESETS.values())


def test_resolve_options_generic_overrides() -> None:
    r = resolve_options("generic", interval_s=5, hatanaka=True, gzip=True)
    assert r.interval_s == 5 and r.hatanaka and r.gzip and r.version == "3.04"
    assert r.preset is PRESETS["generic"]
    r2 = resolve_options("generic")
    assert r2.interval_s is None and not r2.hatanaka and not r2.gzip


def test_resolve_options_fixed_presets_reject_overrides() -> None:
    assert resolve_options("csrs-ppp").interval_s == 30
    with pytest.raises(ValueError, match="fixed"):
        resolve_options("csrs-ppp", interval_s=1)
    with pytest.raises(ValueError, match="fixed"):
        resolve_options("opus", gzip=True)
    with pytest.raises(KeyError):
        resolve_options("nope")
