import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { resetPrefsForTests } from "@/lib/prefs";
import type { BaseModeView, NtripHistoryRecord, NtripInfo } from "@/lib/types";
import { sampleRover, sampleState } from "@/test/fixtures";
import Corrections from "./Corrections";

const ntripInfo = (over: Partial<NtripInfo> = {}): NtripInfo => ({
  running: true,
  host: "100.100.50.10",
  port: 2101,
  bind_mode: "tailscale",
  mountpoint: "MTRK",
  anonymous: false,
  username: "rover",
  connection_url: "ntrip://rover:***@100.100.50.10:2101/MTRK",
  clients: 1,
  max_clients: 4,
  rejected: 2,
  sourcetable: "STR;MTRK;MTRK;RTCM 3.3;1005(1),1077(1);2;GPS+GLO+GAL+BDS;mtrtk;BD;23.84;90.26;0;0;mtrtk;none;B;N;1900;\r\nENDSOURCETABLE\r\n",
  ...over,
});

const historyRow = (over: Partial<NtripHistoryRecord> = {}): NtripHistoryRecord => ({
  id: 9,
  ip: "100.100.50.13",
  mountpoint: "MTRK",
  user_agent: "str2str",
  username: "rover",
  connected_utc: "2026-09-18T10:00:00+00:00",
  disconnected_utc: "2026-09-18T10:30:00+00:00",
  bytes_sent: 555,
  last_lat: null,
  last_lon: null,
  reason: "client closed",
  ...over,
});

const baseModeView = (over: Partial<BaseModeView> = {}): BaseModeView => ({
  available: true, mode: "survey-in", site: null, verified: false, last_1005: null, svin: { min_duration_s: 300, acc_limit_m: 2 }, ...over,
});

interface Answers {
  ntrip?: NtripInfo;
  history?: NtripHistoryRecord[];
  clients?: unknown[];
  baseMode?: BaseModeView;
}

/** The routes the page may read; anything else is a 404 and asserted against. */
function mockFetch(a: Answers = {}) {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    if (init?.method && init.method !== "GET") return json({ detail: "not found" }, 404);
    if (url.endsWith("/api/ntrip")) return json(a.ntrip ?? ntripInfo());
    if (url.includes("/api/ntrip/history")) return json(a.history ?? [historyRow()]);
    if (url.endsWith("/api/ntrip/clients")) return json(a.clients ?? []);
    if (url.startsWith("/api/base/mode")) return json(a.baseMode ?? baseModeView());
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
}

const calls = () => (globalThis.fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit | undefined][] } }).mock.calls.map(([u]) => String(u));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Corrections /></MemoryRouter>
    </QueryClientProvider>,
  );
}

const region = (name: string | RegExp) => screen.getByRole("region", { name });
const statValue = (scope: HTMLElement, label: string) => within(scope).getByText(label).parentElement!.querySelector("[data-stat-value]")!;
const rowOf = (scope: HTMLElement, text: string | RegExp) => within(scope).getByText(text).closest("tr")!;

