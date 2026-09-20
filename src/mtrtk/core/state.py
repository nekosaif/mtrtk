"""Typed live receiver state. The UI, sampler and alert rules consume these models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

GNSS_NAMES: dict[int, str] = {
    0: "GPS",
    1: "SBAS",
    2: "Galileo",
    3: "BeiDou",
    4: "IMES",
    5: "QZSS",
    6: "GLONASS",
    7: "NavIC",
}
# (gnssId, sigId) -> signal name, per u-blox ZED-F9P interface description
SIGNAL_NAMES: dict[tuple[int, int], str] = {
    (0, 0): "L1C/A",
    (0, 3): "L2CL",
    (0, 4): "L2CM",
    (0, 6): "L5I",
    (0, 7): "L5Q",
    (1, 0): "L1C/A",
    (2, 0): "E1C",
    (2, 1): "E1B",
    (2, 3): "E5aI",
    (2, 4): "E5aQ",
    (2, 5): "E5bI",
    (2, 6): "E5bQ",
    (3, 0): "B1I D1",
    (3, 1): "B1I D2",
    (3, 2): "B2I D1",
    (3, 3): "B2I D2",
    (3, 5): "B1C",
    (3, 7): "B2a",
    (5, 0): "L1C/A",
    (5, 1): "L1S",
    (5, 4): "L2CM",
    (5, 5): "L2CL",
    (5, 8): "L5I",
    (5, 9): "L5Q",
    (6, 0): "L1OF",
    (6, 2): "L2OF",
    (7, 0): "L5A",
}
FIX_TYPE_NAMES: dict[int, str] = {
    0: "No fix",
    1: "Dead reckoning",
    2: "2D",
    3: "3D",
    4: "GNSS+DR",
    5: "Time only",
}
CARR_SOLN_NAMES: dict[int, str] = {0: "None", 1: "RTK float", 2: "RTK fixed"}
ANT_STATUS_NAMES: dict[int, str] = {0: "Init", 1: "Unknown", 2: "OK", 3: "Short", 4: "Open"}
ANT_POWER_NAMES: dict[int, str] = {0: "Off", 1: "On", 2: "Unknown"}
JAMMING_STATE_NAMES: dict[int, str] = {0: "Unknown", 1: "OK", 2: "Warning", 3: "Critical"}


def signal_name(gnss_id: int, sig_id: int) -> str:
    return SIGNAL_NAMES.get((gnss_id, sig_id), f"sig{sig_id}")


class Position(BaseModel):
    lat: float | None = None
    lon: float | None = None
    height_m: float | None = None  # above ellipsoid
    hmsl_m: float | None = None  # above mean sea level (receiver geoid model)
    ecef_x_m: float | None = None
    ecef_y_m: float | None = None
    ecef_z_m: float | None = None
    invalid_llh: bool = False


class Accuracy(BaseModel):
    h_acc_m: float | None = None
    v_acc_m: float | None = None
    p_acc_m: float | None = None  # 3D position accuracy (NAV-HPPOSECEF)
    t_acc_ns: int | None = None
    s_acc_mps: float | None = None
    head_acc_deg: float | None = None


class Dops(BaseModel):
    g: float | None = None
    p: float | None = None
    t: float | None = None
    v: float | None = None
    h: float | None = None
    n: float | None = None
    e: float | None = None


class FixInfo(BaseModel):
    fix_type: int = 0
    fix_type_name: str = "No fix"
    gnss_fix_ok: bool = False
    diff_soln: bool = False
    carr_soln: int = 0
    carr_soln_name: str = "None"
    num_sv: int = 0
    last_correction_age: int = 0  # NAV-PVT flags3 code (0 = n/a)
    psm_state: int = 0
    spoof_det_state: int = 0
    ttff_ms: int | None = None
    uptime_ms: int | None = None


class Velocity(BaseModel):
    vel_n_mps: float | None = None
    vel_e_mps: float | None = None
    vel_d_mps: float | None = None
    ground_speed_mps: float | None = None
    heading_motion_deg: float | None = None


class TimeInfo(BaseModel):
    utc: datetime | None = None
    itow_ms: int | None = None
    gps_week: int | None = None
    gps_tow_s: float | None = None
    leap_s: int | None = None
    valid_date: bool = False
    valid_time: bool = False
    fully_resolved: bool = False
    valid_utc: bool = False
    utc_standard: int | None = None
    t_acc_ns: int | None = None
    clk_bias_ns: int | None = None
    clk_drift_nsps: int | None = None
    f_acc_psps: int | None = None
    leap_source: int | None = None
    time_to_leap_event_s: int | None = None
    leap_change: int | None = None


class Signal(BaseModel):
    sig_id: int
    name: str
    freq_id: int = 0
    cno: int = 0
    pr_res_m: float = 0.0
    quality_ind: int = 0
    corr_source: int = 0
    iono_model: int = 0
    health: int = 0
    pr_used: bool = False
    cr_used: bool = False
    do_used: bool = False


class Satellite(BaseModel):
    gnss_id: int
    gnss: str
    sv_id: int
    cno: int = 0
    elev: int | None = None
    azim: int | None = None
    pr_res_m: float = 0.0
    quality_ind: int = 0
    used: bool = False
    health: int = 0
    diff_corr: bool = False
    smoothed: bool = False
    orbit_source: int = 0
    eph_avail: bool = False
    alm_avail: bool = False
    signals: list[Signal] = Field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.gnss_id, self.sv_id)


class SatSummary(BaseModel):
    tracked: int = 0
    used: int = 0
    per_gnss: dict[str, dict[str, int]] = Field(
        default_factory=dict
    )  # {"GPS": {"tracked": 12, "used": 9}}


class Hardware(BaseModel):
    ant_status: int = 0
    ant_status_name: str = "Init"
    ant_power: int = 2
    ant_power_name: str = "Unknown"
    noise_per_ms: int = 0
    agc_cnt: int = 0
    jam_ind: int = 0
    jamming_state: int = 0
    jamming_state_name: str = "Unknown"
    rtc_calib: bool = False
    safe_boot: bool = False
    xtal_absent: bool = False


class RfBlock(BaseModel):
    block_id: int
    jamming_state: int = 0
    jamming_state_name: str = "Unknown"
    ant_status: int = 0
    ant_status_name: str = "Init"
    ant_power: int = 2
    ant_power_name: str = "Unknown"
    post_status: int = 0
    noise_per_ms: int = 0
    agc_cnt: int = 0
    jam_ind: int = 0
    ofs_i: int = 0
    mag_i: int = 0
    ofs_q: int = 0
    mag_q: int = 0


class Spectrum(BaseModel):
    block_id: int
    span_hz: int
    res_hz: int
    center_hz: int
    pga_db: int
    bins: list[int]


class PortStats(BaseModel):
    port_id: int
    tx_pending: int = 0
    tx_bytes: int = 0
    tx_usage: int = 0
    tx_peak_usage: int = 0
    rx_pending: int = 0
    rx_bytes: int = 0
    rx_usage: int = 0
    rx_peak_usage: int = 0
    overrun_errs: int = 0
    skipped: int = 0


class SurveyIn(BaseModel):
    active: bool = False
    valid: bool = False
    dur_s: int = 0
    obs: int = 0
    mean_x_m: float | None = None
    mean_y_m: float | None = None
    mean_z_m: float | None = None
    mean_acc_m: float | None = None


class RtcmMsgStats(BaseModel):
    count: int = 0
    bytes: int = 0
    last_seen_mono: float | None = None


class RtcmStats(BaseModel):
    messages: dict[int, RtcmMsgStats] = Field(default_factory=dict)
    total_count: int = 0
    total_bytes: int = 0
    bytes_per_s: float = 0.0


class Firmware(BaseModel):
    sw_version: str = ""
    hw_version: str = ""
    fw_version: str = ""  # e.g. "HPG 1.13"
    protver: str = ""  # e.g. "27.12"
    module: str = ""  # e.g. "ZED-F9P"
    extensions: list[str] = Field(default_factory=list)


class ReceiverState(BaseModel):
    connected: bool = False
    source: str = ""
    position: Position = Field(default_factory=Position)
    accuracy: Accuracy = Field(default_factory=Accuracy)
    dops: Dops = Field(default_factory=Dops)
    fix: FixInfo = Field(default_factory=FixInfo)
    velocity: Velocity = Field(default_factory=Velocity)
    time: TimeInfo = Field(default_factory=TimeInfo)
    sats: list[Satellite] = Field(default_factory=list)
    sat_summary: SatSummary = Field(default_factory=SatSummary)
    hardware: Hardware | None = None
    rf: list[RfBlock] = Field(default_factory=list)
    spectrum: list[Spectrum] = Field(default_factory=list)
    ports: list[PortStats] = Field(default_factory=list)
    survey_in: SurveyIn = Field(default_factory=SurveyIn)
    rtcm_out: RtcmStats = Field(default_factory=RtcmStats)
    firmware: Firmware = Field(default_factory=Firmware)
    epoch_count: int = 0
    raw_epochs: int = 0  # RXM-RAWX frames seen (never parsed)
    last_epoch_mono: float | None = None
