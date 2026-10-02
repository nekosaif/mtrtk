/**
 * TypeScript mirror of the daemon's JSON. Field names are identical to the wire.
 *
 * Sources of truth, in order: `docs/api.md`, `src/mtrtk/core/state.py` (ReceiverState and its
 * sections), `src/mtrtk/store/models.py` (Site, Event, SystemStats, NtripClientRecord),
 * `src/mtrtk/jobs.py` (Job), `src/mtrtk/base/ntrip_caster.py` (`ClientInfo.public()`),
 * `src/mtrtk/config.py` (Settings → ConfigValues) and `src/mtrtk/web/ws.py` (the WebSocket
 * protocol). `contract.test.ts` checks the request bodies here against `openapi.snapshot.json`.
 *
 * Numbers that Python types as `int` are `number` here; datetimes are ISO-8601 UTC strings;
 * `dict[int, …]` keys arrive as strings.
 */

// ------------------------------------------------------------ receiver state

export interface Position {
  lat: number | null;
  lon: number | null;
  /** Height above the ellipsoid. */
  height_m: number | null;
  /** Height above mean sea level (receiver geoid model). */
  hmsl_m: number | null;
  ecef_x_m: number | null;
  ecef_y_m: number | null;
  ecef_z_m: number | null;
  invalid_llh: boolean;
}

export interface Accuracy {
  h_acc_m: number | null;
  v_acc_m: number | null;
  /** 3D position accuracy (NAV-HPPOSECEF). */
  p_acc_m: number | null;
  t_acc_ns: number | null;
  s_acc_mps: number | null;
  head_acc_deg: number | null;
}

export interface Dops {
  g: number | null;
  p: number | null;
  t: number | null;
  v: number | null;
  h: number | null;
  n: number | null;
  e: number | null;
}

/** UBX fixType: 0 no fix, 1 dead reckoning, 2 2D, 3 3D, 4 GNSS+DR, 5 time only (a fixed base). */
export type FixType = 0 | 1 | 2 | 3 | 4 | 5;
/** UBX carrSoln: 0 none, 1 RTK float, 2 RTK fixed. */
export type CarrSoln = 0 | 1 | 2;

export interface FixInfo {
  fix_type: number;
  fix_type_name: string;
  gnss_fix_ok: boolean;
  diff_soln: boolean;
  carr_soln: number;
  carr_soln_name: string;
  num_sv: number;
  /** NAV-PVT flags3 code (0 = n/a). */
  last_correction_age: number;
  psm_state: number;
  spoof_det_state: number;
  ttff_ms: number | null;
  uptime_ms: number | null;
}

export interface Velocity {
  vel_n_mps: number | null;
  vel_e_mps: number | null;
  vel_d_mps: number | null;
  ground_speed_mps: number | null;
  heading_motion_deg: number | null;
}

export interface TimeInfo {
  /** ISO-8601 UTC, or null before the first time fix. */
  utc: string | null;
  itow_ms: number | null;
  gps_week: number | null;
  gps_tow_s: number | null;
  leap_s: number | null;
  valid_date: boolean;
  valid_time: boolean;
  fully_resolved: boolean;
  valid_utc: boolean;
  utc_standard: number | null;
  t_acc_ns: number | null;
  clk_bias_ns: number | null;
  clk_drift_nsps: number | null;
  f_acc_psps: number | null;
  leap_source: number | null;
  time_to_leap_event_s: number | null;
  leap_change: number | null;
}

export interface Signal {
  sig_id: number;
  name: string;
  freq_id: number;
  cno: number;
  pr_res_m: number;
  quality_ind: number;
  corr_source: number;
  iono_model: number;
  health: number;
  pr_used: boolean;
  cr_used: boolean;
  do_used: boolean;
}

export interface Satellite {
  gnss_id: number;
  /** System name: GPS, SBAS, Galileo, BeiDou, IMES, QZSS, GLONASS, NavIC. */
  gnss: string;
  sv_id: number;
  cno: number;
  elev: number | null;
  azim: number | null;
  pr_res_m: number;
  quality_ind: number;
  used: boolean;
  health: number;
  diff_corr: boolean;
  smoothed: boolean;
  orbit_source: number;
  eph_avail: boolean;
  alm_avail: boolean;
  signals: Signal[];
}

export interface SatSummary {
  tracked: number;
  used: number;
  /** `{"GPS": {"tracked": 12, "used": 9}}` */
  per_gnss: Record<string, { tracked: number; used: number }>;
}

export interface Hardware {
  ant_status: number;
  ant_status_name: string;
  ant_power: number;
  ant_power_name: string;
  noise_per_ms: number;
  agc_cnt: number;
  jam_ind: number;
  jamming_state: number;
  jamming_state_name: string;
  rtc_calib: boolean;
  safe_boot: boolean;
  xtal_absent: boolean;
}

