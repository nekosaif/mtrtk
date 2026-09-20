/**
 * Shared fixtures for page tests. `sampleState()` is one `ReceiverState` as the daemon would
 * send it (field names from `@/lib/types`): a 3D fix in Dhaka, eight satellites across four
 * systems (six used), a survey-in running, RTCM flowing. Every call returns a fresh object, so a
 * test may mutate its copy.
 */
import type { NtripClient, ReceiverState, Satellite } from "@/lib/types";

export function sat(gnss_id: number, gnss: string, sv_id: number, cno: number, elev: number | null, azim: number | null, used = true): Satellite {
  return {
    gnss_id, gnss, sv_id, cno, elev, azim, pr_res_m: 0.3, quality_ind: 7, used, health: 1, diff_corr: false, smoothed: false, orbit_source: 1, eph_avail: true, alm_avail: true,
    signals: [{ sig_id: 0, name: "L1C/A", freq_id: 0, cno, pr_res_m: 0.3, quality_ind: 7, corr_source: 0, iono_model: 0, health: 1, pr_used: used, cr_used: used, do_used: used }],
  };
}

export function sampleState(): ReceiverState {
  const sats = [
    sat(0, "GPS", 5, 45, 72, 120), sat(0, "GPS", 12, 38, 35, 210), sat(0, "GPS", 25, 22, 8, 300, false),
    sat(6, "GLONASS", 3, 40, 50, 40), sat(6, "GLONASS", 9, 30, 20, 330),
    sat(2, "Galileo", 4, 42, 60, 180), sat(3, "BeiDou", 21, 36, 44, 260), sat(5, "QZSS", 194, 28, 15, 95, false),
  ];
  return {
    connected: true, source: "serial:/dev/ttyACM0", epoch_count: 120, raw_epochs: 120, last_epoch_mono: 1,
    position: { lat: 23.8373506, lon: 90.2625502, height_m: -36.268, hmsl_m: 13.363, ecef_x_m: -26748.172, ecef_y_m: 5837156.618, ecef_z_m: 2561801.261, invalid_llh: false },
    accuracy: { h_acc_m: 0.012, v_acc_m: 0.018, p_acc_m: 0.02, t_acc_ns: 20, s_acc_mps: 0.01, head_acc_deg: 1 },
    dops: { g: 1.5, p: 1.2, t: 0.8, v: 1.0, h: 0.7, n: 0.5, e: 0.4 },
    fix: { fix_type: 3, fix_type_name: "3D", gnss_fix_ok: true, diff_soln: false, carr_soln: 0, carr_soln_name: "None", num_sv: 6, last_correction_age: 0, psm_state: 0, spoof_det_state: 0, ttff_ms: 2500, uptime_ms: 600000 },
    velocity: { vel_n_mps: 0, vel_e_mps: 0, vel_d_mps: 0, ground_speed_mps: 0, heading_motion_deg: 0 },
    time: { utc: "2026-09-18T16:47:34+00:00", itow_ms: 492472000, gps_week: 2436, gps_tow_s: 492472, leap_s: 18, valid_date: true, valid_time: true, fully_resolved: true, valid_utc: true, utc_standard: 3, t_acc_ns: 20, clk_bias_ns: 1500, clk_drift_nsps: -7, f_acc_psps: 300, leap_source: 2, time_to_leap_event_s: 100000, leap_change: 0 },
    sats,
    sat_summary: { tracked: 8, used: 6, per_gnss: { GPS: { tracked: 3, used: 2 }, GLONASS: { tracked: 2, used: 2 }, Galileo: { tracked: 1, used: 1 }, BeiDou: { tracked: 1, used: 1 }, QZSS: { tracked: 1, used: 0 } } },
    hardware: { ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "On", noise_per_ms: 90, agc_cnt: 3000, jam_ind: 12, jamming_state: 1, jamming_state_name: "OK", rtc_calib: true, safe_boot: false, xtal_absent: false },
    rf: [
      { block_id: 0, jamming_state: 1, jamming_state_name: "OK", ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "On", post_status: 0, noise_per_ms: 80, agc_cnt: 4000, jam_ind: 10, ofs_i: 1, mag_i: 100, ofs_q: -1, mag_q: 99 },
      { block_id: 1, jamming_state: 1, jamming_state_name: "OK", ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "On", post_status: 0, noise_per_ms: 70, agc_cnt: 5000, jam_ind: 5, ofs_i: 0, mag_i: 90, ofs_q: 0, mag_q: 91 },
    ],
    spectrum: [{ block_id: 0, span_hz: 100_000_000, res_hz: 390_625, center_hz: 1_580_000_000, pga_db: 20, bins: Array.from({ length: 256 }, (_, i) => 60 + Math.round(30 * Math.exp(-((i - 128) ** 2) / 800))) }],
    ports: [{ port_id: 0x0300, tx_pending: 10, tx_bytes: 123456, tx_usage: 5, tx_peak_usage: 40, rx_pending: 0, rx_bytes: 999, rx_usage: 1, rx_peak_usage: 3, overrun_errs: 0, skipped: 0 }],
    survey_in: { active: true, valid: false, dur_s: 120, obs: 118, mean_x_m: -26748.1, mean_y_m: 5837156.6, mean_z_m: 2561801.3, mean_acc_m: 1.9 },
    rtcm_out: { messages: { "1005": { count: 120, bytes: 3000, last_seen_mono: 1 }, "1077": { count: 120, bytes: 40000, last_seen_mono: 1 }, "1230": { count: 24, bytes: 400, last_seen_mono: 1 } }, total_count: 264, total_bytes: 43400, bytes_per_s: 1900 },
    firmware: { sw_version: "EXT CORE 1.00 (f10c36)", hw_version: "00190000", fw_version: "HPG 1.13", protver: "27.12", module: "ZED-F9P", extensions: ["FWVER=HPG 1.13", "PROTVER=27.12"] },
  };
}

/** One connected NTRIP rover, with a GGA position 30 m north of the base unless overridden. */
export function sampleRover(overrides: Partial<NtripClient> = {}): NtripClient {
  return {
    id: 1, ip: "100.64.0.7", port: 51234, mountpoint: "MTRTK", user_agent: "NTRIP u-center/23.08", username: null, version: 2,
    connected_utc: "2026-09-18T16:40:00+00:00", bytes_sent: 120_000, dropped_frames: 0,
    last_gga_lat: 23.83762, last_gga_lon: 90.26255, last_gga_utc: "2026-09-18T16:47:33+00:00",
    ...overrides,
  };
}
