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
  id: number;
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
  nmea_udp_targets: string[];
  nmea_serial: string | null;
  json_udp_port: number | null;
  /** secret */
  alert_webhook_url: string | null;
  public_domain: string | null;
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
  source?: string;
  frame?: string;
  epoch?: string | null;
  notes?: string | null;
}
export interface KeepBody {
  keep: boolean;
}

/** One entry of a 422 `detail` list. Never carries `input`. */
export interface ValidationIssue {
  loc: (string | number)[];
  msg: string;
  type: string;
}

// ----------------------------------------------------------------- WebSocket

export const WS_TOPICS = ["pvt", "sats", "rtcm", "svin", "rf", "span", "ntrip", "events", "system", "receiver", "base", "jobs", "rawlog", "daemon"] as const;
export type WsTopic = (typeof WS_TOPICS)[number];

/** The sections an `epoch` bundle may carry; only subscribed ones are present. */
export interface EpochSections {
  pvt?: Pick<ReceiverState, "position" | "accuracy" | "dops" | "fix" | "velocity" | "time">;
  sats?: Pick<ReceiverState, "sats" | "sat_summary">;
  rtcm?: RtcmStats;
  svin?: SurveyIn;
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
