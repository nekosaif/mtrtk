import { render, screen, within } from "@testing-library/react";
import { Tape } from "./Tape";
import { resetLiveForTests, useLive } from "@/lib/live";
import type { ReceiverState } from "@/lib/types";

function state(): ReceiverState {
  return {
    connected: true, source: "serial:/dev/ttyACM0", epoch_count: 10, raw_epochs: 10, last_epoch_mono: null,
    position: { lat: 23.8, lon: 90.2, height_m: -36, hmsl_m: 13, ecef_x_m: null, ecef_y_m: null, ecef_z_m: null, invalid_llh: false },
    accuracy: { h_acc_m: 0.02, v_acc_m: 0.03, p_acc_m: null, t_acc_ns: null, s_acc_mps: null, head_acc_deg: null },
    dops: { g: null, p: 1.2, t: null, v: null, h: null, n: null, e: null },
    fix: { fix_type: 3, fix_type_name: "3D", gnss_fix_ok: true, diff_soln: true, carr_soln: 2, carr_soln_name: "RTK fixed", num_sv: 20, last_correction_age: 0, psm_state: 0, spoof_det_state: 0, ttff_ms: null, uptime_ms: null },
    velocity: { vel_n_mps: 0, vel_e_mps: 0, vel_d_mps: 0, ground_speed_mps: 0, heading_motion_deg: 0 },
    time: { utc: "2026-09-18T16:47:34+00:00", itow_ms: 1, gps_week: null, gps_tow_s: null, leap_s: null, valid_date: true, valid_time: true, fully_resolved: true, valid_utc: true, utc_standard: null, t_acc_ns: null, clk_bias_ns: null, clk_drift_nsps: null, f_acc_psps: null, leap_source: null, time_to_leap_event_s: null, leap_change: null },
    sats: [], sat_summary: { tracked: 25, used: 20, per_gnss: {} }, hardware: null, rf: [], spectrum: [], ports: [],
    survey_in: { active: false, valid: false, dur_s: 0, obs: 0, mean_x_m: null, mean_y_m: null, mean_z_m: null, mean_acc_m: null },
    rtcm_out: { messages: {}, total_count: 100, total_bytes: 12_000, bytes_per_s: 1228.8 },
    firmware: { sw_version: "", hw_version: "", fw_version: "HPG 1.13", protver: "27.12", module: "ZED-F9P", extensions: [] },
  };
}

describe("Tape", () => {
  beforeEach(() => resetLiveForTests());

  it("shows six readings from the live state, every number in tabular figures", () => {
    useLive.setState({ status: "open", connected: true, stale: false, state: state(), lastEpochAt: Date.now(), receiverConnected: true, ntripClients: [{ id: 1 } as never, { id: 2 } as never] });
    render(<Tape />);
    const tape = screen.getByRole("status");
    expect(tape).toHaveTextContent("16:47:34 UTC"); // receiver time, not the browser clock
    expect(tape).toHaveTextContent("RTK fixed");
    expect(tape).toHaveTextContent("sats 20/25");
    expect(tape).toHaveTextContent("hAcc 2.0 cm");
    expect(tape).toHaveTextContent("RTCM 1.2 kB/s");
    expect(tape).toHaveTextContent("2 rovers");
    expect(tape).toHaveTextContent(/live$/);
    const numeric = within(tape).getAllByTestId("reading");
    expect(numeric).toHaveLength(5); // clock, sats, hAcc, RTCM, rovers; the fix is a badge
    for (const el of numeric) expect(el.className).toContain("num");
    for (const el of numeric) expect(el.className).not.toContain("text-ink-3");
  });

  it("greys the readings and says so when no epoch has arrived for 5 s", () => {
    useLive.setState({ status: "open", connected: true, stale: true, state: state(), lastEpochAt: Date.now() - 6000, receiverConnected: true, ntripClients: [] });
    render(<Tape />);
    const tape = screen.getByRole("status");
    for (const el of within(tape).getAllByTestId("reading")) expect(el.className).toContain("text-ink-3");
    expect(tape).toHaveTextContent("Waiting for data");
    expect(tape).toHaveTextContent("waiting for epochs");
    expect(tape).toHaveTextContent("0 rovers");
  });

  it("shows a visible reconnecting state and greys the last known readings", () => {
    useLive.setState({ status: "reconnecting", connected: false, stale: false, state: state(), lastEpochAt: Date.now(), receiverConnected: true, attempts: 3 });
    render(<Tape />);
    const tape = screen.getByRole("status");
    expect(tape).toHaveTextContent(/reconnecting/);
    for (const el of within(tape).getAllByTestId("reading")) expect(el.className).toContain("text-ink-3");
  });

  it("says connecting with the browser clock before any state arrives", () => {
    render(<Tape />);
    const tape = screen.getByRole("status");
    expect(tape).toHaveTextContent(/UTC/);
    expect(tape).toHaveTextContent(/connecting/);
    expect(tape).toHaveTextContent("Waiting for data");
    expect(within(tape).queryByText(/sats/)).toBeNull();
  });

  it("shows the receiver error banner and lets it be dismissed", async () => {
    useLive.setState({ status: "open", connected: true, state: state(), receiverConnected: false, receiverError: "link failure: [Errno 5] Input/output error" });
    render(<Tape />);
    const tape = screen.getByRole("status");
    expect(tape).toHaveTextContent("Receiver disconnected");
    const banner = within(tape).getByRole("alert");
    expect(banner).toHaveTextContent(/link failure/);
    within(banner).getByRole("button", { name: /dismiss/i }).click();
    expect(useLive.getState().receiverError).toBeNull();
  });
});