export interface RfBlock {
  block_id: number;
  jamming_state: number;
  jamming_state_name: string;
  ant_status: number;
  ant_status_name: string;
  ant_power: number;
  ant_power_name: string;
  post_status: number;
  noise_per_ms: number;
  agc_cnt: number;
  jam_ind: number;
  ofs_i: number;
  mag_i: number;
  ofs_q: number;
  mag_q: number;
}

export interface Spectrum {
  block_id: number;
  span_hz: number;
  res_hz: number;
  center_hz: number;
  pga_db: number;
  bins: number[];
}

export interface PortStats {
  port_id: number;
  tx_pending: number;
  tx_bytes: number;
  tx_usage: number;
  tx_peak_usage: number;
  rx_pending: number;
  rx_bytes: number;
  rx_usage: number;
  rx_peak_usage: number;
  overrun_errs: number;
  skipped: number;
}

export interface SurveyIn {
  active: boolean;
  valid: boolean;
  dur_s: number;
  obs: number;
  mean_x_m: number | null;
  mean_y_m: number | null;
  mean_z_m: number | null;
  mean_acc_m: number | null;
}

export interface RtcmMsgStats {
  count: number;
  bytes: number;
  last_seen_mono: number | null;
}

export interface RtcmStats {
  /** Keyed by RTCM message number as a string ("1005", "1077", …). */
  messages: Record<string, RtcmMsgStats>;
  total_count: number;
  total_bytes: number;
  bytes_per_s: number;
}

export interface Firmware {
  sw_version: string;
  hw_version: string;
  /** e.g. "HPG 1.13" */
  fw_version: string;
  /** e.g. "27.12" */
  protver: string;
  /** e.g. "ZED-F9P" */
  module: string;
  extensions: string[];
}

/** `GET /api/state`, and the WebSocket snapshot's `state`. */
export interface ReceiverState {
  connected: boolean;
  source: string;
  position: Position;
  accuracy: Accuracy;
  dops: Dops;
  fix: FixInfo;
  velocity: Velocity;
  time: TimeInfo;
  sats: Satellite[];
  sat_summary: SatSummary;
  hardware: Hardware | null;
  rf: RfBlock[];
  spectrum: Spectrum[];
  ports: PortStats[];
  survey_in: SurveyIn;
  rtcm_out: RtcmStats;
  firmware: Firmware;
  /** Rover RTK status; all defaults on a base. */
  rtk: RtkStatus;
  /** Newest last, at most the daemon's `MAX_TIME_MARKS`. */
  time_marks: TimeMark[];
  attitude: Attitude | null;
  /** INS rovers (SBG / VectorNav): filter state and health; null or absent on u-blox. */
  ins?: InsStatus | null;
  /** INS rovers: the latest IMU sample. */
  imu?: ImuSample | null;
  epoch_count: number;
  /** RXM-RAWX frames seen (never parsed). */
  raw_epochs: number;
  last_epoch_mono: number | null;
}

// ------------------------------------------------------------- store models

export interface SystemStats {
  cpu_pct: number;
  mem_pct: number;
  disk_free_gb: number;
  disk_used_pct: number;
  uptime_s: number;
  temp_c: number | null;
  load1: number | null;
  ts_utc: string;
}

export type Level = "info" | "warning" | "error";

export interface EventItem {
  /**
   * `store.models.Event.id` is `int | None`: the row id is assigned on insert, and the copy
   * published on `events.new` carries whatever `lastrowid` gave. A live entry without one can
   * be shown but not acknowledged.
   */
  id: number | null;
  ts_utc: string;
  level: Level;
  kind: string;
  message: string;
  meta: Record<string, unknown>;
  acked: boolean;
}

export interface Site {
  id: number | null;
  name: string;
  x: number;
  y: number;
  z: number;
  lat: number | null;
  lon: number | null;
  height_m: number | null;
  sigma_x: number | null;
  sigma_y: number | null;
  sigma_z: number | null;
  frame: string;
  epoch: string | null;
  /** survey-in | csrs-ppp | auspos | opus | manual */
  source: string;
  notes: string | null;
  created_utc: string | null;
  active: boolean;
}

/** `GET /api/ntrip/history` rows (`NtripClientRecord`). */
export interface NtripHistoryRecord {
  id: number;
  ip: string | null;
  mountpoint: string | null;
  user_agent: string | null;
  username: string | null;
  connected_utc: string;
  disconnected_utc: string | null;
  bytes_sent: number;
  last_lat: number | null;
  last_lon: number | null;
  reason: string | null;
}

/** `GET /api/ntrip/clients` items and the `ntrip.clients` WebSocket payload (`ClientInfo.public()`). */
export interface NtripClient {
  id: number;
  ip: string;
  port: number;
  mountpoint: string;
  user_agent: string;
  username: string | null;
  version: number;
  connected_utc: string;
  bytes_sent: number;
  dropped_frames: number;
  last_gga_lat: number | null;
  last_gga_lon: number | null;
  last_gga_utc: string | null;
}

