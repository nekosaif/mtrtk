"""Receiver configuration profiles as CFG-VALSET key/value lists (pyubx2 key names)."""

from __future__ import annotations

from dataclasses import dataclass, field

from pyubx2 import SET_LAYER_BBR, SET_LAYER_FLASH, SET_LAYER_RAM
from pyubx2.ubxhelpers import val2sphp

from mtrtk.config import Settings

# Most CFG keys are numeric; bitfield (X-type) keys carry their raw bytes, which is what
# pyubx2 requires when it serialises a VALSET and what a VALGET readback returns.
CfgValue = int | bytes
CfgItems = list[tuple[str, CfgValue]]

LAYERS_RAM = SET_LAYER_RAM
LAYERS_ALL = SET_LAYER_RAM | SET_LAYER_BBR | SET_LAYER_FLASH
MAX_KEYS_PER_VALSET = 64

RTCM_MSM7 = ("1077", "1087", "1097", "1127")
RTCM_MSM4 = ("1074", "1084", "1094", "1124")

# UBX messages both roles emit on USB (value = output every N navigation epochs)
COMMON_MSGOUT: dict[str, int] = {
    "NAV_PVT": 1,
    "NAV_SAT": 1,
    "NAV_SIG": 1,
    "NAV_DOP": 1,
    "NAV_STATUS": 1,
    "NAV_CLOCK": 1,
    "NAV_TIMEGPS": 1,
    "NAV_TIMEUTC": 1,
    "NAV_HPPOSLLH": 1,
    "NAV_HPPOSECEF": 1,
    "NAV_EOE": 1,
    "RXM_RAWX": 1,
    "RXM_SFRBX": 1,
    "MON_HW": 1,
    "MON_RF": 1,
}

# L1/L2 signal plan for HPG 1.13 (no L5 / E5a / B2a keys — those need newer firmware)
SIGNALS_L1_L2: CfgItems = [
    (f"CFG_SIGNAL_{name}_ENA", 1)
    for name in (
        "GPS",
        "GPS_L1CA",
        "GPS_L2C",
        "GLO",
        "GLO_L1",
        "GLO_L2",
        "GAL",
        "GAL_E1",
        "GAL_E5B",
        "BDS",
        "BDS_B1",
        "BDS_B2",
        "QZSS",
        "QZSS_L1CA",
        "QZSS_L2C",
    )
]
SIGNALS_L1_L2 += [("CFG_SIGNAL_SBAS_ENA", 0)]

# Features whose keys may be NAK'd on old firmware. Each is its own VALSET.
OPTIONAL_FEATURES: dict[str, CfgItems] = {
    "MON-SPAN": [("CFG_MSGOUT_UBX_MON_SPAN_USB", 5)],
    "MON-COMMS": [("CFG_MSGOUT_UBX_MON_COMMS_USB", 5)],
    "NAV-TIMELS": [("CFG_MSGOUT_UBX_NAV_TIMELS_USB", 10)],
}


@dataclass
class Profile:
    name: str
    # must be accepted (receiver_strict) — applied in <=64-key chunks
    core: CfgItems
    # applied only when the readback differs (changing signals restarts the engine)
    signals: CfgItems
    # feature -> keys; a NAK disables that feature
    optional: dict[str, CfgItems] = field(default_factory=dict)
    nav_hz: int = 1


def _optional_features() -> dict[str, CfgItems]:
    """A per-profile copy: dropping a NAK'd feature must not edit the template."""
    return {name: list(items) for name, items in OPTIONAL_FEATURES.items()}


def _msgout(names: dict[str, int]) -> CfgItems:
    return [(f"CFG_MSGOUT_UBX_{name}_USB", rate) for name, rate in names.items()]


