"""Export presets for the PPP services we target."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    service_url: str
    description: str
    version: str
    interval_s: float | None
    exclude_systems: tuple[str, ...]
    hatanaka: bool
    gzip: bool
    constraints: tuple[str, ...]
    adjustable: bool = False


PRESETS: dict[str, Preset] = {
    "csrs-ppp": Preset(
        id="csrs-ppp",
        name="CSRS-PPP (NRCan)",
        service_url="https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php",
        description=(
            "Free global PPP. Static mode, ITRF2020 result at the observation epoch. "
            "Upload the .crx.gz file."
        ),
        version="3.04",
        interval_s=30,
        exclude_systems=(),
        hatanaka=True,
        gzip=True,
        constraints=(
            "24 h of data recommended (a few hours minimum)",
            "RINEX 2.11 or 3.x, Hatanaka + gzip accepted",
            "Choose 'Static' and ITRF outside Canada",
            "Result: .sum + .pos in a zip by e-mail",
        ),
    ),
    "auspos": Preset(
        id="auspos",
        name="AUSPOS (Geoscience Australia)",
        service_url="https://gnss.ga.gov.au/auspos",
        description=(
            "Free global GPS PPP/relative processing. 1 h minimum, 2 h+ recommended, up to 7 days."
        ),
        version="3.04",
        interval_s=30,
        exclude_systems=(),
        hatanaka=False,
        gzip=True,
        constraints=(
            "1 h minimum, 7 days maximum",
            "GPS observations are used",
            "Result: PDF report + SINEX (.SNX) by e-mail",
        ),
    ),
    "opus": Preset(
        id="opus",
        name="OPUS (NGS, USA)",
        service_url="https://geodesy.noaa.gov/OPUS/",
        description=(
            "US National Geodetic Survey. GPS L1/L2 only, RINEX 2.11, 15 min to 48 h. "
            "Works inside the continental US only."
        ),
        version="2.11",
        interval_s=30,
        exclude_systems=("R", "E", "J", "C", "S", "I"),
        hatanaka=False,
        gzip=False,
        constraints=(
            "Only for sites in the USA",
            "GPS only, dual-frequency; F9P L2C acceptance must be checked",
            "Antenna type NONE unless NGS-calibrated",
            "Result: text e-mail",
        ),
    ),
    "generic": Preset(
        id="generic",
        name="Generic RINEX 3.04",
        service_url="",
        description=(
            "Full-rate mixed RINEX for RTKLIB PPK, Trimble RTX post-processing or any other "
            "tool. Interval and compression are adjustable."
        ),
        version="3.04",
        interval_s=None,
        exclude_systems=(),
        hatanaka=False,
        gzip=False,
        constraints=("All constellations, native interval", "Hatanaka and gzip optional"),
        adjustable=True,
    ),
}


@dataclass(frozen=True)
class ResolvedOptions:
    preset: Preset
    version: str
    interval_s: float | None
    exclude_systems: tuple[str, ...]
    hatanaka: bool
    gzip: bool


def resolve_options(
    preset_id: str,
    interval_s: float | None = None,
    hatanaka: bool | None = None,
    gzip: bool | None = None,
) -> ResolvedOptions:
    preset = PRESETS[preset_id]
    requested = {"interval_s": interval_s, "hatanaka": hatanaka, "gzip": gzip}
    overrides = sorted(k for k, v in requested.items() if v is not None)
    if overrides and not preset.adjustable:
        raise ValueError(
            f"preset {preset_id!r} has fixed options; use 'generic' to adjust {overrides}"
        )
    return ResolvedOptions(
        preset,
        preset.version,
        preset.interval_s if interval_s is None else interval_s,
        preset.exclude_systems,
        preset.hatanaka if hatanaka is None else hatanaka,
        preset.gzip if gzip is None else gzip,
    )