export type JobStatus = "queued" | "running" | "done" | "failed";
/** The job kinds a panel can be narrowed to: RINEX exports and PPK runs. */
export type JobKind = "export" | "ppk";

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  created_utc: string;
  updated_utc: string | null;
  /** 0…1 */
  progress: number;
  message: string | null;
  params: Record<string, unknown>;
  result: Record<string, unknown> | null;
  /** `"TypeError: …"` for a job that raised; `"interrupted by restart"`, `"shutdown"`, `"cancelled"`. */
  error: string | null;
}

export interface JobFile {
  name: string;
  bytes: number;
}

// -------------------------------------------------------------- API answers

export interface HealthResponse {
  status: "ok";
  role: Role;
  connected: boolean;
  /** True on a replay source, which otherwise answers every route a live base does. */
  passive: boolean;
}

export interface LoginResponse {
  /** Empty when no password is configured. */
  token: string;
}

export interface OkResponse {
  ok: boolean;
}

export interface Capabilities {
  protver: string;
  fw_version: string;
  module: string;
  supported: string[];
  unsupported: string[];
}

/** `GET /api/status` — one screen. */
export interface StatusSummary {
  role: Role;
  version: string;
  uptime_s: number;
  connected: boolean;
  source: string;
  firmware: Pick<Firmware, "fw_version" | "protver" | "module">;
  fix: Pick<FixInfo, "fix_type_name" | "carr_soln_name" | "num_sv">;
  position: Pick<Position, "lat" | "lon" | "height_m">;
  accuracy: Pick<Accuracy, "h_acc_m" | "v_acc_m">;
  survey_in: Pick<SurveyIn, "active" | "valid" | "dur_s" | "mean_acc_m">;
  ntrip_clients: number;
  /** Callers turned away at `max_clients`; invisible in the live count. */
  ntrip_rejected: number;
  rtcm_bytes_per_s: number;
  epoch_count: number;
  capabilities: Pick<Capabilities, "supported" | "unsupported"> | null;
  /** The receiver driver: `ublox`, `sbg_ellipse` or `vectornav`. */
  driver?: DriverSummary;
}

/** `GET /api/system` */
export interface SystemInfo {
  hostname: string;
  tailscale_ip: string | null;
  data_dir: string;
  /** null until the first sample */
  stats: SystemStats | null;
  versions: Record<string, string>;
}

/** `GET /api/receiver` — always 200, even with nothing connected. */
export interface ReceiverInfo {
  connected: boolean;
  /** Replay source: reapply/reset/poll are refused with 409, so disable them. */
  passive: boolean;
  source: string;
  capabilities: Capabilities | null;
  firmware: Firmware;
  driver?: DriverSummary;
  /** INS rovers only: the unit, its configuration report and status; null on u-blox. */
  ins?: InsBlock | null;
}

export interface ReapplyResponse {
  ok: boolean;
  capabilities: Capabilities;
}

export type ResetKind = "hot" | "warm" | "cold" | "factory";

export interface ResetResponse {
  ok: boolean;
  kind: ResetKind;
}

/** The parsed fields of the polled message plus `identity` ("ACK-NAK" when the firmware refused). */
export type PollResponse = { identity: string } & Record<string, unknown>;

export type BaseMode = "survey-in" | "fixed" | "off";

/** `GET /api/base/mode`, and the answer of PUT mode and POST survey/restart. */
export interface BaseModeView {
  /** false: rover role or replay source — no base-mode manager. */
  available: boolean;
  mode: BaseMode;
  /** The site the receiver was actually put on; null without a manager. */
  site: string | null;
  verified: boolean;
  last_1005: { station_id: number; x: number; y: number; z: number } | null;
  svin: { min_duration_s: number; acc_limit_m: number };
}

/** POST sites, POST survey/freeze, POST sites/{name}/activate all answer this. */
export interface SiteResult {
  site: Site;
  /** True when the receiver is sitting on this site right now. */
  applied: boolean;
}

/** `GET /api/ntrip` — with no caster the live fields are null. */
export interface NtripInfo {
  running: boolean;
  host: string;
  port: number;
  /** tailscale | lan | all | an IP */
  bind_mode: string;
  mountpoint: string;
  anonymous: boolean;
  username: string | null;
  /** password masked */
  connection_url: string;
  clients: number | null;
  max_clients: number;
  rejected: number | null;
  sourcetable: string | null;
}

export interface LogFile {
  name: string;
  hour_utc: string;
  bytes: number;
  complete: boolean;
  keep: boolean;
  /** The hour the raw logger has open right now. */
  open: boolean;
  /** Sorted by message name. */
  msg_counts: Record<string, number>;
  start_utc: string | null;
  end_utc: string | null;
  /** The base site the hour was logged at; null in survey-in, on a rover, or in an old log. */
  site: string | null;
}

export interface LogsResponse {
  files: LogFile[];
  total_bytes: number;
  hours: number;
  disk_free_gb: number;
  /** Retention prunes below this; warn when `disk_free_gb < min_free_gb`. */
  min_free_gb: number;
}

export interface HourSlot {
  hour_utc: string;
  available: boolean;
  bytes: number;
  complete: boolean;
}

export type Resolution = "1s" | "1m";

