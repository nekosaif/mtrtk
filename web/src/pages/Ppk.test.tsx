import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { binQualities, QualityStrip } from "@/components/QualityStrip";
import { STATUS_MARK } from "@/lib/palette";
import { trackBounds } from "@/components/TrackMap";
import { resetLiveForTests, useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import { maps, resetMaplibreMock } from "@/test/maplibreMock";
import Ppk, { parseCsv, parseXyz, qualitiesOf, windowProblem } from "./Ppk";
import { preloadMaps } from "@/test/lazyMaps";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));
beforeAll(preloadMaps); // the lazy maps resolve from the module cache, not a cold transform

const BASE_URL = "http://100.100.50.10:8080";
const DEFAULTS = { rnx2rtkp: true, convbin: true, demo5: true, conf: { "pos1-posmode": "kinematic", "pos1-elmask": "15" }, ntrip_base_url: BASE_URL, max_upload_bytes: 2 * 1024 ** 3 };
const SUMMARY = { epochs: 60, duration_s: 59, interval_s: 1, fixed_pct: 96.7, float_pct: 3.3, single_pct: 0, mean_sd_fixed: { n: 0.004, e: 0.003, u: 0.009 }, gaps: [], time_system: "GPST", first_time: "2026-09-18T10:00:00", last_time: "2026-09-18T10:00:59" };
const FILES = [
  { name: "track.csv", bytes: 4000 },
  { name: "track.geojson", bytes: 9000 },
  { name: "track.pos", bytes: 7000 },
  { name: "events.csv", bytes: 300 },
  { name: "events.geojson", bytes: 400 },
];
const DONE = {
  id: "abc123",
  kind: "ppk",
  status: "done",
  progress: 1,
  message: "done",
  params: { rover: { kind: "window", start: "2026-09-18T10:00:00Z", end: "2026-09-18T11:00:00Z" }, base: { kind: "remote", url: BASE_URL } },
  result: { summary: SUMMARY, events: { total: 2, ok: 1, gap_too_large: 1, no_neighbours: 0 }, warnings: ["the base position is the base RINEX's APPROX POSITION XYZ"], inputs: { base_xyz_source: "remote-site:roof", soltype: "combined" }, files: FILES },
  error: null,
  created_utc: "2026-09-18T11:05:00+00:00",
  updated_utc: "2026-09-18T11:06:00+00:00",
};
const TRACK = {
  type: "FeatureCollection",
  features: [
    { type: "Feature", geometry: { type: "LineString", coordinates: [[90.26, 23.83, 10], [90.27, 23.84, 11]] }, properties: { q: 2, epochs: 2 } },
    { type: "Feature", geometry: { type: "LineString", coordinates: [[90.27, 23.84, 11], [90.28, 23.85, 12]] }, properties: { q: 1, epochs: 58 } },
    { type: "Feature", geometry: { type: "Point", coordinates: [90.26, 23.83, 10] }, properties: { q: 2 } },
  ],
};
const EVENTS_GEO = { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [90.26, 23.83, 10] }, properties: { count: 7, status: "ok" } }] };
const SESSION = { id: 5, name: "Field 1", start_utc: "2026-09-18T10:00:00+00:00", end_utc: "2026-09-18T11:00:00+00:00" };
/** What the daemon keeps per uploaded file name. */
const UPLOADS: Record<string, unknown> = {
  "rover.ubx": { upload_id: "a00000000001", name: "rover.ubx", bytes: 8, detected: "ubx", rinex: null, kind: "rover" },
  "base.obs": { upload_id: "b00000000001", name: "base.obs", bytes: 81, detected: "rinex", rinex: "obs", kind: "base" },
  "base.nav": { upload_id: "c00000000001", name: "base.nav", bytes: 81, detected: "rinex", rinex: "nav", kind: "base" },
  "base.ubx": { upload_id: "d00000000001", name: "base.ubx", bytes: 8, detected: "ubx", rinex: null, kind: "base" },
};
const EVENTS_CSV = "n,count,gps_week,gps_tow_s,time_gpst,lat,lon,height_m,q,sdn_m,sde_m,sdu_m,interp_gap_s,status,time_utc\n1,7,2384,468000.5,2026-09-18 10:00:00.500,23.830000000,90.260000000,10.000,1,0.004,0.003,0.009,1.0,ok,2026-09-18 09:59:42.500\n2,8,2384,468030.0,2026-09-18 10:00:30.000,,,,,,,,5.0,gap_too_large,2026-09-18 10:00:12.000\n";

