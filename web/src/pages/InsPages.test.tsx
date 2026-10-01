/**
 * The INS rover UI across pages: the Receiver page's INS branch, the RTK page's corrections
 * notice, the Dashboard's IMU card, and the live store's INS epoch / `ins.config` handling.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { configureLive, resetLiveForTests, useLive } from "@/lib/live";
import type { InsBlock, InsStatus, ReceiverInfo, ReceiverState } from "@/lib/types";
import { sampleState } from "@/test/fixtures";
import Dashboard from "./Dashboard";
import Receiver from "./Receiver";
import Rtk from "./Rtk";

vi.mock("@/components/MapPanel", () => ({ MapPanel: () => <div data-testid="map" /> }));

const insStatus: InsStatus = {
  vendor: "vectornav",
  mode: 2,
  mode_name: "Tracking",
  general_ok: {},
  aiding: { gps_fix: true },
  errors: { imu: false, gps: false },
  uptime_s: null,
  cpu_pct: null,
  com_status: null,
  gnss_fix: 3,
  gnss_fix_name: "3D",
  gnss_vel: null,
};

const insBlock: InsBlock = {
  vendor: "vectornav",
  driver: "vectornav",
  connected: true,
  port: "/dev/ttyUSB0",
  apply_config: false,
  info: { model: "VN-200T-CR", serial: "0100012345", firmware: "2.0.0.0", hardware: "4", details: {} },
  config_report: {
    items: [{ name: "binary_output_1", state: "pending", current: null, wanted: null }],
    applied: [],
    unchanged: [],
    pending: ["binary_output_1"],
    mismatched: [],
    unsupported: [],
    errors: [],
    notes: ["INS_OUTPUT_HZ=7 is not a VectorNav output rate: using 8 Hz (divisor 100)"],
    saved: false,
    current: {},
    wanted: {},
  },
  lever_arms: [{ name: "gnss1", configured: null, read_back: [0, 0, 0] }],
  status: insStatus,
  rtcm_unverified: false,
  dropped_rtcm_bytes: 0,
  raw_gnss_format: null,
  stats: {},
};

const capsNoRtcm = { accepts_rtcm: false, raw_gnss_log: true, attitude: true, imu: true, sats: true, spectrum: false };

function insState(): ReceiverState {
  const s = sampleState();
  // A Hardware block that would render the u-blox card: the INS page must hide it all the same.
  const hardware = { ant_status: 2, ant_status_name: "OK", ant_power: 1, ant_power_name: "ON", noise_per_ms: 90, agc_cnt: 4000, jam_ind: 3, jamming_state: 1, jamming_state_name: "OK", rtc_calib: true, safe_boot: false, xtal_absent: false };
  return { ...s, rf: [], spectrum: [], hardware, ports: [], ins: insStatus, imu: { accel_mps2: [0, 0, -9.81], gyro_radps: [0, 0, 0], temperature_c: 31.5, timestamp_us: 5 }, attitude: { roll_deg: 0, pitch_deg: 0, heading_deg: 45, acc_roll_deg: null, acc_pitch_deg: null, acc_heading_deg: null, source: "vn-ins" } };
}

type Route = (init?: RequestInit) => unknown;

function mockFetch(routes: Record<string, Route>) {
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = new URL(String(url), "http://x").pathname;
    const route = routes[path];
    if (!route) return new Response(JSON.stringify({ detail: "not found" }), { status: 404 });
    return new Response(JSON.stringify(route(init)), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
}

const posts = (suffix: string) => (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit | undefined][] } }).mock.calls.filter(([u, i]) => String(u).endsWith(suffix) && i?.method === "POST");

function renderWith(page: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>{page}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  resetLiveForTests();
  useLive.setState({ state: insState(), role: "rover", status: "open", connected: true, stale: false, lastEpochAt: Date.now(), receiverConnected: true });
});

describe("Receiver page on an INS rover", () => {
  const info: ReceiverInfo = { connected: true, passive: false, source: "/dev/ttyUSB0", capabilities: null, firmware: sampleState().firmware, driver: { name: "vectornav", capabilities: capsNoRtcm }, ins: insBlock };

  it("shows the INS panels and hides the u-blox RF, spectrum and port cards", async () => {
    mockFetch({ "/api/receiver": () => info });
    renderWith(<Receiver />);
    expect(await screen.findByRole("region", { name: "INS unit" })).toHaveTextContent("VN-200T-CR");
    expect(screen.getByRole("region", { name: "INS filter" })).toHaveTextContent("Tracking");
    expect(screen.getByRole("region", { name: "IMU" })).toHaveTextContent("31.5 °C");
    expect(screen.getByRole("region", { name: "INS configuration" })).toHaveTextContent("divisor 100");
    for (const gone of ["Spectrum", "Ports", "Firmware", "RF health", "Antenna & hardware"]) expect(screen.queryByRole("region", { name: gone })).toBeNull();
    expect(screen.queryByRole("button", { name: /poll a message/i })).toBeNull();
  });

  it("applies the profile with force after the confirmation, and re-reads without", async () => {
    mockFetch({ "/api/receiver": () => info, "/api/receiver/profile": () => ({ ok: true, applied: true, report: insBlock.config_report }) });
    renderWith(<Receiver />);
    await userEvent.click(await screen.findByRole("button", { name: "Apply INS configuration" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(posts("/api/receiver/profile")).toHaveLength(1));
    expect(JSON.parse(posts("/api/receiver/profile")[0][1]!.body as string)).toEqual({ apply: true, force: true });
    await userEvent.click(screen.getByRole("button", { name: "Re-read configuration" }));
    await waitFor(() => expect(posts("/api/receiver/profile")).toHaveLength(2));
    expect(JSON.parse(posts("/api/receiver/profile")[1][1]!.body as string)).toEqual({ apply: false, force: false });
  });

  it("restarts the unit after a confirmation", async () => {
    mockFetch({ "/api/receiver": () => info, "/api/receiver/reset": () => ({ ok: true, kind: "hot" }) });
    renderWith(<Receiver />);
    await userEvent.click(await screen.findByRole("button", { name: "Restart unit…" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(posts("/api/receiver/reset")).toHaveLength(1));
    expect(await screen.findByRole("status", { name: "Reset progress" })).toHaveTextContent("Restart sent");
  });
});

describe("RTK page corrections notice", () => {
  const overview = (driver: object) => ({ role: "rover", driver, ntrip: null, ntrip_url: null, rtk: {}, outputs: { nmea_tcp: null, nmea_udp: [], nmea_serial: null, json_udp: null, sentences: [] }, session: null, collect: { state: "idle" } });
  const history = () => ({ res: "1s", columns: ["ts", "carr_soln", "fix_type"], rows: [] });

  it("says a VN-200 does not take RTCM", async () => {
    mockFetch({ "/api/rover": () => overview({ name: "vectornav", capabilities: capsNoRtcm, rtcm_unverified: false }), "/api/history": history });
    renderWith(<Rtk />);
    expect(await screen.findByRole("status", { name: "RTCM notice" })).toHaveTextContent("This receiver does not accept RTCM corrections (VN-200)");
  });

  it("flags an RTCM path the unit has not confirmed", async () => {
    const sbg = overview({ name: "sbg_ellipse", capabilities: { ...capsNoRtcm, accepts_rtcm: true }, rtcm_unverified: true });
    mockFetch({ "/api/rover": () => ({ ...sbg, ntrip_url: "ntrip://u:***@base:2101/MTRK" }), "/api/history": history });
    renderWith(<Rtk />);
    expect(await screen.findByRole("status", { name: "RTCM notice" })).toHaveTextContent("RTCM path unverified on this unit");
  });

  it("does not call an RTCM path unverified when no corrections are sent", async () => {
    mockFetch({ "/api/rover": () => overview({ name: "sbg_ellipse", capabilities: { ...capsNoRtcm, accepts_rtcm: true }, rtcm_unverified: true }), "/api/history": history });
    renderWith(<Rtk />);
    await screen.findByRole("region", { name: "Outputs" }); // the rover overview has arrived
    expect(screen.queryByRole("status", { name: "RTCM notice" })).toBeNull();
  });

  it("shows no notice for a u-blox rover", async () => {
    mockFetch({ "/api/rover": () => overview({ name: "ublox", capabilities: { ...capsNoRtcm, accepts_rtcm: true, spectrum: true } }), "/api/history": history });
    renderWith(<Rtk />);
    await screen.findByRole("region", { name: "Solution" });
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalled());
    expect(screen.queryByRole("status", { name: "RTCM notice" })).toBeNull();
  });
});

describe("Dashboard IMU card", () => {
  beforeEach(() => mockFetch({ "/api/rover": () => ({}) }));

  it("appears with an IMU sample", () => {
    renderWith(<Dashboard />);
    const card = screen.getByRole("region", { name: "IMU" });
    expect(card).toHaveTextContent("Tracking");
    expect(card).toHaveTextContent("31.5 °C");
    expect(card).toHaveTextContent("9.81 m/s²");
  });

  it("is absent on a u-blox receiver", () => {
    useLive.setState({ state: sampleState() });
    renderWith(<Dashboard />);
    expect(screen.queryByRole("region", { name: "IMU" })).toBeNull();
  });
});

describe("live store, INS", () => {
  it("merges the epoch's ins bundle and keeps the latest ins.config report", () => {
    const log = vi.fn();
    configureLive({ log });
    const store = useLive.getState();
    store.applyMessage({ type: "epoch", t: null, ins: { ins: { ...insStatus, mode: 1, mode_name: "Aligning" }, imu: null, attitude: null } });
    const s = useLive.getState().state!;
    expect(s.ins?.mode_name).toBe("Aligning");
    expect(s.imu).toBeNull();
    store.applyMessage({ type: "update", topic: "ins", source: "ins.config", data: insBlock.config_report });
    expect(useLive.getState().insConfig?.pending).toEqual(["binary_output_1"]);
    store.applyMessage({ type: "update", topic: "ins", source: "ins.config", data: { not: "a report" } });
    expect(useLive.getState().insConfig?.pending).toEqual(["binary_output_1"]);
    expect(log).toHaveBeenCalledWith("debug", "ws: ins.config carried an unexpected payload", { not: "a report" });
  });
});