/**
 * `GET /api/history`. `ts` is always the first column. At `res=1m` a 1 s name is mapped to its
 * rollup column and `columns` carries the mapped name (`h_acc_m` → `h_acc_avg`).
 */
export interface HistoryResponse {
  res: Resolution;
  columns: string[];
  rows: (number | null)[][];
}

/** `GET /api/history/metrics` — what each resolution accepts (`ts` not listed). */
export type HistoryMetrics = Record<Resolution, string[]>;

export interface ConfigChange {
  changed: string[];
  restart_required: boolean;
}

export type Role = "base" | "rover";
export type DynModel = "portable" | "stationary" | "pedestrian" | "automotive" | "airborne1g" | "airborne2g" | "airborne4g";

/**
 * Every `Settings` field, as `GET /api/config` returns it (secrets masked to `"***"`, the
 * password inside `ntrip_url` masked). The whole object can be posted back as a no-op.
 */
export interface ConfigValues {
  role: Role;
  mtrtk_source: string;
  baud: number;
  data_dir: string;
  /** read-only */
  mtrtk_env_file: string;
  station_id: string;
  country: string;
  marker_name: string;
  antenna_type: string;
  antenna_height_m: number;
  observer: string;
  agency: string;
  receiver_strict: boolean;
  receiver_ack_timeout_s: number;
  replay_speed: number;
  replay_loop: boolean;
  replay_log: boolean;
  base_mode: BaseMode;
  svin_min_duration_s: number;
  svin_acc_limit_m: number;
  active_site: string | null;
  rtcm_msm: 4 | 7;
  rtcm_1230_rate: number;
  rtcm_station_id: number;
  ntrip_bind: string;
  ntrip_port: number;
  mountpoint: string;
  ntrip_user: string;
  /** secret: `"***"` when set, `""` anonymous, null undecided */
  ntrip_password: string | null;
  ntrip_max_clients: number;
  web_bind: string;
  web_port: number;
  /** secret: `"***"` when set */
  web_password: string | null;
  web_allow_insecure: boolean;
  web_allowed_hosts: string[];
  log_messages: string[];
  min_free_gb: number;
  fsync_interval_s: number;
  rover_driver: "ublox" | "sbg_ellipse" | "vectornav";
  rover_nav_hz: number;
  rover_dynmodel: DynModel;
  /** url-secret: only the password part is masked */
  ntrip_url: string | null;
  ntrip_gga_interval_s: number;
  nmea_tcp_port: number;
  nmea_tcp_bind: string;
  nmea_tcp_max_clients: number;
  nmea_sentences: string[];
  nmea_slow_interval_s: number;
  nmea_udp_targets: string[];
  nmea_serial: string | null;
  nmea_serial_baud: number;
  json_udp_port: number | null;
  point_epochs: number;
  point_fixed_only: boolean;
  /** secret */
  alert_webhook_url: string | null;
  public_domain: string | null;
  tunnel_token: string | null;
  ins_port: string | null;
  ins_baud: number;
  ins_rtcm_port: string | null;
  /** null: Port B opens at `ins_baud` */
  ins_rtcm_baud: number | null;
  ins_output_hz: number;
  ins_apply_config: boolean;
  ins_raw_gnss: boolean;
  /** [x, y, z] metres; the PUT also takes "x,y,z" */
  ins_lever_arm_gnss1: [number, number, number] | null;
  ins_lever_arm_gnss2: [number, number, number] | null;
  ins_imu_lever_arm: [number, number, number] | null;
  ins_imu_axis: string;
  ins_motion_profile: "general" | "automotive" | "marine" | "airplane" | "helicopter" | "uav" | "pedestrian";
  /** [lat, lon, alt] */
  ins_init_position: [number, number, number] | null;
  ins_vn_rtcm: boolean;
  ins_vn_scenario: number | null;
  ins_vn_ahrs_aiding: boolean | null;
  ins_vn_ref_rotation: string | null;
  ins_vn_vpe: string | null;
  log_level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
}

export type ConfigKey = keyof ConfigValues;

/** `GET /api/config` */
export interface ConfigResponse {
  values: ConfigValues;
  /** What `.env` says and the running process does not: applied by a restart. */
  pending: Partial<ConfigValues>;
  env_file: string;
  secret_keys: ConfigKey[];
  live_keys: ConfigKey[];
  read_only_keys: ConfigKey[];
  url_secret_keys: ConfigKey[];
}

// ------------------------------------------------------------ request bodies