describe("Corrections page", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    localStorage.clear();
    useLive.setState({ state: sampleState(), status: "open", connected: true, stale: false, lastEpochAt: Date.now(), receiverConnected: true, ntripClients: [] });
    mockFetch();
  });

  // ---- the brief's acceptance test ---------------------------------------------------------

  it("shows RTCM types, the connection string and live clients", async () => {
    useLive.setState({ state: sampleState(), status: "open", lastEpochAt: Date.now(), ntripClients: [{ id: 1, ip: "100.100.50.12", port: 5000, mountpoint: "MTRK", user_agent: "NTRIP SWMaps", username: "rover", version: 2, connected_utc: new Date().toISOString(), bytes_sent: 12345, dropped_frames: 0, last_gga_lat: 23.8, last_gga_lon: 90.2, last_gga_utc: null }] });
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      const p = String(url);
      if (p.endsWith("/api/ntrip")) return new Response(JSON.stringify({ running: true, host: "100.100.50.10", port: 2101, mountpoint: "MTRK", anonymous: false, username: "rover", bind_mode: "tailscale", connection_url: "ntrip://rover:***@100.100.50.10:2101/MTRK", sourcetable: "STR;MTRK;...\r\nENDSOURCETABLE\r\n" }), { status: 200 });
      if (p.includes("/api/ntrip/history")) return new Response(JSON.stringify([{ id: 9, ip: "100.100.50.13", mountpoint: "MTRK", user_agent: "str2str", username: "rover", connected_utc: "2026-09-18T10:00:00+00:00", disconnected_utc: "2026-09-18T10:30:00+00:00", bytes_sent: 555, last_lat: null, last_lon: null, reason: "client closed" }]), { status: 200 });
      return new Response("[]", { status: 200 });
    }) as typeof fetch;
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Corrections /></MemoryRouter></QueryClientProvider>);
    expect(screen.getByText("1077")).toBeInTheDocument();
    expect(screen.getByText(/GPS MSM7/)).toBeInTheDocument();
    expect(await screen.findByText("ntrip://rover:***@100.100.50.10:2101/MTRK")).toBeInTheDocument();
    expect(screen.getByText("NTRIP SWMaps")).toBeInTheDocument();
    expect(await screen.findByText("str2str")).toBeInTheDocument();
    expect(screen.getByText(/1 rover connected/i)).toBeInTheDocument();
  });

  // ---- RTCM table (rulings 1, 2) ------------------------------------------------------------

  it("lists every message type with its description, count and last-seen age relative to the newest", async () => {
    const state = sampleState();
    state.rtcm_out.messages = {
      "1005": { count: 120, bytes: 3000, last_seen_mono: 4100.0 },
      "1077": { count: 120, bytes: 40000, last_seen_mono: 4100.0 },
      "1230": { count: 24, bytes: 400, last_seen_mono: 4099.8 },
      "1087": { count: 120, bytes: 30000, last_seen_mono: 4034.5 },
      "4072": { count: 3, bytes: 90, last_seen_mono: null },
    };
    useLive.setState({ state });
    renderPage();
    const table = region("RTCM 3 output");
    const rows = within(table).getAllByRole("row");
    // header + 5 types, sorted by number
    expect(rows).toHaveLength(6);
    expect(rows.slice(1).map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual(["1005", "1077", "1087", "1230", "4072"]);
    expect(rowOf(table, "1005")).toHaveTextContent("Station ARP (base position)");
    expect(rowOf(table, "1077")).toHaveTextContent("GPS MSM7");
    expect(rowOf(table, "1087")).toHaveTextContent("GLONASS MSM7");
    expect(rowOf(table, "1230")).toHaveTextContent("GLONASS code-phase biases");
    expect(rowOf(table, "4072")).toHaveTextContent("u-blox proprietary");
    expect(rowOf(table, "1077")).toHaveTextContent("120");
    // ages: relative to the newest last_seen_mono (4100.0), never a wall-clock time
    expect(rowOf(table, "1077")).toHaveTextContent("latest");
    expect(rowOf(table, "1230")).toHaveTextContent("0.2 s ago");
    expect(rowOf(table, "1087")).toHaveTextContent("1 min ago");
    expect(rowOf(table, "4072")).toHaveTextContent("—");
    expect(within(table).queryByText(/UTC/)).toBeNull();
    // rates need a second sample
    expect(within(rowOf(table, "1077")).getAllByRole("cell")[3]).toHaveTextContent("—");
    await screen.findByText(ntripInfo().connection_url);
  });

  it("shows Hz and bytes/s from the count deltas between epochs", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      const t0 = new Date("2026-09-18T16:47:00Z").getTime();
      vi.setSystemTime(t0);
      renderPage();
      const table = region("RTCM 3 output");
      vi.setSystemTime(t0 + 5000);
      act(() => {
        const prev = useLive.getState().state!;
        const messages = { ...prev.rtcm_out.messages, "1077": { count: 125, bytes: 42_000, last_seen_mono: 6 }, "1005": { count: 125, bytes: 3125, last_seen_mono: 6 } };
        useLive.setState({ state: { ...prev, epoch_count: prev.epoch_count + 1, rtcm_out: { ...prev.rtcm_out, messages } }, lastEpochAt: t0 + 5000 });
      });
      const cells = within(rowOf(table, "1077")).getAllByRole("cell");
      expect(cells[3]).toHaveTextContent("1.00 Hz");
      expect(cells[4]).toHaveTextContent("400 B/s");
      expect(within(rowOf(table, "1230")).getAllByRole("cell")[3]).toHaveTextContent("0.00 Hz");
      await screen.findByText(ntripInfo().connection_url);
    } finally {
      vi.useRealTimers();
    }
  });

  it("puts the site check on the 1005 row: verified, mismatch, not yet verified, and nothing on a survey-in base", async () => {
    // a fixed base, the socket has not yet compared a 1005: waiting
    mockFetch({ baseMode: baseModeView({ mode: "fixed", site: "PILLAR", verified: false }) });
    renderPage();
    const table = region("RTCM 3 output");
    await screen.findByText(ntripInfo().connection_url);
    await within(table).findByText(/not yet verified/i);
    let badge = within(rowOf(table, "1005")).getByText(/not yet verified/i);
    expect(badge).toHaveAttribute("data-level", "warning");
    // the socket confirms the match
    act(() => useLive.setState({ base: { mode: "fixed", site: "PILLAR", reason: null, verified: true, mismatch: null } }));
    badge = within(rowOf(table, "1005")).getByText(/site verified/i);
    expect(badge).toHaveAttribute("data-level", "good");
    expect(badge).toHaveTextContent("PILLAR");
    // then a mismatch
    act(() => useLive.setState({ base: { mode: "fixed", site: "PILLAR", reason: null, verified: false, mismatch: { site: "PILLAR", dx: 0.5, dy: -0.2, dz: 0.1 } } }));
    badge = within(rowOf(table, "1005")).getByText(/site mismatch/i);
    expect(badge).toHaveAttribute("data-level", "critical");
    expect(rowOf(table, "1005")).toHaveTextContent("0.500, -0.200, 0.100 m");
    // a survey-in base has nothing to check against
    act(() => useLive.setState({ base: { mode: "survey-in", site: null, reason: null, verified: null, mismatch: null } }));
    expect(within(rowOf(table, "1005")).queryByText(/verified|mismatch/i)).toBeNull();
  });

  it("explains an empty stream and greys the figures when the data is stale", () => {
    const state = sampleState();
    state.rtcm_out = { messages: {}, total_count: 0, total_bytes: 0, bytes_per_s: 0 };
    useLive.setState({ state, stale: true });
    renderPage();
    expect(screen.getByText("No RTCM output")).toBeInTheDocument();
    expect(within(region("RTCM 3 output")).getByText(/No RTCM messages yet/)).toBeInTheDocument();
    const grid = screen.getByTestId("corrections-grid");
    expect(grid).toHaveAttribute("data-stale", "true");
    expect(grid.className).toContain("[&_.num]:text-ink-3");
  });

  // ---- stream panel -----------------------------------------------------------------------

  it("shows the bitrate, the totals and a labelled 5-minute sparkline", () => {
    renderPage();
    const stream = region("Stream");
    expect(statValue(stream, "Bitrate")).toHaveTextContent("1.9 kB/s");
    expect(statValue(stream, "Total output")).toHaveTextContent("43.4 kB");
    expect(statValue(stream, "Messages")).toHaveTextContent("264");
    expect(within(stream).getByText(/Bitrate, last 5 min/)).toBeInTheDocument();
    expect(within(stream).getByText("Collecting…")).toBeInTheDocument();
  });

  // ---- NTRIP panel (ruling 3) -------------------------------------------------------------

  it("shows the caster: connection URL with a copy button, bind, mountpoint, auth, clients and rejected", async () => {
    renderPage();
    const panel = region("NTRIP caster");
    expect(await within(panel).findByText("ntrip://rover:***@100.100.50.10:2101/MTRK")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /copy/i })).toBeInTheDocument();
    expect(within(panel).getByText(/password hidden/i)).toBeInTheDocument();
    expect(statValue(panel, "Listening on")).toHaveTextContent("100.100.50.10:2101");
    expect(statValue(panel, "Bind mode")).toHaveTextContent("tailscale");
    expect(statValue(panel, "Mountpoint")).toHaveTextContent("/MTRK");
    expect(statValue(panel, "Authentication")).toHaveTextContent("user rover");
    expect(statValue(panel, "Clients")).toHaveTextContent("1 / 4");
    expect(statValue(panel, "Rejected")).toHaveTextContent("2");
    expect(statValue(panel, "Rejected")).toHaveAttribute("title", expect.stringMatching(/max_clients|full/i));
    // the sourcetable is behind a disclosure, verbatim
    const details = panel.querySelector("details")!;
    expect(details).not.toBeNull();
    expect(within(details).getByText("Sourcetable")).toBeInTheDocument();
    expect(details.querySelector("pre")).toHaveTextContent(/STR;MTRK;/);
    expect(details.querySelector("pre")).toHaveTextContent(/ENDSOURCETABLE/);
  });

  it("says anonymous when no user is configured", async () => {
    mockFetch({ ntrip: ntripInfo({ anonymous: true, username: null, connection_url: "ntrip://100.100.50.10:2101/MTRK", rejected: 0 }) });
    renderPage();
    const panel = region("NTRIP caster");
    await within(panel).findByText("ntrip://100.100.50.10:2101/MTRK");
    expect(statValue(panel, "Authentication")).toHaveTextContent("anonymous");
    expect(within(panel).queryByText(/password hidden/i)).toBeNull();
  });

  it("shows an empty state when the caster is not running", async () => {
    mockFetch({ ntrip: ntripInfo({ running: false, clients: null, rejected: null, sourcetable: null }) });
    renderPage();
    const panel = region("NTRIP caster");
    expect(await within(panel).findByText(/caster is off/i)).toBeInTheDocument();
    expect(within(panel).getByText(/replay|rover/i)).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: /copy/i })).toBeNull();
    expect(within(panel).queryByText("Bind mode")).toBeNull();
  });

  // ---- clients table (ruling 4) -----------------------------------------------------------

  it("lists the store's rovers with address, agent, version, user, connected-for, sent, dropped and last GGA in the persisted mode", async () => {
    const connected = new Date(Date.now() - 125_000).toISOString();
    const gga = new Date(Date.now() - 3000).toISOString();
    useLive.setState({ ntripClients: [sampleRover({ connected_utc: connected, last_gga_utc: gga, dropped_frames: 2, bytes_sent: 120_000 })] });
    renderPage();
    const panel = region(/Connected rovers/);
    expect(screen.getByRole("heading", { level: 2, name: "Connected rovers (1)" })).toBeInTheDocument();
    const row = rowOf(panel, "100.64.0.7:51234");
    expect(row).toHaveTextContent("NTRIP u-center/23.08");
    expect(row).toHaveTextContent("v2");
    expect(row).toHaveTextContent("anonymous");
    expect(row).toHaveTextContent("2m 05s");
    expect(row).toHaveTextContent("120.0 kB");
    expect(within(row).getAllByRole("cell")[6]).toHaveTextContent("2");
    // DMS is the default coordinate mode; the age of the GGA sits beside it
    expect(row).toHaveTextContent("23°50'15.4320\"N");
    expect(row).toHaveTextContent("90°15'45.1800\"E");
    expect(row).toHaveTextContent("3 s ago");
    expect(screen.getByText(/1 rover connected/i)).toBeInTheDocument();
    await screen.findByText(ntripInfo().connection_url);
  });

  it("falls back to GET /api/ntrip/clients before the socket has listed any rover, and follows the store afterwards", async () => {
    mockFetch({ clients: [sampleRover({ id: 7, ip: "100.64.0.9", port: 4000 })] });
    renderPage();
    const panel = region(/Connected rovers/);
    expect(await within(panel).findByText("100.64.0.9:4000")).toBeInTheDocument();
    act(() => useLive.setState({ ntripClients: [sampleRover({ id: 8, ip: "100.64.0.10", port: 4001, last_gga_lat: null, last_gga_lon: null, last_gga_utc: null })] }));
    expect(within(panel).getByText("100.64.0.10:4001")).toBeInTheDocument();
    expect(within(panel).queryByText("100.64.0.9:4000")).toBeNull();
    expect(within(rowOf(panel, "100.64.0.10:4001")).getAllByRole("cell")[7]).toHaveTextContent("—");
    expect(calls().some((u) => u.endsWith("/api/ntrip/clients"))).toBe(true);
  });

  it("says so when no rover is connected", async () => {
    renderPage();
    const panel = region(/Connected rovers/);
    await screen.findByText(ntripInfo().connection_url);
    expect(within(panel).getByText(/No rovers connected/)).toBeInTheDocument();
    expect(screen.getByText(/0 rovers connected/i)).toBeInTheDocument();
  });

  // ---- history table (ruling 5) -----------------------------------------------------------

  it("lists recent connections with UTC times, a local tooltip, duration, sent and reason", async () => {
    mockFetch({
      history: [
        historyRow(),
        historyRow({ id: 10, ip: "100.100.50.14", user_agent: "NTRIP SWMaps", username: null, connected_utc: "2026-09-18T11:00:00+00:00", disconnected_utc: null, bytes_sent: 2_500_000, reason: null }),
      ],
    });
    renderPage();
    const panel = region("Recent connections");
    const row = (await within(panel).findByText("str2str")).closest("tr")!;
    expect(row).toHaveTextContent("100.100.50.13");
    expect(row).toHaveTextContent("2026-09-18 10:00:00 UTC");
    expect(row).toHaveTextContent("2026-09-18 10:30:00 UTC");
    expect(within(row).getByText("2026-09-18 10:00:00 UTC")).toHaveAttribute("title", expect.stringMatching(/UTC[+-]\d{2}:\d{2}/));
    expect(row).toHaveTextContent("30m 00s");
    expect(row).toHaveTextContent("555 B");
    expect(row).toHaveTextContent("client closed");
    const open = rowOf(panel, "100.100.50.14");
    expect(open).toHaveTextContent("still connected");
    expect(open).toHaveTextContent("2.5 MB");
    expect(calls().some((u) => u.includes("/api/ntrip/history?limit=100"))).toBe(true);
  });

  it("says so when there is no history", async () => {
    mockFetch({ history: [] });
    renderPage();
    const panel = region("Recent connections");
    expect(await within(panel).findByText(/No connections recorded yet/)).toBeInTheDocument();
  });

  // ---- housekeeping -----------------------------------------------------------------------

  it("waits for the receiver without a state", () => {
    useLive.setState({ state: null });
    renderPage();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Corrections");
    expect(screen.getByText(/Waiting for the receiver/)).toBeInTheDocument();
  });

  it("only reads the ntrip and base-mode routes", async () => {
    renderPage();
    await screen.findByText(ntripInfo().connection_url);
    await screen.findByText("str2str");
    const urls = calls();
    expect(urls.length).toBeGreaterThan(0);
    for (const u of urls) expect(u).toMatch(/^\/api\/(ntrip(\/clients|\/history\?limit=100)?|base\/mode)$/);
  });
});