def _common_core(meas_ms: int, dynmodel: int, rtcm_out: bool) -> CfgItems:
    items: CfgItems = [
        ("CFG_RATE_MEAS", meas_ms),
        ("CFG_RATE_NAV", 1),
        ("CFG_RATE_TIMEREF", 1),
        ("CFG_USBOUTPROT_UBX", 1),
        ("CFG_USBOUTPROT_NMEA", 0),
        ("CFG_USBOUTPROT_RTCM3X", 1 if rtcm_out else 0),
        ("CFG_USBINPROT_UBX", 1),
        ("CFG_USBINPROT_NMEA", 0),
        ("CFG_USBINPROT_RTCM3X", 0 if rtcm_out else 1),
        ("CFG_NAVSPG_DYNMODEL", dynmodel),
        ("CFG_NAVSPG_INFIL_MINELEV", 10),
        ("CFG_INFMSG_UBX_USB", b"\x00"),  # X1 bitfield: no UBX-INF messages on USB
        ("CFG_ITFM_ENABLE", 1),
        ("CFG_ITFM_ANTSETTING", 2),
    ]
    return items + _msgout(COMMON_MSGOUT)


def base_profile(settings: Settings) -> Profile:
    enabled = RTCM_MSM7 if settings.rtcm_msm == 7 else RTCM_MSM4
    disabled = RTCM_MSM4 if settings.rtcm_msm == 7 else RTCM_MSM7
    core = _common_core(1000, 2, rtcm_out=True) + _msgout({"NAV_SVIN": 1})
    core += [("CFG_MSGOUT_RTCM_3X_TYPE1005_USB", 1)]
    core += [(f"CFG_MSGOUT_RTCM_3X_TYPE{t}_USB", 1) for t in enabled]
    core += [(f"CFG_MSGOUT_RTCM_3X_TYPE{t}_USB", 0) for t in disabled]
    core += [
        ("CFG_MSGOUT_RTCM_3X_TYPE1230_USB", settings.rtcm_1230_rate),
        ("CFG_MSGOUT_RTCM_3X_TYPE4072_0_USB", 0),
        ("CFG_MSGOUT_RTCM_3X_TYPE4072_1_USB", 0),
        ("CFG_RTCM_DF003_OUT", settings.rtcm_station_id),
    ]
    return Profile("base", core, list(SIGNALS_L1_L2), _optional_features(), nav_hz=1)


def rover_profile(settings: Settings) -> Profile:
    meas_ms = round(1000 / settings.rover_nav_hz)
    core = _common_core(meas_ms, settings.dynmodel_code, rtcm_out=False)
    core += _msgout({"NAV_RELPOSNED": 1, "RXM_RTCM": 1, "TIM_TM2": 1, "NAV_VELNED": 1})
    core += [("CFG_NAVHPG_DGNSSMODE", 3), ("CFG_TMODE_MODE", 0)]
    return Profile(
        "rover",
        core,
        list(SIGNALS_L1_L2),
        _optional_features(),
        nav_hz=settings.rover_nav_hz,
    )


def tmode_off() -> CfgItems:
    return [("CFG_TMODE_MODE", 0)]


def tmode_survey_in(min_dur_s: int, acc_limit_m: float) -> CfgItems:
    return [
        ("CFG_TMODE_MODE", 1),
        ("CFG_TMODE_SVIN_MIN_DUR", int(min_dur_s)),
        ("CFG_TMODE_SVIN_ACC_LIMIT", int(round(acc_limit_m * 10_000))),  # 0.1 mm units
    ]


def tmode_fixed_ecef(x_m: float, y_m: float, z_m: float, acc_m: float) -> CfgItems:
    items: CfgItems = [("CFG_TMODE_MODE", 2), ("CFG_TMODE_POS_TYPE", 0)]
    for axis, value in (("X", x_m), ("Y", y_m), ("Z", z_m)):
        std_cm, hp_01mm = val2sphp(value, 0.01)
        items += [
            (f"CFG_TMODE_ECEF_{axis}", int(std_cm)),
            (f"CFG_TMODE_ECEF_{axis}_HP", int(hp_01mm)),
        ]
    items.append(("CFG_TMODE_FIXED_POS_ACC", int(round(acc_m * 10_000))))
    return items


def chunked(items: CfgItems, n: int = MAX_KEYS_PER_VALSET) -> list[CfgItems]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def all_keys(profile: Profile) -> list[str]:
    keys = [k for k, _ in profile.core] + [k for k, _ in profile.signals]
    for items in profile.optional.values():
        keys += [k for k, _ in items]
    return keys