export interface LoginBody {
  password: string;
}
export interface ConfigBody {
  values: Partial<ConfigValues>;
}
export interface ResetBody {
  kind: ResetKind;
}
export interface PollBody {
  /** e.g. "MON" */
  msg_class: string;
  /** e.g. "MON-VER" */
  msg_id: string;
}
export interface ModeBody {
  mode: BaseMode;
  svin_min_duration_s?: number;
  svin_acc_limit_m?: number;
  /** Only with `mode: "fixed"`. */
  site?: string;
}
export interface FreezeBody {
  name: string;
  activate?: boolean;
}
/** A site as `x,y,z` (ECEF metres) or as `lat,lon,height_m`; half a coordinate is a 422. */
export interface SiteBody {
  name: string;
  x?: number | null;
  y?: number | null;
  z?: number | null;
  lat?: number | null;
  lon?: number | null;
  height_m?: number | null;
  sigma_m?: number | null;
  /** Per-axis ECEF 1-sigma (metres), as a PPP import reports them; each wins over `sigma_m`. */
  sigma_x?: number | null;
  sigma_y?: number | null;
  sigma_z?: number | null;
  source?: string;
  frame?: string;
  epoch?: string | null;
  notes?: string | null;
}
export interface KeepBody {
  keep: boolean;
}
/** `POST /api/export`: a UTC window and a preset; the overrides only apply to `generic`. */
export interface ExportRequest {
  start: string;
  end: string;
  preset?: string;
  interval_s?: number | null;
  hatanaka?: boolean | null;
  gzip?: boolean | null;
  include_nav?: boolean;
}

/** `POST /api/rover/collect`: average epochs into a named point (rover role). */
export interface CollectBody {
  name: string;
  code?: string | null;
  note?: string | null;
  epochs?: number | null;
  fixed_only?: boolean | null;
}
/** `PUT /api/rover/ntrip`: switch the NTRIP caster (`ntrip://user:pass@host:port/mount`). */
export interface NtripUrlBody {
  url: string;
}
/** `POST /api/rover/sessions`: start a survey session. */
export interface SessionBody {
  name?: string | null;
  notes?: string | null;
}
/** `PATCH /api/rover/points/{point_id}`: `null` (or a missing key) leaves a field as it is. */
export interface PointPatch {
  name?: string | null;
  code?: string | null;
  note?: string | null;
}

/** `GET /api/export/presets` items (`mtrtk.rinex.presets.Preset`); tuples arrive as JSON lists. */
export interface Preset {
  /** Stable id used by the API, the CLI and the UI: csrs-ppp | auspos | opus | generic. */
  id: string;
  name: string;
  /** Empty for `generic`, which targets no service. */
  service_url: string;
  description: string;
  /** RINEX version, "3.04" or "2.11". */
  version: string;
  /** Seconds; null keeps the receiver's native rate. */
  interval_s: number | null;
  /** RINEX system letters left out (OPUS: everything but GPS). */
  exclude_systems: string[];
  hatanaka: boolean;
  gzip: boolean;
  /** What the service asks of the data, worded for the operator. */
  constraints: string[];
  /** Only an adjustable preset takes interval/Hatanaka/gzip overrides. */
  adjustable: boolean;
}

/** `POST /api/base/ppp/import`: a parsed result plus the name the daemon suggests. Nothing is saved yet. */
export interface PppResult {
  /** csrs-ppp | auspos | opus */
  source: string;
  /** The parser that read it, e.g. "csrs-sum". */
  format: string;
  /** As reported, e.g. "ITRF2020". */
  frame: string;
  /** As reported, e.g. "2026.71". */
  epoch: string | null;
  x: number;
  y: number;
  z: number;
  /** Per-axis ECEF 1-sigma in metres (CSRS-PPP's 95 % already divided by 1.96). */
  sigma_x: number | null;
  sigma_y: number | null;
  sigma_z: number | null;
  lat: number;
  lon: number;
  height_m: number;
  notes: string[];
  suggested_name: string;
}

/** The 422 `detail` of a PPP import the parser could not read. */
export interface PppImportRefusal {
  message: string;
  hint: string;
  /** The file's first 200 characters. */
  head: string;
}

/** One entry of an export job's `result.files` (the manifest's list). */
export interface ExportFile {
  name: string;
  bytes: number;
  /** obs | nav | manifest */
  role: string;
}

/** One entry of a 422 `detail` list. Never carries `input`. */
export interface ValidationIssue {
  loc: (string | number)[];
  msg: string;
  type: string;
}

// ----------------------------------------------------------------- WebSocket

export const WS_TOPICS = ["pvt", "sats", "rtcm", "svin", "rf", "span", "ntrip", "events", "system", "receiver", "base", "jobs", "rawlog", "daemon", "rtk", "survey", "ins"] as const;
export type WsTopic = (typeof WS_TOPICS)[number];

/** The sections an `epoch` bundle may carry; only subscribed ones are present. */
export interface EpochSections {
  pvt?: Pick<ReceiverState, "position" | "accuracy" | "dops" | "fix" | "velocity" | "time">;
  sats?: Pick<ReceiverState, "sats" | "sat_summary">;
  rtcm?: RtcmStats;
  svin?: SurveyIn;
  rtk?: RtkStatus;
  /** INS rovers: filter status, IMU sample and attitude at the epoch (null on u-blox). */
  ins?: { ins: InsStatus | null; imu: ImuSample | null; attitude: Attitude | null };
}

export interface WsSnapshot {
  type: "snapshot";
  role: Role;
  topics: string[];
  state: ReceiverState;
}