let calls: [string, RequestInit | undefined][] = [];
let defaults: unknown = DEFAULTS;
let sessions: unknown[] = [];
let jobs: unknown[] = [DONE];
/** events.geojson is answered only once a test releases it: it may land after the map's style. */
let releaseEvents: () => void = () => {};
let submitResponse: () => Response = () => new Response(JSON.stringify({ ...DONE, id: "new1", status: "queued", result: null }), { status: 200 });

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

let qc: QueryClient;

function renderPpk() {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Ppk />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("PPK page", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetMaplibreMock();
    calls = [];
    defaults = DEFAULTS;
    sessions = [];
    jobs = [DONE];
    const eventsGate = new Promise<void>((resolve) => {
      releaseEvents = resolve;
    });
    submitResponse = () => json({ ...DONE, id: "new1", status: "queued", result: null });
    useLive.setState({ state: sampleState(), role: "rover", status: "open", lastEpochAt: Date.now() });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const u = String(url);
      calls.push([u, init]);
      if (u === "/api/ppk/defaults") return json(defaults);
      if (u === "/api/rover/sessions") return json(sessions);
      if (u === "/api/base/sites") return json([{ name: "roof", active: true }]);
      if (u.startsWith("/api/jobs?") && !init?.method) return json(jobs);
      if (u === "/api/ppk" && init?.method === "POST") return submitResponse();
      if (u === "/api/ppk/upload" && init?.method === "POST") return json(UPLOADS[((init.body as FormData).get("file") as File).name]);
      if (u === "/api/jobs/abc123" && init?.method === "DELETE") {
        jobs = [];
        return json({ deleted: "abc123" });
      }
      if (u === "/api/jobs/abc123/files/events.geojson") {
        await eventsGate;
        return json(EVENTS_GEO);
      }
      if (u === "/api/jobs/abc123/files") return json(FILES);
      if (u === "/api/jobs/abc123/files/track.geojson") return json(TRACK);
      if (u === "/api/jobs/abc123/files/events.csv") return new Response(EVENTS_CSV, { status: 200 });
      return json({});
    }) as typeof fetch;
  });
  // Every request answered and rendered before the test ends: no update lands outside act().
  afterEach(async () => {
    await waitFor(() => expect(qc.isFetching() + qc.isMutating()).toBe(0));
    act(() => resetLiveForTests());
  });

  it("prefills the remote base's address and posts a window against it", async () => {
    renderPpk();
    const url = await screen.findByLabelText(/base web address/i);
    await waitFor(() => expect(url).toHaveValue(BASE_URL));
    await userEvent.click(screen.getByRole("radio", { name: "Window" }));
    await userEvent.click(screen.getByRole("button", { name: /run ppk/i }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/ppk" && i?.method === "POST")).toBe(true));
    const [, init] = calls.find(([u, i]) => u === "/api/ppk" && i?.method === "POST")!;
    const body = JSON.parse(init!.body as string);
    expect(body).toMatchObject({ rover: { kind: "window" }, base: { kind: "remote", url: BASE_URL }, events: true, include_qzss: false });
    expect(body.rover.start).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$/);
    expect(Date.parse(body.rover.end) - Date.parse(body.rover.start)).toBe(3600_000);
    // The remote base's site is the default position: neither a site nor coordinates are sent.
    expect(body).not.toHaveProperty("base_site");
    expect(body).not.toHaveProperty("base_xyz");
    expect(body).not.toHaveProperty("conf_overrides");
  });

  it("sends a changed elevation mask as an rnx2rtkp override and manual ECEF coordinates", async () => {
    renderPpk();
    await screen.findByLabelText(/base web address/i);
    await userEvent.click(screen.getByRole("radio", { name: "Window" }));
    await userEvent.click(screen.getByRole("radio", { name: "Manual XYZ" }));
    const run = screen.getByRole("button", { name: /run ppk/i });
    await userEvent.type(screen.getByLabelText("X (m)"), "23.78");
    await userEvent.type(screen.getByLabelText("Y (m)"), "90.41");
    await userEvent.type(screen.getByLabelText("Z (m)"), "12");
    expect(run).toBeDisabled(); // latitude/longitude typed as ECEF
    expect(screen.getByText(/ECEF X, Y, Z in metres/)).toBeInTheDocument();
    for (const [axis, v] of [["X (m)", "-26748.172"], ["Y (m)", "5837156.618"], ["Z (m)", "2561801.261"]]) {
      await userEvent.clear(screen.getByLabelText(axis));
      await userEvent.type(screen.getByLabelText(axis), v);
    }
    const mask = screen.getByLabelText(/elevation mask/i);
    await userEvent.clear(mask);
    await userEvent.type(mask, "10");
    await userEvent.click(run);
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/ppk" && i?.method === "POST")).toBe(true));
    const body = JSON.parse(calls.find(([u, i]) => u === "/api/ppk" && i?.method === "POST")![1]!.body as string);
    expect(body.base_xyz).toEqual([-26748.172, 5837156.618, 2561801.261]);
    expect(body.conf_overrides).toEqual({ "pos1-elmask": "10" });
  });

  it("shows a refusal's issues field by field, as the daemon sent them", async () => {
    submitResponse = () => json({ detail: [{ loc: ["body", "rover"], msg: "start must be before end", type: "value_error" }] }, 422);
    renderPpk();
    await screen.findByLabelText(/base web address/i);
    await userEvent.click(screen.getByRole("radio", { name: "Window" }));
    await userEvent.click(screen.getByRole("button", { name: /run ppk/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("rover: start must be before end");
  });

  it("selecting a done job shows its summary, quality, map, events and downloads", async () => {
    renderPpk();
    await userEvent.click(await screen.findByRole("button", { name: "View job abc123" }));
    const result = await screen.findByRole("region", { name: /result · job abc123/i });
    expect(within(result).getByText("96.7 %")).toBeInTheDocument();
    expect(within(result).getByText("4.0 mm / 3.0 mm / 9.0 mm")).toBeInTheDocument();
    expect(within(result).getByRole("link", { name: "Download track.csv" })).toHaveAttribute("href", "/api/jobs/abc123/files/track.csv");
    expect(within(result).getByRole("link", { name: "Download summary.json" })).toBeInTheDocument();
    expect(await within(result).findByRole("img", { name: "Solution quality: 97% fixed" })).toBeInTheDocument();
    expect(within(result).getByText(/APPROX POSITION XYZ/)).toBeInTheDocument();
    const table = await within(result).findByRole("table", { name: "Camera events" });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("gap too large")).toBeInTheDocument();
    // The map draws the track once its style loads, and is fitted to it.
    await waitFor(() => expect(maps).toHaveLength(1));
    maps[0].fire("style.load");
    expect(maps[0].layers).toEqual(["track"]);
    expect(maps[0].fitCalls[0]).toEqual([[90.26, 23.83, 90.28, 23.85], expect.objectContaining({ padding: 40 })]);
    // events.geojson lands after the style has loaded: its points are still drawn.
    act(() => releaseEvents());
    await waitFor(() => expect(maps[0].layers).toEqual(["track", "events"]));
    expect(maps[0].getSource("events")!.data).toEqual(EVENTS_GEO);
  });

  it("closes the result of a job once that job is deleted", async () => {
    renderPpk();
    await userEvent.click(await screen.findByRole("button", { name: "View job abc123" }));
    expect(await screen.findByRole("region", { name: /result · job abc123/i })).toBeInTheDocument();
    act(() => releaseEvents());
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/jobs/abc123" && i?.method === "DELETE")).toBe(true));
    await waitFor(() => expect(screen.queryByRole("region", { name: /result · job abc123/i })).not.toBeInTheDocument());
  });

  it("uploads the rover and base files and sends their ids, the navigation file with a RINEX base", async () => {
    renderPpk();
    await screen.findByLabelText(/base web address/i);
    await userEvent.click(within(screen.getByRole("radiogroup", { name: "Rover source" })).getByRole("radio", { name: "Upload" }));
    await userEvent.upload(screen.getByLabelText("Rover file"), new File([new Uint8Array([0xb5, 0x62])], "rover.ubx"));
    await screen.findByText("rover.ubx");
    await userEvent.click(within(screen.getByRole("radiogroup", { name: "Base source" })).getByRole("radio", { name: "Upload" }));
    await userEvent.upload(screen.getByLabelText("Base file"), new File(["obs"], "base.obs"));
    await userEvent.upload(await screen.findByLabelText("Navigation file (optional)"), new File(["nav"], "base.nav"));
    await screen.findByText("base.nav");
    const uploads = calls.filter(([u, i]) => u === "/api/ppk/upload" && i?.method === "POST").map(([, i]) => i!.body as FormData);
    expect(uploads.map((f) => [...f.keys()])).toEqual([["kind", "file"], ["kind", "file"], ["kind", "file"]]);
    expect(uploads.map((f) => f.get("kind"))).toEqual(["rover", "base", "base"]);
    await userEvent.click(screen.getByRole("button", { name: /run ppk/i }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/ppk" && i?.method === "POST")).toBe(true));
    const body = JSON.parse(calls.find(([u, i]) => u === "/api/ppk" && i?.method === "POST")![1]!.body as string);
    expect(body.rover).toEqual({ kind: "upload", upload_id: "a00000000001" });
    expect(body.base).toEqual({ kind: "upload", upload_id: "b00000000001", nav_upload_id: "c00000000001" });
  });

  it("drops a navigation file once the base is replaced by a raw UBX file", async () => {
    renderPpk();
    await screen.findByLabelText(/base web address/i);
    await userEvent.click(screen.getByRole("radio", { name: "Window" }));
    await userEvent.click(within(screen.getByRole("radiogroup", { name: "Base source" })).getByRole("radio", { name: "Upload" }));
    await userEvent.upload(screen.getByLabelText("Base file"), new File(["obs"], "base.obs"));
    await userEvent.upload(await screen.findByLabelText("Navigation file (optional)"), new File(["nav"], "base.nav"));
    await screen.findByText("base.nav");
    await userEvent.upload(screen.getByLabelText("Base file"), new File([new Uint8Array([0xb5, 0x62])], "base.ubx"));
    await screen.findByText("base.ubx");
    expect(screen.queryByLabelText("Navigation file (optional)")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Site" }));
    await userEvent.click(screen.getByRole("button", { name: /run ppk/i }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/ppk" && i?.method === "POST")).toBe(true));
    const body = JSON.parse(calls.find(([u, i]) => u === "/api/ppk" && i?.method === "POST")![1]!.body as string);
    expect(body.base).toEqual({ kind: "upload", upload_id: "d00000000001" });
    expect(body.base_site).toBe("roof");
  });

  it("posts a rover session against the remote base by default on a rover", async () => {
    sessions = [SESSION];
    renderPpk();
    const url = await screen.findByLabelText(/base web address/i);
    await waitFor(() => expect(url).toHaveValue(BASE_URL));
    await screen.findByRole("option", { name: /Field 1/ });
    await userEvent.click(screen.getByRole("button", { name: /run ppk/i }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/ppk" && i?.method === "POST")).toBe(true));
    const body = JSON.parse(calls.find(([u, i]) => u === "/api/ppk" && i?.method === "POST")![1]!.body as string);
    expect(body.rover).toEqual({ kind: "session", session_id: 5 });
    expect(body.base).toEqual({ kind: "remote", url: BASE_URL });
  });

  it("says when RTKLIB is missing on this host", async () => {
    defaults = { ...DEFAULTS, rnx2rtkp: false, demo5: false };
    renderPpk();
    expect(await screen.findByText(/RTKLIB not installed on this host/)).toBeInTheDocument();
    expect(screen.getByText(/rnx2rtkp not found/)).toBeInTheDocument();
  });

  it("defaults a base host to its own logs and the window rover", async () => {
    useLive.setState({ role: "base" });
    renderPpk();
    expect(await screen.findByRole("radio", { name: "Local logs" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Window" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByRole("radio", { name: "Session" })).toBeDisabled();
    await screen.findByText(/else this host's active site, roof/);
    await userEvent.click(screen.getByRole("button", { name: /run ppk/i }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/ppk" && i?.method === "POST")).toBe(true));
    const body = JSON.parse(calls.find(([u, i]) => u === "/api/ppk" && i?.method === "POST")![1]!.body as string);
    expect(body.base).toEqual({ kind: "local" });
    expect(body.rover).toMatchObject({ kind: "window" });
    expect(body).not.toHaveProperty("base_site");
    expect(body).not.toHaveProperty("base_xyz");
  });
});

describe("PPK helpers", () => {
  it("checks a window", () => {
    expect(windowProblem("2026-09-18T10:00", "2026-09-18T11:00")).toBeNull();
    expect(windowProblem("2026-09-18T11:00", "2026-09-18T10:00")).toMatch(/before/);
    expect(windowProblem("2026-09-01T00:00", "2026-09-18T00:00")).toMatch(/7 days/);
    expect(windowProblem("", "2026-09-18T00:00")).toMatch(/both/);
  });

  it("takes ECEF metres, not latitude and longitude", () => {
    expect(parseXyz(["-26748.172", "5837156.618", "2561801.261"])).toEqual([-26748.172, 5837156.618, 2561801.261]);
    expect(parseXyz(["23.78", "90.41", "12"])).toBeNull();
    expect(parseXyz(["", "1", "2"])).toBeNull();
  });

  it("reads per-epoch quality from the track's runs and the events CSV", () => {
    expect(qualitiesOf(TRACK as never)).toEqual([2, 2, ...Array(58).fill(1)]);
    expect(parseCsv(EVENTS_CSV)).toHaveLength(2);
    expect(parseCsv(EVENTS_CSV, 1)[0]).toMatchObject({ count: "7", status: "ok" });
    expect(trackBounds(TRACK as never)).toEqual([90.26, 23.83, 90.28, 23.85]);
    expect(trackBounds({ type: "FeatureCollection", features: [] })).toBeNull();
  });

  it("bins a long strip into its commonest quality per cell", () => {
    const qs = [...Array(1000).fill(1), ...Array(200).fill(2)];
    const cells = binQualities(qs, 6);
    expect(cells).toEqual([1, 1, 1, 1, 1, 2]);
    expect(binQualities([1, 2, 5], 10)).toEqual([1, 2, 5]);
    render(<QualityStrip qs={[]} />);
    expect(screen.getByText("No epochs.")).toBeInTheDocument();
  });

  it("colours one strip cell per epoch by Q", () => {
    const { container } = render(<QualityStrip qs={[1, 2, 3, 4, 5, 6, 0]} />);
    const fills = [...container.querySelectorAll("rect")].map((r) => r.getAttribute("fill"));
    expect(fills).toEqual([STATUS_MARK.good, STATUS_MARK.warning, STATUS_MARK.serious, STATUS_MARK.serious, STATUS_MARK.critical, "var(--sys-galileo)", "var(--ink-3)"]);
  });
});
