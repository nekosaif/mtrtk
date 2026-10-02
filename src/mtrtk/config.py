"""Runtime settings, loaded from environment variables and an optional .env file."""

from __future__ import annotations

import ipaddress
import math
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator
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

# Free-text settings all end up in `.env`, which is re-read on every `GET /api/config` and on
# every restart, so each one is bounded. Names are generous next to what they describe (a RINEX
# marker name is 60 columns, an antenna type 20); a URL gets the 512 that fits any sane one, and
# `public_domain` the 253 bytes a DNS name can actually be.
NAME_MAX = 64
URL_MAX = 512
DOMAIN_MAX = 253

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
    "ins_port",
    "ins_rtcm_port",
    "ins_rtcm_baud",
    "ins_lever_arm_gnss1",
    "ins_lever_arm_gnss2",
    "ins_imu_lever_arm",
    "ins_init_position",
    "ins_vn_scenario",
    "ins_vn_ahrs_aiding",
    "ins_vn_ref_rotation",
    "ins_vn_vpe",
)

# INS vectors arrive as "x,y,z" (or "lat,lon,alt") strings from the environment.
INS_VECTOR_FIELDS = (
    "ins_lever_arm_gnss1",
    "ins_lever_arm_gnss2",
    "ins_imu_lever_arm",
    "ins_init_position",
)
Vector3 = Annotated[tuple[float, float, float] | None, NoDecode]