export interface WsEpoch extends EpochSections {
  type: "epoch";
  /** Receiver UTC as epoch seconds, or null before the first time fix. */
  t: number | null;
}

/**
 * Everything that is not the snapshot or an epoch. `topic` is the subscription name and
 * `source` the bus topic underneath it — which is how `receiver.connected` is told from
 * `receiver.disconnected`. Payloads by source:
 *
 * | source | data |
 * | --- | --- |
 * | `state.hardware` | `Hardware` |
 * | `state.rf` | `RfBlock[]` |
 * | `state.spectrum` | `Spectrum[]` (throttled to one per second) |
 * | `ntrip.clients` | `NtripClient[]` |
 * | `events.new` | `EventItem` |
 * | `system.stats` | `SystemStats` |
 * | `jobs.update` | `Job` |
 * | `receiver.connected` | source name, e.g. `"serial:/dev/ttyACM0"` |
 * | `receiver.disconnected` | reason string |
 * | `receiver.error` | reason string |
 * | `receiver.capabilities` | `Capabilities` |
 * | `receiver.reset` | `{ kind: ResetKind }` |
 * | `base.mode` | `BaseModeEvent` |
 * | `base.site_verified` | `SiteCheck` (dx/dy/dz) |
 * | `base.site_mismatch` | `SiteCheck` (dx/dy/dz, or `reason`) |
 * | `rawlog.rotated` / `rawlog.closed` / `rawlog.pruned` | file path string |
 * | `rawlog.error` | string |
 * | `rawlog.backpressure` / `rawlog.drained` | `{ queued: number }` |
 * | `daemon.consumer_failed` | `ConsumerFailed` |
 * | `ntrip_client.status` (topic `rtk`) | `NtripClientStatus` |
 * | `state.time_mark` (topic `rtk`) | `TimeMark` |
 * | `points.progress` (topic `survey`) | `CollectStatus` |
 * | `points.saved` (topic `survey`) | `Point` |
 * | `ins.config` (topic `ins`) | `InsConfigReport` |
 */
export interface WsUpdate {
  type: "update";
  topic: WsTopic | string;
  source: string;
  data: unknown;
}

export type WsMessage = WsSnapshot | WsEpoch | WsUpdate;

export interface BaseModeEvent {
  mode: BaseMode;
  site: string | null;
  /** Why the receiver ended up here when it was refused something; null on success. */
  reason: string | null;
}

export interface SiteCheck {
  site: string;
  dx?: number;
  dy?: number;
  dz?: number;
  reason?: string;
}

export interface ConsumerFailed {
  name: string;
  /** `"TypeName: message"` */
  error: string;
}

export interface RawlogQueue {
  queued: number;
}

// --------------------------------------------------------------------- rover

/** RTCM the rover's receiver reports having received (UBX-RXM-RTCM), per message type. */
export interface RtcmRxStats {
  count: number;
  used: number;
  crc_failed: number;
  last_seen_mono: number | null;
}

/** `ReceiverState.rtk` (`mtrtk.core.state.RtkStatus`); rides the epoch bundle as `rtk`. */
export interface RtkStatus {
  carr_soln: number;
  carr_soln_name: string;
  diff_soln: boolean;
  rel_pos_n_m: number | null;
  rel_pos_e_m: number | null;
  rel_pos_d_m: number | null;
  baseline_m: number | null;
  heading_deg: number | null;
  heading_valid: boolean;
  acc_n_m: number | null;
  acc_e_m: number | null;
  acc_d_m: number | null;
  acc_length_m: number | null;
  acc_heading_deg: number | null;
  ref_station_id: number | null;
  rel_pos_valid: boolean;
  is_moving: boolean;
  ref_pos_missing: boolean;
  ref_obs_missing: boolean;
  normalized: boolean;
  /** NAV-PVT's lastCorrectionAge bucket, decoded to its upper bound. */
  corr_age_receiver_s: number | null;
  /** Seconds since the daemon last injected RTCM. */
  corr_age_s: number | null;
  /** Keyed by RTCM message number (a string on the wire). */
  rtcm_rx: Record<string, RtcmRxStats>;
  rtcm_rx_total: number;
  rtcm_crc_failed: number;
  last_rtcm_mono: number | null;
}

/** One UBX-TIM-TM2 event (an EXTINT edge); the `state.time_mark` update payload. */
export interface TimeMark {
  channel: number;
  count: number;
  rising_week: number | null;
  rising_tow_s: number | null;
  falling_week: number | null;
  falling_tow_s: number | null;
  new_rising: boolean;
  new_falling: boolean;
  /** 0 receiver, 1 GNSS, 2 UTC. */
  time_base: number;
  utc_based: boolean;
  acc_est_ns: number;
  rising_utc: string | null;
}

export interface Attitude {
  roll_deg: number | null;
  pitch_deg: number | null;
  heading_deg: number | null;
  acc_roll_deg: number | null;
  acc_pitch_deg: number | null;
  acc_heading_deg: number | null;
  source: string;
}

