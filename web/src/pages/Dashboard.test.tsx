import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { resetPrefsForTests } from "@/lib/prefs";
import type { BaseModeView } from "@/lib/types";
import { sampleRover, sampleState } from "@/test/fixtures";
import { maps, resetMaplibreMock } from "@/test/maplibreMock";
import Dashboard from "./Dashboard";
import { preloadMaps } from "@/test/lazyMaps";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));
beforeAll(preloadMaps); // the lazy maps resolve from the module cache, not a cold transform

const baseModeView = (over: Partial<BaseModeView> = {}): BaseModeView => ({
  available: true, mode: "survey-in", site: null, verified: false, last_1005: null, svin: { min_duration_s: 300, acc_limit_m: 2 }, ...over,
});

/** The only network the page may touch is `GET /api/base/mode`; anything else is a 404. */
function mockFetch(view: BaseModeView | null = baseModeView()) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (url.startsWith("/api/base/mode")) return new Response(JSON.stringify(view ?? {}), { status: view ? 200 : 503, headers: { "content-type": "application/json" } });
    return new Response(JSON.stringify({ detail: "not found" }), { status: 404, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
}

function renderDashboard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Dashboard /></MemoryRouter>
    </QueryClientProvider>,
  );
}

function pushEpoch(patch: (s: ReturnType<typeof sampleState>) => void) {
  act(() => {
    const prev = useLive.getState().state ?? sampleState();
    const next = { ...prev, epoch_count: prev.epoch_count + 1 };
    patch(next);
    useLive.setState({ state: next, lastEpochAt: (useLive.getState().lastEpochAt ?? 0) + 1000, stale: false });
  });
}

