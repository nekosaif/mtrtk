"""Runtime settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Role(StrEnum):
    BASE = "base"
    ROVER = "rover"


class BaseMode(StrEnum):
    SURVEY_IN = "survey-in"
    FIXED = "fixed"
    OFF = "off"


class DynModel(StrEnum):
    PORTABLE = "portable"
    STATIONARY = "stationary"
    PEDESTRIAN = "pedestrian"
    AUTOMOTIVE = "automotive"
    AIRBORNE1G = "airborne1g"
    AIRBORNE2G = "airborne2g"
    AIRBORNE4G = "airborne4g"


# u-blox CFG-NAVSPG-DYNMODEL enumeration values
DYNMODEL_CODES: dict[DynModel, int] = {
    DynModel.PORTABLE: 0,
    DynModel.STATIONARY: 2,
    DynModel.PEDESTRIAN: 3,
    DynModel.AUTOMOTIVE: 4,
    DynModel.AIRBORNE1G: 6,
    DynModel.AIRBORNE2G: 7,
    DynModel.AIRBORNE4G: 8,
}

BIND_MODES = ("tailscale", "lan", "all")

DEFAULT_LOG_MESSAGES = [
    "RXM-RAWX",
    "RXM-SFRBX",
    "NAV-PVT",
    "NAV-HPPOSLLH",
    "NAV-SVIN",
    "TIM-TM2",
    "MON-VER",
]


# Optional fields whose empty environment value means "unset" rather than "empty string".
# `ntrip_password` is *not* in this list: there `""` means anonymous access, distinct from unset.
OPTIONAL_FIELDS = (
    "active_site",
    "web_password",
    "ntrip_url",
    "nmea_serial",
    "json_udp_port",
    "alert_webhook_url",
    "public_domain",
)


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _validate_bind(name: str, value: str) -> str:
    if value in BIND_MODES:
        return value
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be one of {BIND_MODES} or an IP address, got {value!r}"
        ) from exc
    return value


class Settings(BaseSettings):
    """All mtrtk configuration. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- identity / receiver -------------------------------------------------
    role: Role = Role.BASE
    mtrtk_source: str = "auto"  # "auto" | serial device path | "file:<path>"
    baud: int = 115200
    data_dir: Path = Path("/data")
    station_id: str = Field("MTRK", pattern=r"^[A-Z0-9]{4}$")
    country: str = "BGD"
    marker_name: str = "MTRK"
    antenna_type: str = "NONE"
    antenna_height_m: float = 0.0
    observer: str = "mtrtk"
    agency: str = "mtrtk"
    receiver_strict: bool = True  # fail startup if a core CFG key is rejected
    replay_speed: float = 1.0  # file source pacing multiplier; 0 = as fast as possible
    replay_loop: bool = False

    # --- base ----------------------------------------------------------------
    base_mode: BaseMode = BaseMode.SURVEY_IN
    svin_min_duration_s: int = 300
    svin_acc_limit_m: float = 2.0
    active_site: str | None = None
    rtcm_msm: Literal[4, 7] = 7
    rtcm_1230_rate: int = 5
    rtcm_station_id: int = Field(0, ge=0, le=4095)

    # --- NTRIP caster --------------------------------------------------------
    ntrip_bind: str = "tailscale"
    ntrip_port: int = 2101
    mountpoint: str = "MTRK"
    ntrip_user: str = "rover"
    ntrip_password: str | None = None  # None = not decided (error for base); "" = anonymous

    # --- web -----------------------------------------------------------------
    web_bind: str = "tailscale"
    web_port: int = 8080
    web_password: str | None = None
    web_allow_insecure: bool = False

    # --- logging / retention -------------------------------------------------
    log_messages: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_LOG_MESSAGES)
    )
    min_free_gb: float = 5.0
    fsync_interval_s: int = 10

    # --- rover ---------------------------------------------------------------
    rover_driver: Literal["ublox", "sbg_ellipse", "vectornav"] = "ublox"
    rover_nav_hz: int = Field(5, ge=1, le=8)
    rover_dynmodel: DynModel = DynModel.PORTABLE
    ntrip_url: str | None = None
    ntrip_gga_interval_s: int = 10
    nmea_tcp_port: int = 10110
    nmea_udp_targets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    nmea_serial: str | None = None
    json_udp_port: int | None = None

    # --- alerts / exposure ---------------------------------------------------
    alert_webhook_url: str | None = None
    public_domain: str | None = None

    # --- validators ----------------------------------------------------------
    @field_validator(*OPTIONAL_FIELDS, mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """Treat a blank environment value as "not set".

        `KEY=` in a .env file (or an empty value from Compose) arrives as `""`, which is neither
        `None` nor a parseable int: `JSON_UDP_PORT=` used to abort startup, and the `str | None`
        fields silently became `""` instead of their `None` default. `ntrip_password` is
        deliberately excluded - there an empty value means "anonymous access", not "unset".
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_messages", "nmea_udp_targets", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("rtcm_msm", mode="before")
    @classmethod
    def _rtcm_msm(cls, value: object) -> object:
        """Environment values arrive as strings; `Literal[4, 7]` only accepts ints."""
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return int(value)
        return value

    @field_validator("ntrip_bind")
    @classmethod
    def _ntrip_bind(cls, value: str) -> str:
        return _validate_bind("NTRIP_BIND", value)

    @field_validator("web_bind")
    @classmethod
    def _web_bind(cls, value: str) -> str:
        return _validate_bind("WEB_BIND", value)

    @model_validator(mode="after")
    def _cross_checks(self) -> Settings:
        if (
            self.web_bind not in ("tailscale",)
            and not self.web_password
            and not self.web_allow_insecure
        ):
            raise ValueError(
                "WEB_PASSWORD must be set when WEB_BIND is not 'tailscale' "
                "(or set WEB_ALLOW_INSECURE=1 to accept an unauthenticated UI)"
            )
        if self.role is Role.BASE and self.ntrip_password is None:
            raise ValueError(
                "NTRIP_PASSWORD must be set for the base role "
                "(use NTRIP_PASSWORD= with an empty value to allow anonymous rovers)"
            )
        return self

    # --- derived -------------------------------------------------------------
    @property
    def source_is_file(self) -> bool:
        return self.mtrtk_source.startswith("file:")

    @property
    def source_path(self) -> Path:
        if not self.source_is_file:
            raise ValueError("source is not a file")
        return Path(self.mtrtk_source[len("file:") :])

    @property
    def dynmodel_code(self) -> int:
        return DYNMODEL_CODES[self.rover_dynmodel]

    @property
    def ntrip_anonymous(self) -> bool:
        return self.ntrip_password == ""