/**
 * The rover's NTRIP client (`mtrtk.rover.ntrip_client.NtripClientStatus`), from `GET /api/rover`
 * and the `ntrip_client.status` update. Both add `last_rtcm_age_s` and `connected_for_s`: the
 * `*_mono` fields are the daemon's monotonic clock, meaningless here. `next_retry_s` is the
 * backoff delay of the pending reconnect.
 */
export interface NtripClientStatus {
  connected: boolean;
  host: string;
  port: number;
  mountpoint: string;
  version: number | null;
  bytes_received: number;
  frames_injected: number;
  crc_dropped: number;
  last_rtcm_mono: number | null;
  last_error: string | null;
  reconnects: number;
  next_retry_s: number | null;
  since_mono: number | null;
  last_rtcm_age_s: number | null;
  connected_for_s: number | null;
}

export type CollectState = "idle" | "collecting" | "done" | "aborted";

/** `GET/POST/DELETE /api/rover/collect` and the `points.progress` update payload. */
export interface CollectStatus {
  state: CollectState;
  name: string | null;
  target: number;
  accepted: number;
  skipped: number;
  sd_n: number | null;
  sd_e: number | null;
  sd_u: number | null;
  mean_lat: number | null;
  mean_lon: number | null;
  mean_h: number | null;
  point_id: number | null;
  reason: string | null;
}

/** `GET /api/rover/sessions` items (`mtrtk.store.models.Session`). */
export interface Session {
  id: number;
  name: string | null;
  start_utc: string;
  end_utc: string | null;
  role: string | null;
  notes: string | null;
}

/** `GET /api/rover/points` items and the `points.saved` update payload (`mtrtk.store.models.Point`). */
export interface Point {
  id: number;
  session_id: number | null;
  name: string;
  code: string | null;
  note: string | null;
  ts_utc: string;
  lat: number;
  lon: number;
  height_m: number;
  hmsl_m: number | null;
  n_epochs: number;
  /** Sample standard deviations of the averaged epochs, metres. */
  sd_n: number;
  sd_e: number;
  sd_u: number;
  /** The worst over the accepted epochs. */
  fix_type: number;
  carr_soln: number;
  h_acc_m: number | null;
  v_acc_m: number | null;
}

export interface RoverOutputs {
  nmea_tcp: { port: number; clients: number } | null;
  nmea_udp: string[];
  nmea_serial: string | null;
  json_udp: number | null;
  sentences: string[];
}

/** `GET /api/rover` (409 on a base). */
export interface RoverOverview {
  role: string;
  driver: { name: string; capabilities: Record<string, boolean>; rtcm_unverified?: boolean };
  ntrip: NtripClientStatus | null;
  /** The configured caster URL, password masked as `***`; null when none is set. */
  ntrip_url: string | null;
  rtk: RtkStatus;
  outputs: RoverOutputs;
  session: Session | null;
  collect: CollectStatus;
  /** POINT_EPOCHS / POINT_FIXED_ONLY: what the Survey form starts from. */
  collect_defaults?: { epochs: number; fixed_only: boolean };
}

// ------------------------------------------------------------------------------ PPK

/** `GET /api/ppk/defaults`: what this host can run and what a job starts from. */
export interface PpkDefaults {
  rnx2rtkp: boolean;
  convbin: boolean;
  /** The rnx2rtkp on this host is RTKLIB demo5 (better ambiguity resolution than stock 2.4.3). */
  demo5: boolean;
  /** The rnx2rtkp option file a job starts from, before `conf_overrides`. */
  conf: Record<string, string>;
  /** The base's web address guessed from `NTRIP_URL` (`http://<caster host>:8080`); null without one. */
  ntrip_base_url: string | null;
  max_upload_bytes: number;
}

/** `POST /api/ppk/upload` (multipart `kind`, `file`). */
export interface PpkUpload {
  upload_id: string;
  name: string;
  bytes: number;
  detected: "ubx" | "rinex";
  /** For a RINEX file: observations or navigation. */
  rinex: "obs" | "nav" | null;
  kind: "rover" | "base";
}

export interface PpkRoverBody {
  kind: "session" | "window" | "upload";
  session_id?: number | null;
  start?: string | null;
  end?: string | null;
  upload_id?: string | null;
}

export interface PpkBaseBody {
  kind: "local" | "remote" | "upload";
  url?: string | null;
  upload_id?: string | null;
  nav_upload_id?: string | null;
  /** The remote base's WEB_PASSWORD; used by the job, never stored. */
  password?: string | null;
}

/** `POST /api/ppk` body. */
export interface PpkSubmit {
  rover: PpkRoverBody;
  base: PpkBaseBody;
  base_site?: string | null;
  base_xyz?: [number, number, number] | null;
  events?: boolean;
  include_qzss?: boolean;
  conf_overrides?: Record<string, string>;
}

