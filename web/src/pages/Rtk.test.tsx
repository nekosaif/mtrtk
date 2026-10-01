import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import Rtk from "./Rtk";

const overview = { role: "rover", driver: { name: "ublox", capabilities: { accepts_rtcm: true, raw_gnss_log: true, attitude: false, imu: false, sats: true, spectrum: true } }, ntrip: { connected: true, host: "100.100.50.10", port: 2101, mountpoint: "MTRK", version: 2, bytes_received: 123456, frames_injected: 400, crc_dropped: 0, last_rtcm_mono: 1, last_error: null, reconnects: 0, next_retry_s: null, since_mono: 1, last_rtcm_age_s: 0.4, connected_for_s: 65 }, ntrip_url: "ntrip://rover:***@100.100.50.10:2101/MTRK", rtk: {}, outputs: { nmea_tcp: { port: 10110, clients: 0 }, nmea_udp: [], nmea_serial: null, json_udp: null, sentences: ["GGA"] }, session: null, collect: { state: "idle" } };
let calls: [string, RequestInit | undefined][] = [];

function renderRtk() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><Rtk /></MemoryRouter></QueryClientProvider>);
}

describe("RTK page", () => {
  beforeEach(() => {
    resetLiveForTests();
    calls = [];
    const state = sampleState();
    state.rtk = { ...state.rtk, carr_soln: 2, carr_soln_name: "RTK fixed", baseline_m: 1234.56, heading_deg: 91.2, heading_valid: true, corr_age_s: 1.2, ref_station_id: 7, rtcm_rx: { "1077": { count: 120, used: 118, crc_failed: 0, last_seen_mono: 1 }, "1005": { count: 120, used: 120, crc_failed: 1, last_seen_mono: 1 } }, rtcm_rx_total: 240, rtcm_crc_failed: 1 } as typeof state.rtk;
    useLive.setState({ state, role: "rover", status: "open", lastEpochAt: Date.now(), ntripClient: overview.ntrip as never });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      if (String(url).endsWith("/api/rover")) return new Response(JSON.stringify(overview), { status: 200 });
      if (String(url).includes("/api/history")) return new Response(JSON.stringify({ res: "1s", columns: ["ts", "carr_soln", "fix_type"], rows: [[1, 2, 3], [2, 1, 3], [3, 0, 3]] }), { status: 200 });
      if (init?.method === "PUT") return new Response(JSON.stringify({ ok: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("shows RTK solution, correction age, RTCM table and NTRIP client", async () => {
    renderRtk();
    expect(await screen.findByText(/RTK fixed/)).toBeInTheDocument();
    expect(screen.getByText(/1234\.56 m/)).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: /correction age/i })).toHaveAttribute("aria-valuenow", "1.2");
    expect(screen.getByText("1077")).toBeInTheDocument();
    expect(await screen.findByText(/100\.100\.50\.10:2101\/MTRK/)).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: /fix state/i })).toBeInTheDocument();
  });

  it("asks the history for the last ten minutes of fix state at 1 s", async () => {
    renderRtk();
    await screen.findByRole("img", { name: /fix state/i });
    const hist = calls.map(([u]) => u).find((u) => u.includes("/api/history"))!;
    const q = new URL(hist, "http://x").searchParams;
    expect(q.get("metrics")).toBe("carr_soln,fix_type");
    expect(q.get("res")).toBe("1s");
    expect(Date.parse(q.get("to")!) - Date.parse(q.get("from")!)).toBe(600_000);
    expect(screen.getByRole("img", { name: /fix state/i })).toHaveAccessibleName(/33% RTK fixed/);
  });

  it("edits the NTRIP url", async () => {
    renderRtk();
    await userEvent.click(await screen.findByRole("button", { name: /change caster/i }));
    const input = screen.getByLabelText(/ntrip url/i);
    await userEvent.clear(input);
    await userEvent.type(input, "ntrip://rover:pw@base:2101/MTRK");
    await userEvent.click(screen.getByRole("button", { name: /^connect$/i }));
    const put = calls.find(([, i]) => i?.method === "PUT")!;
    expect(put[0]).toBe("/api/rover/ntrip");
    expect(JSON.parse(put[1]!.body as string)).toEqual({ url: "ntrip://rover:pw@base:2101/MTRK" });
  });

  it("prefills the caster form with the configured URL, password masked", async () => {
    renderRtk();
    await screen.findByText(/100\.100\.50\.10:2101\/MTRK/);
    await screen.findByRole("img", { name: /fix state/i }); // the overview has landed too
    await userEvent.click(screen.getByRole("button", { name: /change caster/i }));
    expect(screen.getByLabelText(/ntrip url/i)).toHaveValue("ntrip://rover:***@100.100.50.10:2101/MTRK");
  });

  it("shows the caster's error and a stale correction age", async () => {
    const state = useLive.getState().state!;
    useLive.setState({
      state: { ...state, rtk: { ...state.rtk, carr_soln: 0, carr_soln_name: "None", corr_age_s: 12.5 } },
      ntripClient: { ...overview.ntrip, connected: false, last_error: "401 Unauthorized", next_retry_s: 8 } as never,
    });
    renderRtk();
    expect(await screen.findByText(/401 Unauthorized/)).toBeInTheDocument();
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    const meter = screen.getByRole("meter", { name: /correction age/i });
    expect(meter).toHaveAttribute("aria-valuenow", "12.5");
    expect(meter.querySelector("[data-gauge-fill]")).toHaveStyle({ background: "var(--status-critical)" });
  });

  it("lists camera time marks newest first", async () => {
    useLive.setState({
      timeMarks: [
        { channel: 0, count: 2, rising_week: 2436, rising_tow_s: 492473.25, falling_week: null, falling_tow_s: null, new_rising: true, new_falling: false, time_base: 2, utc_based: true, acc_est_ns: 20, rising_utc: "2026-09-18T16:47:35.250000+00:00" },
        { channel: 0, count: 1, rising_week: 2436, rising_tow_s: 492472, falling_week: null, falling_tow_s: null, new_rising: true, new_falling: false, time_base: 2, utc_based: true, acc_est_ns: 20, rising_utc: "2026-09-18T16:47:34+00:00" },
      ],
    });
    renderRtk();
    const table = await screen.findByRole("table", { name: /time marks/i });
    const rows = table.querySelectorAll("tbody tr");
    expect(rows[0]).toHaveTextContent("16:47:35.250000");
    expect(rows[1]).toHaveTextContent("16:47:34.000000");
  });
});