# Sentences mtrtk can synthesize (rover/nmea_out.py builds each; a test keeps the two in step).
NMEA_SENTENCE_NAMES = ("GGA", "RMC", "GST", "GSA", "GSV", "VTG", "ZDA", "HDT", "PASHR")


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _parse_vector3(name: str, value: object) -> object:
    """`"x,y,z"` -> a 3-tuple of finite floats; tuples and lists (a `model_dump()`) pass."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None  # this runs before `_empty_is_unset`, so a blank value is handled here too
    parts: list[object]
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, list | tuple):
        parts = list(value)
    else:
        return value
    if len(parts) != 3:
        raise ValueError(f"{name.upper()} must be three comma-separated numbers, got {value!r}")
    try:
        floats = tuple(float(p) for p in parts)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name.upper()} must be three numbers, got {value!r}") from exc
    if not all(math.isfinite(f) for f in floats):
        raise ValueError(f"{name.upper()} must be finite numbers, got {value!r}")
    return floats


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
    # Where `PUT /api/config` persists changes. Relative to the process's working directory,
    # which is also how `model_config`'s own `env_file=".env"` resolves it on the way in.
    mtrtk_env_file: Path = Path(".env")
    station_id: str = Field("MTRK", pattern=r"^[A-Z0-9]{4}$", max_length=4)
    country: str = Field("BGD", max_length=NAME_MAX)
    marker_name: str = Field("MTRK", max_length=NAME_MAX)
    antenna_type: str = Field("NONE", max_length=NAME_MAX)
    antenna_height_m: float = 0.0
    observer: str = Field("mtrtk", max_length=NAME_MAX)
    agency: str = Field("mtrtk", max_length=NAME_MAX)
    # 1: a core CFG key the receiver rejects (or a profile that fails verification) aborts
    # startup - the daemon exits 1 instead of reconnecting. 0: log a warning and keep running
    # with whatever the receiver did accept.
    receiver_strict: bool = True
    # How long each poll / CFG-VALSET / CFG-VALGET waits for the receiver's answer. 2 s is
    # ample on USB; a receiver reached over a slow tunnel (socat over a Tailscale relay) wants
    # more. Under 0.5 s a healthy receiver times out; past 30 s a dead one stalls every start.
    receiver_ack_timeout_s: float = Field(2.0, ge=0.5, le=30)
    replay_speed: float = 1.0  # file source pacing multiplier; 0 = as fast as possible
    replay_loop: bool = False
    replay_log: bool = False  # write raw logs even when replaying a file (tests, demos)

    # --- base ----------------------------------------------------------------
    base_mode: BaseMode = BaseMode.SURVEY_IN
    # A day is longer than any survey-in worth waiting for, and 100 m is well past the point
    # where a "fixed" base is fiction. Both go to `.env`, so a typo is carried into every restart
    # - and both reach the receiver, where a negative duration is not a value CFG-TMODE can hold.
    svin_min_duration_s: int = Field(300, ge=1, le=86400)
    svin_acc_limit_m: float = Field(2.0, gt=0, le=100)
    active_site: str | None = Field(None, max_length=NAME_MAX)
    rtcm_msm: Literal[4, 7] = 7
    rtcm_1230_rate: int = 5
    rtcm_station_id: int = Field(0, ge=0, le=4095)

    # --- NTRIP caster --------------------------------------------------------
    ntrip_bind: str = "tailscale"
    ntrip_port: int = 2101
    mountpoint: str = Field("MTRK", max_length=NAME_MAX)
    ntrip_user: str = Field("rover", max_length=NAME_MAX)
    ntrip_password: str | None = None  # None = not decided (error for base); "" = anonymous
    ntrip_max_clients: int = 32

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
    ntrip_url: str | None = Field(None, max_length=URL_MAX)
    ntrip_gga_interval_s: int = Field(10, ge=0, le=3600)  # 0 = do not send GGA
    nmea_tcp_port: int = Field(10110, ge=-1, le=65535)  # -1 = off, 0 = any free port
    # NMEA consumers (a tablet, an autopilot, gpsd) sit on the LAN and cannot authenticate, so
    # the default listens on every interface; `tailscale` or an IP narrows it.
    nmea_tcp_bind: str = "lan"
    nmea_tcp_max_clients: int = Field(16, ge=1, le=1024)
    nmea_sentences: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["GGA", "RMC", "GST", "GSA", "GSV", "VTG", "ZDA"]
    )
    nmea_slow_interval_s: float = Field(1.0, gt=0, le=60)  # GSA/GSV/ZDA period
    nmea_udp_targets: Annotated[list[str], NoDecode] = Field(default_factory=list)
    nmea_serial: str | None = None
    json_udp_port: int | None = Field(None, ge=1, le=65535)
    # NMEA_SERIAL's own line speed: a consumer's baud, not the receiver link's BAUD.
    nmea_serial_baud: int = Field(115200, ge=1200, le=4_000_000)

    # --- alerts / exposure ---------------------------------------------------
    alert_webhook_url: str | None = Field(None, max_length=URL_MAX)
    public_domain: str | None = Field(None, max_length=DOMAIN_MAX)

    # --- survey points (rover) -----------------------------------------------
    point_epochs: int = Field(30, ge=1, le=3600)  # epochs averaged per point
    point_fixed_only: bool = True  # count only RTK-fixed epochs

    # --- INS drivers (ROVER_DRIVER=sbg_ellipse|vectornav) ---------------------
    # No auto-detect: INS units sit behind generic FTDI/CP210x bridges, so the port is explicit.
    ins_port: str | None = None
    ins_baud: int = Field(115200, ge=1200, le=4_000_000)  # the bench Ellipse-D runs at 921600
    # SBG: a separate serial device carrying RTCM to the unit's Port B when the main port
    # cannot take it. None = inject on the main port.
    ins_rtcm_port: str | None = None
    # Port B's own line rate (set in sbgCenter, often 115200 for an RTCM input); None = INS_BAUD.
    ins_rtcm_baud: int | None = Field(None, ge=1200, le=4_000_000)
    ins_output_hz: int = Field(10, ge=1, le=200)
    ins_apply_config: bool = False  # write the vendor config subset on connect
    ins_raw_gnss: bool = True  # capture the unit's raw GNSS stream for PPK
    # Lever arms in metres, body frame: IMU -> antenna (GNSS1/2), and the SBG IMU lever arm.
    ins_lever_arm_gnss1: Vector3 = None
    ins_lever_arm_gnss2: Vector3 = None
    ins_imu_lever_arm: Vector3 = None
    ins_imu_axis: str = Field("xyz", min_length=1, max_length=NAME_MAX)  # SBG axis mapping
    # Validated against what each vendor supports by its driver.
    ins_motion_profile: Literal[
        "general", "automotive", "marine", "airplane", "helicopter", "uav", "pedestrian"
    ] = "general"
    ins_init_position: Vector3 = None  # lat, lon (deg), alt (m) for the unit's initial fix

    # --- VectorNav (ROVER_DRIVER=vectornav) -----------------------------------
    # Forward RTCM to the VN-200: undocumented for that unit (VERIFY(vn-rtcm)), so opt-in.
    ins_vn_rtcm: bool = False
    # Register 67 INS basic configuration: scenario (VERIFY(vn-reg67-scenario)) and AHRS aiding.
    ins_vn_scenario: int | None = Field(None, ge=0, le=255)
    ins_vn_ahrs_aiding: bool | None = None
    # Register 26 reference frame rotation: 9 comma-separated floats, row-major.
    ins_vn_ref_rotation: str | None = None
    # Register 35 VPE basic control: "enable,headingMode,filteringMode,tuningMode".
    ins_vn_vpe: str | None = None

    @field_validator("ins_vn_ref_rotation")
    @classmethod
    def _vn_ref_rotation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            floats = [float(p) for p in value.split(",")]
        except ValueError:
            floats = []
        if len(floats) != 9 or not all(math.isfinite(f) for f in floats):
            raise ValueError(f"INS_VN_REF_ROTATION must be nine comma-separated numbers: {value!r}")
        return value

    @field_validator("ins_vn_vpe")
    @classmethod
    def _vn_vpe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 4 or not all(p.isdigit() and int(p) <= 255 for p in parts):
            raise ValueError(
                "INS_VN_VPE must be four integers "
                f"'enable,headingMode,filteringMode,tuningMode': {value!r}"
            )
        return value

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

    @field_validator("nmea_sentences", mode="before")
    @classmethod
    def _nmea_sentences(cls, value: object) -> object:
        """CSV, any case; an unknown name is a startup error rather than a silent no-op."""
        value = _split_csv(value)
        if isinstance(value, list):
            names = [str(v).strip().upper() for v in value]
            unknown = [n for n in names if n not in NMEA_SENTENCE_NAMES]
            if unknown:
                raise ValueError(
                    f"unknown NMEA sentence(s) {unknown}; choose from {list(NMEA_SENTENCE_NAMES)}"
                )
            return names
        return value

    @field_validator("rtcm_msm", mode="before")
    @classmethod
    def _rtcm_msm(cls, value: object) -> object:
        """Environment values arrive as strings; `Literal[4, 7]` only accepts ints."""
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return int(value)
        return value

    @field_validator(*INS_VECTOR_FIELDS, mode="before")
    @classmethod
    def _ins_vector(cls, value: object, info: ValidationInfo) -> object:
        return _parse_vector3(info.field_name or "value", value)

    @field_validator("ins_init_position")
    @classmethod
    def _ins_init_position(
        cls, value: tuple[float, float, float] | None
    ) -> tuple[float, float, float] | None:
        if value is not None and not (-90 <= value[0] <= 90 and -180 <= value[1] <= 180):
            raise ValueError(f"INS_INIT_POSITION must be lat,lon,alt in degrees, got {value}")
        return value

    @field_validator("ntrip_bind")
    @classmethod
    def _ntrip_bind(cls, value: str) -> str:
        return _validate_bind("NTRIP_BIND", value)

    @field_validator("nmea_tcp_bind")
    @classmethod
    def _nmea_tcp_bind(cls, value: str) -> str:
        return _validate_bind("NMEA_TCP_BIND", value)

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
        # A file source (MTRTK_SOURCE=file:...) replays a capture through the INS stack instead.
        # Only a rover builds that stack: a base with a leftover ROVER_DRIVER still starts.
        if (
            self.role is Role.ROVER
            and self.rover_driver != "ublox"
            and self.ins_port is None
            and not self.source_is_file
        ):
            raise ValueError(f"INS_PORT is required for ROVER_DRIVER={self.rover_driver}")
        return self

    # --- derived -------------------------------------------------------------
    @property
    def ins_rtcm_baud_or_main(self) -> int:
        """The rate INS_RTCM_PORT is opened at: INS_RTCM_BAUD, else INS_BAUD."""
        return self.ins_rtcm_baud or self.ins_baud

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

    def udp_targets(self) -> list[tuple[str, int]]:
        """`NMEA_UDP_TARGETS` as `(host, port)` pairs; an entry that is not `host:port` (port
        1-65535) is skipped rather than failing the whole rover."""
        out: list[tuple[str, int]] = []
        for item in self.nmea_udp_targets:
            host, _, port = item.rpartition(":")
            # isascii: str.isdigit() also accepts digits like '²' that int() refuses.
            if host and port.isascii() and port.isdigit() and 0 < int(port) < 65536:
                out.append((host, int(port)))
        return out