/** `summary.json`'s `summary` (`mtrtk.ppk.pos.PpkSummary.to_json`). Times are GPST. */
export interface PpkSummary {
  epochs: number;
  duration_s: number;
  interval_s: number;
  fixed_pct: number;
  float_pct: number;
  single_pct: number;
  /** Mean σ of the fixed epochs, metres. */
  mean_sd_fixed: { n: number; e: number; u: number } | null;
  /** The clock of every time here: GPST, written with no UTC offset. */
  time_system?: "GPST";
  /** [from, to, seconds] of every gap longer than the job's limit (GPST). */
  gaps: [string, string, number][];
  /** GPST, no offset ("2026-09-18T10:00:18"); older jobs wrote "+00:00" on the same GPST value. */
  first_time: string | null;
  last_time: string | null;
}

export interface PpkEventCounts {
  total: number;
  ok: number;
  gap_too_large: number;
  no_neighbours: number;
}

/** A done PPK job's `result` (the run's `summary.json`). */
export interface PpkResult {
  summary: PpkSummary;
  events: PpkEventCounts;
  warnings: string[];
  inputs: Record<string, unknown>;
  files: JobFile[];
}

// ---------------------------------------------------------------- INS rovers

/** `ReceiverState.ins`: an SBG or VectorNav unit's filter state and health. */
export interface InsStatus {
  /** "sbg" | "vectornav" */
  vendor: string;
  /** Vendor filter mode: SBG EKF 0..4 (4 = nav position), VectorNav 0..2 (2 = tracking). */
  mode: number | null;
  mode_name: string;
  /** SBG STATUS general flags, true = OK. */
  general_ok: Record<string, boolean>;
  /** Aiding sources the unit reports receiving. */
  aiding: Record<string, boolean>;
  /** VectorNav InsStatus error bits, true = the unit flags an error. */
  errors: Record<string, boolean>;
  uptime_s: number | null;
  cpu_pct: number | null;
  com_status: number | null;
  /** The unit's own GNSS fix code (0 = no solution). */
  gnss_fix: number | null;
  gnss_fix_name: string;
  gnss_vel: Velocity | null;
  /** A dual-antenna unit's own GNSS heading (SBG GPS1_HDT), not the bearing to the base. */
  gnss_heading_deg: number | null;
  gnss_heading_acc_deg: number | null;
  gnss_heading_valid: boolean;
  /** The separation of the unit's two antennas, not the distance to the base. */
  antenna_baseline_m: number | null;
}

/** `ReceiverState.imu`: body frame. */
export interface ImuSample {
  accel_mps2: [number, number, number] | null;
  gyro_radps: [number, number, number] | null;
  temperature_c: number | null;
  timestamp_us: number | null;
}

export interface DriverCapabilities {
  accepts_rtcm: boolean;
  raw_gnss_log: boolean;
  attitude: boolean;
  imu: boolean;
  sats: boolean;
  spectrum: boolean;
}

export interface DriverSummary {
  name: string;
  capabilities: DriverCapabilities;
}

export interface InsInfo {
  model: string;
  serial: string;
  firmware: string;
  hardware: string;
  /** The vendor's own identity fields. */
  details: Record<string, unknown>;
}

export type InsItemState = "applied" | "unchanged" | "pending" | "mismatched" | "unsupported" | "error";

export interface InsConfigItem {
  name: string;
  state: InsItemState;
  current: unknown;
  wanted: unknown;
  error?: string;
}

/** A vendor configuration report in the daemon's common shape. */
export interface InsConfigReport {
  items: InsConfigItem[];
  applied: string[];
  unchanged: string[];
  pending: string[];
  mismatched: string[];
  unsupported: string[];
  errors: string[];
  notes: string[];
  /** Saved to the unit's flash by this configure run. */
  saved: boolean;
  current: Record<string, unknown>;
  wanted: Record<string, unknown>;
}

export interface InsLeverArm {
  /** "gnss1" | "gnss2" | "imu" */
  name: string;
  /** From INS_LEVER_ARM_* (metres, x/y/z), null when not set. */
  configured: [number, number, number] | null;
  /** What the unit last read back, null before a read. */
  read_back: number[] | null;
}

/** `GET /api/receiver`'s `ins` block. */
export interface InsBlock {
  vendor: string;
  driver: string;
  connected: boolean;
  port: string | null;
  /** INS_APPLY_CONFIG: false means apply needs an explicit force (the confirm dialog). */
  apply_config: boolean;
  info: InsInfo | null;
  config_report: InsConfigReport | null;
  lever_arms: InsLeverArm[];
  status: InsStatus | null;
  rtcm_unverified: boolean;
  dropped_rtcm_bytes: number;
  raw_gnss_format: string | null;
  stats: Record<string, number>;
  /** Settings already went to flash once since mtrtk started: a further apply lasts until the unit restarts. */
  saved_this_run?: boolean;
}

/** `POST /api/receiver/profile`: apply (or re-read, `apply: false`) the INS profile. */
export interface ProfileBody {
  apply: boolean;
  force: boolean;
}

export interface ProfileResponse {
  ok: boolean;
  applied: boolean;
  report: InsConfigReport | null;
}