describe("Dashboard", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    resetMaplibreMock();
    localStorage.clear();
    mockFetch();
    useLive.setState({ state: sampleState(), status: "open", connected: true, stale: false, lastEpochAt: Date.now(), receiverConnected: true, ntripClients: [] });
  });

  it("shows the coordinate hero, sky plot, systems, survey-in and the recent panel", async () => {
    renderDashboard();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Dashboard");
    const hero = screen.getByTestId("coordinate-readout");
    expect(hero).toHaveTextContent("23°50'14.4622\"N");
    expect(hero).toHaveTextContent("90°15'45.1807\"E");
    expect(hero).toHaveTextContent("1.2 cm");
    expect(hero).toHaveTextContent("-36.268 m");
    expect(hero).toHaveTextContent("13.363 m");
    expect(screen.getByRole("img", { name: /8 satellites/ })).toBeInTheDocument();
    expect(screen.getAllByText("GLONASS").length).toBeGreaterThan(0);
    expect(screen.getByText(/survey-in running/i)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Recent" })).toBeInTheDocument();
    expect(await screen.findByText(/collecting epochs/i)).toBeInTheDocument();
    expect(screen.queryByText(/1 epochs?/)).toBeNull(); // the count waits for a second epoch
    // the map fills its panel: flex body, flex-1 frame (3 above pins the chain a bodyClassName change would break);
    // it is lazy-loaded (F2), so it arrives a tick after the page
    const frame = await screen.findByTestId("map-frame");
    expect(frame.className).toContain("flex-1");
    expect(frame.parentElement!.className.split(/\s+/)).toContain("flex");
    expect(frame.parentElement!.className).toContain("p-0");
    for (const name of ["Sky", "Map", "Fix", "Satellites by system", "Position mode", "Corrections", "Recent"]) {
      expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
    }
  });

  // F1 — the `system` topic arrived every second and was dropped on the floor, while the alert
  // rules fire on the very same CPU, disk and temperature figures.
  it("shows the host's CPU, memory, disk, temperature and uptime from the system topic", () => {
    renderDashboard();
    let host = screen.getByRole("heading", { level: 2, name: "Host" }).closest("section")!;
    expect(host).toHaveTextContent(/waiting for the first host sample/i);
    act(() => useLive.setState({ system: { cpu_pct: 12.4, mem_pct: 40.2, disk_free_gb: 10.53, disk_used_pct: 50.4, uptime_s: 3 * 86400 + 3600, temp_c: 41.6, load1: 0.42, ts_utc: "2026-09-18T16:47:34+00:00" } }));
    host = screen.getByRole("heading", { level: 2, name: "Host" }).closest("section")!;
    expect(host).toHaveTextContent("CPU12 %");
    expect(host).toHaveTextContent("Load0.42");
    expect(host).toHaveTextContent("Memory40 %");
    expect(host).toHaveTextContent("Disk free10.5 GB50 % used");
    expect(host).toHaveTextContent("Temperature42 °C");
    expect(host).toHaveTextContent("Host uptime3d 1h");
    act(() => useLive.setState({ system: { ...useLive.getState().system!, temp_c: null, load1: null } }));
    expect(screen.getByRole("heading", { level: 2, name: "Host" }).closest("section")!).toHaveTextContent("Temperature—");
  });

  // The host figures come from the daemon, not the receiver: a quiet receiver must not grey them,
  // a dropped socket must.
  it("greys the Host figures when the socket is down, not when only the receiver is quiet", () => {
    useLive.setState({ stale: true, system: { cpu_pct: 12.4, mem_pct: 40.2, disk_free_gb: 10.53, disk_used_pct: 50.4, uptime_s: 3600, temp_c: 41.6, load1: 0.42, ts_utc: "2026-09-18T16:47:34+00:00" } });
    renderDashboard();
    expect(screen.getByTestId("dashboard-grid")).toHaveAttribute("data-stale", "true");
    const scopeOf = () => screen.getByRole("heading", { level: 2, name: "Host" }).closest("section")!.parentElement!.closest("[data-stale]")!;
    expect(scopeOf()).not.toBe(screen.getByTestId("dashboard-grid"));
    expect(scopeOf()).toHaveAttribute("data-stale", "false");
    act(() => useLive.setState({ status: "reconnecting", connected: false }));
    expect(scopeOf()).toHaveAttribute("data-stale", "true");
    expect(scopeOf().className).toContain("text-ink-3");
  });

  // D5 — the Recent sparklines take the epochs' own times: a gap in the epochs breaks the lines.
  it("breaks the Recent sparklines where the epochs stopped", () => {
    renderDashboard();
    pushEpoch(() => {});
    pushEpoch(() => {});
    act(() => useLive.setState({ lastEpochAt: useLive.getState().lastEpochAt! + 60_000 })); // a minute of silence
    pushEpoch(() => {});
    pushEpoch(() => {});
    for (const name of [/horizontal accuracy/i, /satellites used/i, /mean c\/n0/i]) expect(screen.getByRole("img", { name }).querySelectorAll("polyline"), String(name)).toHaveLength(2);
  });

  it("switches coordinate format and remembers it", async () => {
    renderDashboard();
    const hero = screen.getByTestId("coordinate-readout");
    await userEvent.selectOptions(within(hero).getByLabelText(/coordinate format/i), "dd");
    expect(hero).toHaveTextContent("23.8373506°");
    expect(hero).toHaveTextContent("90.2625502°");
    expect(localStorage.getItem("mtrtk:coordMode")).toBe('"dd"');
    await userEvent.selectOptions(within(hero).getByLabelText(/coordinate format/i), "ecef");
    expect(hero).toHaveTextContent(/X -26748\.1720\s+Y 5837156\.6180/); // the receiver's own ECEF, not a derived one
    expect(hero).toHaveTextContent("Z 2561801.2610");
  });

  it("copies the two coordinate lines joined by a newline and says so briefly", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderDashboard();
    const hero = screen.getByTestId("coordinate-readout");
    fireEvent.click(within(hero).getByRole("button", { name: /copy/i }));
    expect(writeText).toHaveBeenCalledWith("23°50'14.4622\"N\n90°15'45.1807\"E");
    expect(await within(hero).findByText("Copied")).toBeInTheDocument();
  });

  it("shows the fix badge, fix stats and correction stats", async () => {
    useLive.setState({ ntripClients: [sampleRover(), sampleRover({ id: 2, ip: "100.64.0.9", last_gga_lat: null, last_gga_lon: null })] });
    renderDashboard();
    const fix = screen.getByRole("heading", { level: 2, name: "Fix" }).closest("section")!;
    expect(within(fix).getByText("3D fix")).toBeInTheDocument();
    expect(fix).toHaveTextContent("Satellites used6/8");
    expect(fix).toHaveTextContent("PDOP1.2");
    expect(fix).toHaveTextContent("Uptime10m 00s");
    const corr = screen.getByRole("heading", { level: 2, name: "Corrections" }).closest("section")!;
    expect(corr).toHaveTextContent("RTCM out1.9 kB/s");
    expect(corr).toHaveTextContent("Message types3");
    expect(corr).toHaveTextContent("Rovers connected2");
    expect(corr).toHaveTextContent("Sent43.4 kB");
    // every figure is tabular
    for (const el of [...fix.querySelectorAll("[data-stat-value]"), ...corr.querySelectorAll("[data-stat-value]")]) expect(el.className).toContain("num");
    // the rover with a GGA position is on the map, the one without is not
    await screen.findByTestId("map-frame");
    expect(document.querySelectorAll('[data-marker="rover"]')).toHaveLength(1);
    expect(document.querySelector('[data-marker="rover"]')!.getAttribute("title")).toContain("u-center");
  });

  it("position mode card: survey-in running, complete, fixed site, off", () => {
    const { unmount } = renderDashboard();
    let card = screen.getByRole("heading", { level: 2, name: "Position mode" }).closest("section")!;
    expect(card).toHaveTextContent("Survey-in running");
    expect(card).toHaveTextContent("Elapsed2m 00s");
    expect(card).toHaveTextContent("Observations118");
    expect(card).toHaveTextContent("Mean 3D accuracy1.90 m");
    unmount();

    useLive.setState({ state: { ...sampleState(), survey_in: { active: false, valid: true, dur_s: 900, obs: 890, mean_x_m: 1, mean_y_m: 2, mean_z_m: 3, mean_acc_m: 0.8 } } });
    const r2 = renderDashboard();
    card = screen.getByRole("heading", { level: 2, name: "Position mode" }).closest("section")!;
    expect(card).toHaveTextContent("Survey-in complete");
    expect(card).toHaveTextContent("Mean 3D accuracy80.0 cm");
    expect(card).toHaveTextContent("Duration15m 00s");
    r2.unmount();

    useLive.setState({
      state: { ...sampleState(), survey_in: { active: false, valid: false, dur_s: 0, obs: 0, mean_x_m: null, mean_y_m: null, mean_z_m: null, mean_acc_m: null } },
      base: { mode: "fixed", site: "ROOF", reason: null, verified: true, mismatch: null },
    });
    const r3 = renderDashboard();
    card = screen.getByRole("heading", { level: 2, name: "Position mode" }).closest("section")!;
    expect(card).toHaveTextContent("Fixed site ROOF");
    // C2 — the same predicate and the same words as the Corrections and Site pages (siteCheck)
    expect(card).toHaveTextContent("RTCM 1005 checkSite verified · ROOF");
    r3.unmount();

    useLive.setState({ base: { mode: "fixed", site: "ROOF", reason: null, verified: false, mismatch: { site: "ROOF", dx: 0.4, dy: 0, dz: 0 } } });
    const r4 = renderDashboard();
    card = screen.getByRole("heading", { level: 2, name: "Position mode" }).closest("section")!;
    expect(card).toHaveTextContent("RTCM 1005 checkSite mismatch · ROOF");
    expect(card).toHaveTextContent("Offset0.400, 0.000, 0.000 m");
    r4.unmount();

    useLive.setState({ base: { mode: "off", site: null, reason: null, verified: null, mismatch: null } });
    renderDashboard();
    card = screen.getByRole("heading", { level: 2, name: "Position mode" }).closest("section")!;
    expect(card).toHaveTextContent(/position mode is off/i);
  });

  it("position mode card falls back to GET /api/base/mode until the socket reports a mode", async () => {
    mockFetch(baseModeView({ mode: "fixed", site: "PILLAR", verified: false }));
    useLive.setState({ state: { ...sampleState(), survey_in: { active: false, valid: false, dur_s: 0, obs: 0, mean_x_m: null, mean_y_m: null, mean_z_m: null, mean_acc_m: null } } });
    renderDashboard();
    const card = screen.getByRole("heading", { level: 2, name: "Position mode" }).closest("section")!;
    expect(await within(card).findByText("Fixed site PILLAR")).toBeInTheDocument();
    expect(card).toHaveTextContent("RTCM 1005 checkNot yet verified · PILLAR");
    expect(vi.mocked(globalThis.fetch).mock.calls.every(([u]) => String(u instanceof Request ? u.url : u).startsWith("/api/base/mode"))).toBe(true);
  });

  it("falls back to a grid with the markers still drawn when tiles fail, and recovers when one loads", async () => {
    renderDashboard();
    const frame = await screen.findByTestId("map-frame");
    expect(maps).toHaveLength(1);
    expect(frame).toHaveAttribute("data-offline", "false");
    expect(document.querySelector('[data-marker="base"]')).not.toBeNull();

    act(() => maps[0].fire("error", { error: { status: 0, url: "https://tile.openstreetmap.org/17/98467/57412.png", message: "Failed to fetch" } }));
    expect(frame).toHaveAttribute("data-offline", "true");
    expect(frame.className).toContain("map-grid");
    expect(screen.getByText(/map tiles unavailable/i)).toBeInTheDocument();
    expect(document.querySelector('[data-marker="base"]')).not.toBeNull();
    expect(maps[0].removed).toBe(false);

    // the GeoJSON accuracy source loading is not "tiles are back"
    act(() => maps[0].fire("sourcedata", { dataType: "source", sourceId: "acc", tile: {} }));
    expect(frame).toHaveAttribute("data-offline", "true");
    act(() => maps[0].fire("sourcedata", { dataType: "source", sourceId: "basemap", tile: {} }));
    expect(frame).toHaveAttribute("data-offline", "false");
    expect(screen.queryByText(/map tiles unavailable/i)).toBeNull();
    expect(document.querySelector('[data-marker="base"]')).not.toBeNull();
  });

  it("greys every figure when the data is stale", () => {
    useLive.setState({ stale: true });
    renderDashboard();
    const grid = screen.getByTestId("dashboard-grid");
    expect(grid).toHaveAttribute("data-stale", "true");
    expect(grid.className).toContain("text-ink-3");
    expect(screen.getByText("Waiting for data")).toBeInTheDocument();
  });

  it("draws the sparklines from the last epochs once two are in", async () => {
    renderDashboard();
    expect(screen.getByText(/collecting epochs/i)).toBeInTheDocument();
    pushEpoch((s) => { s.accuracy = { ...s.accuracy, h_acc_m: 0.015 }; });
    pushEpoch((s) => { s.accuracy = { ...s.accuracy, h_acc_m: 0.011 }; });
    const spark = await screen.findByRole("img", { name: /horizontal accuracy/i });
    expect(spark.querySelector("title")).toHaveTextContent("min 1.1 cm, max 1.5 cm, last 1.1 cm");
    expect(screen.getByRole("img", { name: /satellites used/i })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /mean c\/n0/i })).toBeInTheDocument();
    expect(screen.getByText(/3 epochs · 2s/)).toBeInTheDocument();
    // the hero followed the epoch too
    expect(screen.getByTestId("coordinate-readout")).toHaveTextContent("1.1 cm");
  });

  it("shows an empty state before the first state arrives", () => {
    useLive.setState({ state: null });
    renderDashboard();
    expect(screen.getByText(/waiting for the receiver/i)).toBeInTheDocument();
    expect(screen.queryByTestId("coordinate-readout")).toBeNull();
    expect(maps).toHaveLength(0);
  });

  it("on a rover swaps position mode and corrections for the RTK solution and the NTRIP client", async () => {
    const s = sampleState();
    s.rtk = { ...s.rtk, carr_soln: 1, carr_soln_name: "RTK float", corr_age_s: 3.4, baseline_m: 812.345, heading_deg: 45.5, heading_valid: true, ref_station_id: 7 };
    useLive.setState({
      state: s,
      role: "rover",
      ntripClient: { connected: true, host: "100.100.50.10", port: 2101, mountpoint: "MTRK", version: 2, bytes_received: 2048, frames_injected: 12, crc_dropped: 0, last_rtcm_mono: 1, last_error: "timed out", reconnects: 1, next_retry_s: null, since_mono: 1, last_rtcm_age_s: 0.5, connected_for_s: 9 },
    });
    renderDashboard();
    expect(screen.queryByRole("region", { name: "Position mode" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Corrections" })).toBeNull();
    const rtk = screen.getByRole("region", { name: "RTK" });
    expect(within(rtk).getByText("RTK float")).toBeInTheDocument();
    expect(within(rtk).getByRole("meter", { name: /correction age/i })).toHaveAttribute("aria-valuenow", "3.4");
    expect(rtk).toHaveTextContent("812.35 m");
    expect(rtk).toHaveTextContent("7");
    // relPosHeading is the base→rover vector's heading (45.5°): the base lies at 225.5° from here
    expect(within(rtk).getByText("Bearing to base").parentElement).toHaveTextContent("225.5°");
    expect(rtk).not.toHaveTextContent(/heading to base/i);
    const ntrip = screen.getByRole("region", { name: "NTRIP client" });
    expect(ntrip).toHaveTextContent("100.100.50.10:2101/MTRK");
    expect(ntrip).toHaveTextContent("Connected");
    expect(ntrip).toHaveTextContent("2.0 kB");
    expect(ntrip).toHaveTextContent("12");
    expect(ntrip).toHaveTextContent("timed out");
    expect(globalThis.fetch).not.toHaveBeenCalledWith(expect.stringMatching(/^\/api\/base\/mode/), expect.anything());
  });

  it("on a rover with no caster says how to set one", () => {
    useLive.setState({ role: "rover", ntripClient: null });
    renderDashboard();
    expect(screen.getByRole("region", { name: "NTRIP client" })).toHaveTextContent(/no caster/i);
  });

  it("on a rover with no corrections says so to a screen reader too", () => {
    const s = sampleState();
    s.rtk = { ...s.rtk, corr_age_s: null };
    useLive.setState({ state: s, role: "rover", ntripClient: null });
    renderDashboard();
    const meter = within(screen.getByRole("region", { name: "RTK" })).getByRole("meter", { name: /correction age/i });
    expect(meter).toHaveAttribute("aria-valuetext", "no corrections");
    expect(meter).toHaveTextContent("no corrections");
  });
});
