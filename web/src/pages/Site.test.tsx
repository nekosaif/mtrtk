import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { resetPrefsForTests } from "@/lib/prefs";
import type { BaseModeView, ConfigResponse, Site as SiteT, SurveyIn } from "@/lib/types";
import { resetMaplibreMock } from "@/test/maplibreMock";
import { sampleState } from "@/test/fixtures";
import Site from "./Site";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

// ---- fixtures -----------------------------------------------------------------------------

const sites: SiteT[] = [
  { id: 1, name: "roof", x: 1234567.8912, y: -987654.3234, z: 5555555.0, lat: 61.1, lon: -38.6, height_m: 12.3, sigma_x: 0.004, sigma_y: 0.004, sigma_z: 0.004, frame: "ITRF2020", epoch: "2026.71", source: "csrs-ppp", notes: null, created_utc: "2026-09-18T10:00:00+00:00", active: true },
  { id: 2, name: "field", x: 1, y: 2, z: 3, lat: 0, lon: 0, height_m: 0, sigma_x: null, sigma_y: null, sigma_z: null, frame: "WGS84 (receiver)", epoch: null, source: "survey-in", notes: null, created_utc: "2026-09-18T11:00:00+00:00", active: false },
];
const mode: BaseModeView = { available: true, mode: "fixed", site: "roof", verified: true, last_1005: { station_id: 0, x: 1234567.8912, y: -987654.3234, z: 5555555.0 }, svin: { min_duration_s: 300, acc_limit_m: 2.0 } };

const validSurvey: SurveyIn = { active: true, valid: true, dur_s: 420, obs: 410, mean_x_m: -26748.1, mean_y_m: 5837156.6, mean_z_m: 2561801.3, mean_acc_m: 0.8 };

const config = (pending: ConfigResponse["pending"] = {}): ConfigResponse =>
  ({ values: { web_password: null, base_mode: "fixed", active_site: "roof" }, pending, env_file: "/data/.env", secret_keys: [], live_keys: [], read_only_keys: [], url_secret_keys: [] }) as unknown as ConfigResponse;

interface Answers {
  sites?: SiteT[];
  mode?: BaseModeView;
  config?: ConfigResponse;
  /** `{site, applied}` answered by POST activate. */
  activateApplied?: boolean;
  /** Status + detail for POST survey/restart; default 200 with the mode view. */
  restart?: { status: number; detail: string };
  /** Status + detail for DELETE; default 200. */
  remove?: { status: number; detail: string };
  /** Status + detail for PUT mode; default 200 with the mode view. */
  putMode?: { status: number; detail: string };
}

let calls: [string, RequestInit | undefined][] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** The routes the page may touch; anything else is a 404 and shows up in `calls`. */
function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    calls.push([p, init]);
    const m = init?.method ?? "GET";
    if (m === "GET") {
      if (p.endsWith("/api/base/sites")) return json(a.sites ?? sites);
      if (p.endsWith("/api/base/mode")) return json(a.mode ?? mode);
      if (p.endsWith("/api/config")) return json(a.config ?? config());
      if (p.includes("/api/logs/availability")) return json(Array.from({ length: 24 }, (_, i) => ({ hour_utc: `2026-09-18T${String(i).padStart(2, "0")}:00:00+00:00`, available: i < 20, bytes: 1, complete: true })));
      return json({ detail: "not found" }, 404);
    }
    if (m === "POST" && p.endsWith("/activate")) {
      const name = decodeURIComponent(p.split("/").at(-2)!);
      const site = (a.sites ?? sites).find((s) => s.name === name)!;
      return json({ site: { ...site, active: true }, applied: a.activateApplied ?? true });
    }
    if (m === "POST" && p.endsWith("/api/base/sites")) {
      const body = JSON.parse(String(init!.body)) as { name: string };
      return json({ site: { ...sites[0], id: 3, name: body.name, active: false }, applied: false });
    }
    if (m === "POST" && p.endsWith("/api/base/survey/freeze")) {
      const body = JSON.parse(String(init!.body)) as { name: string; activate?: boolean };
      return json({ site: { ...sites[1], id: 4, name: body.name, active: Boolean(body.activate) }, applied: Boolean(body.activate) });
    }
    if (m === "POST" && p.endsWith("/api/base/survey/restart")) return a.restart ? json({ detail: a.restart.detail }, a.restart.status) : json(a.mode ?? mode);
    if (m === "PUT" && p.endsWith("/api/base/mode")) return a.putMode ? json({ detail: a.putMode.detail }, a.putMode.status) : json(a.mode ?? mode);
    if (m === "DELETE" && p.includes("/api/base/sites/")) return a.remove ? json({ detail: a.remove.detail }, a.remove.status) : json({ ok: true });
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
}

const callsTo = (method: string, suffix: string) => calls.filter(([u, i]) => (i?.method ?? "GET") === method && u.endsWith(suffix));
const bodyOf = (call: [string, RequestInit | undefined]) => JSON.parse(String(call[1]!.body)) as Record<string, unknown>;

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Site /></MemoryRouter>
    </QueryClientProvider>,
  );
}

const region = (name: string | RegExp) => screen.getByRole("region", { name });
const findRegion = (name: string | RegExp) => screen.findByRole("region", { name });

function setLive(over: Partial<{ survey: SurveyIn }> = {}) {
  const state = sampleState();
  if (over.survey) state.survey_in = over.survey;
  useLive.setState({ state, status: "open", connected: true, stale: false, lastEpochAt: Date.now(), receiverConnected: true, ntripClients: [], base: { mode: "fixed", site: "roof", reason: null, verified: true, mismatch: null } });
}

describe("Site page", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    resetMaplibreMock();
    localStorage.clear();
    setLive();
    mockFetch();
  });

  // ---- the brief's acceptance tests --------------------------------------------------------

  it("lists sites, marks the active one and shows 1005 verification", async () => {
    renderPage();
    const table = await screen.findByRole("table", { name: /sites/i });
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByText("roof").closest("tr")).toHaveTextContent(/active/i);
    expect(await screen.findByText(/matches the active site/i)).toBeInTheDocument();
    expect(screen.getByText(/20 of 24 hours/i)).toBeInTheDocument();
  });

  it("activates a site after a confirmation and persists the mode", async () => {
    // Ruling 5: activation goes through a ConfirmDialog; ruling 1: a follow-up PUT persists it.
    renderPage();
    const row = (await screen.findByText("field")).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: /activate/i }));
    const dialog = screen.getByRole("dialog", { name: /activate field/i });
    expect(dialog).toHaveTextContent(/within 10 s/i);
    await userEvent.click(within(dialog).getByRole("button", { name: "Activate" }));
    await waitFor(() => expect(callsTo("POST", "/api/base/sites/field/activate")).toHaveLength(1));
    await waitFor(() => expect(callsTo("PUT", "/api/base/mode")).toHaveLength(1));
    expect(bodyOf(callsTo("PUT", "/api/base/mode")[0])).toEqual({ mode: "fixed", site: "field" });
    const status = await screen.findByRole("status", { name: /site result/i });
    expect(status).toHaveTextContent(/field is the active site/i);
    expect(status).toHaveTextContent(/saved to \.env/i);
  });

  it("adds a site from the ECEF form", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /add site/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^name/i), "new");
    await userEvent.type(within(dialog).getByLabelText(/^x/i), "1234567.8912");
    await userEvent.type(within(dialog).getByLabelText(/^y/i), "-987654.3234");
    await userEvent.type(within(dialog).getByLabelText(/^z/i), "5555555.0");
    await userEvent.click(within(dialog).getByRole("button", { name: /save site/i }));
    await waitFor(() => expect(callsTo("POST", "/api/base/sites")).toHaveLength(1));
    const body = bodyOf(callsTo("POST", "/api/base/sites")[0]);
    expect(body).toMatchObject({ name: "new", x: 1234567.8912, y: -987654.3234, z: 5555555.0, frame: "ITRF2020" });
    // Exactly one coordinate triple leaves the form (ruling 5): no geodetic keys at all.
    expect(Object.keys(body)).not.toEqual(expect.arrayContaining(["lat", "lon", "height_m"]));
    expect(body).not.toHaveProperty("lat");
    expect(body).not.toHaveProperty("height_m");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("freeze is disabled while survey-in is not valid", async () => {
    renderPage();
    expect(await screen.findByRole("button", { name: /freeze as site/i })).toBeDisabled();
  });

  // ---- SiteForm (ruling 5) -----------------------------------------------------------------

  it("adds a site from the geodetic tab with only lat/lon/height", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /add site/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("tab", { name: /lat/i }));
    await userEvent.type(within(dialog).getByLabelText(/^name/i), "llh");
    await userEvent.type(within(dialog).getByLabelText(/^latitude/i), "23.8373506");
    await userEvent.type(within(dialog).getByLabelText(/^longitude/i), "90.2625502");
    await userEvent.type(within(dialog).getByLabelText(/^height/i), "-36.268");
    await userEvent.type(within(dialog).getByLabelText(/sigma/i), "0.01");
    await userEvent.clear(within(dialog).getByLabelText(/^source/i));
    await userEvent.type(within(dialog).getByLabelText(/^source/i), "auspos");
    await userEvent.click(within(dialog).getByRole("button", { name: /save site/i }));
    await waitFor(() => expect(callsTo("POST", "/api/base/sites")).toHaveLength(1));
    const body = bodyOf(callsTo("POST", "/api/base/sites")[0]);
    expect(body).toEqual({ name: "llh", lat: 23.8373506, lon: 90.2625502, height_m: -36.268, sigma_m: 0.01, source: "auspos", frame: "ITRF2020" });
  });

  it("refuses half a coordinate before it reaches the API", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /add site/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^name/i), "half");
    await userEvent.type(within(dialog).getByLabelText(/^x/i), "1");
    await userEvent.click(within(dialog).getByRole("button", { name: /save site/i }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/all three/i);
    expect(callsTo("POST", "/api/base/sites")).toHaveLength(0);
  });

  it("shows a duplicate-name 409 verbatim inside the form", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const p = String(input);
      if (init?.method === "POST" && p.endsWith("/api/base/sites")) return json({ detail: "a site named 'roof' already exists" }, 409);
      if (p.endsWith("/api/base/sites")) return json(sites);
      if (p.endsWith("/api/base/mode")) return json(mode);
      if (p.endsWith("/api/config")) return json(config());
      return json([], 200);
    }) as typeof fetch;
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /add site/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^name/i), "roof");
    await userEvent.type(within(dialog).getByLabelText(/^x/i), "1");
    await userEvent.type(within(dialog).getByLabelText(/^y/i), "2");
    await userEvent.type(within(dialog).getByLabelText(/^z/i), "3");
    await userEvent.click(within(dialog).getByRole("button", { name: /save site/i }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("a site named 'roof' already exists");
  });

  // ---- activation semantics (ruling 1) -----------------------------------------------------

  it("shows applied:false as a warning, not a success, and does not persist the mode", async () => {
    mockFetch({ activateApplied: false });
    renderPage();
    const row = (await screen.findByText("field")).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: /activate/i }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Activate" }));
    const status = await screen.findByRole("status", { name: /site result/i });
    expect(status).toHaveTextContent(/not applied/i);
    expect(status).toHaveTextContent(/receiver is not on it/i);
    expect(status).not.toHaveTextContent(/saved to \.env/i);
    expect(callsTo("PUT", "/api/base/mode")).toHaveLength(0);
  });

  it("says when the follow-up persist fails, with the detail", async () => {
    mockFetch({ putMode: { status: 409, detail: "base mode manager not running: this daemon has no base mode (rover role or replay source)" } });
    renderPage();
    const row = (await screen.findByText("field")).closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: /activate/i }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Activate" }));
    const status = await screen.findByRole("status", { name: /site result/i });
    expect(status).toHaveTextContent(/field is the active site/i);
    expect(status).toHaveTextContent(/not saved to \.env/i);
    expect(status).toHaveTextContent("base mode manager not running: this daemon has no base mode (rover role or replay source)");
  });

  it("surfaces the config's pending base mode as an applies-after-restart note", async () => {
    mockFetch({ config: config({ base_mode: "survey-in", active_site: null }) });
    renderPage();
    const panel = await findRegion("Position mode");
    expect(await within(panel).findByText(/after a restart/i)).toHaveTextContent(/survey-in/);
  });

  // ---- position mode panel (ruling 4) ------------------------------------------------------

  it("disables every receiver write when no manager is available and explains why", async () => {
    mockFetch({ mode: { ...mode, available: false, site: null, verified: false, last_1005: null } });
    renderPage();
    const panel = await findRegion("Position mode");
    expect(await within(panel).findByText(/replay or rover role/i)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /apply/i })).toBeDisabled();
    expect(within(panel).getByRole("radio", { name: /survey-in/i })).toBeDisabled();
    const survey = region("Survey-in");
    expect(within(survey).getByRole("button", { name: /restart survey-in/i })).toBeDisabled();
    expect(within(survey).getByRole("button", { name: /freeze as site/i })).toBeDisabled();
    // Sites are durable state a base picks up at its next start: the table stays writable.
    expect(screen.getByRole("button", { name: /add site/i })).toBeEnabled();
  });

  it("requires a site for fixed mode and sends it with the PUT", async () => {
    mockFetch({ mode: { ...mode, mode: "survey-in", site: null, verified: false, last_1005: null } });
    setLive({ survey: { ...validSurvey, valid: false, dur_s: 120, obs: 118, mean_acc_m: 1.9 } });
    renderPage();
    const panel = await findRegion("Position mode");
    await waitFor(() => expect(within(panel).getByRole("radio", { name: /survey-in/i })).toBeChecked());
    await userEvent.click(within(panel).getByRole("radio", { name: /^fixed/i }));
    const select = within(panel).getByRole("combobox", { name: /site/i });
    // No site chosen yet: the hint stands in for the daemon's 409.
    await userEvent.selectOptions(select, "");
    expect(within(panel).getByText(/needs a site/i)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /apply/i })).toBeDisabled();
    await userEvent.selectOptions(select, "roof");
    expect(within(panel).queryByText(/needs a site/i)).toBeNull();
    await userEvent.click(within(panel).getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(callsTo("PUT", "/api/base/mode")).toHaveLength(1));
    expect(bodyOf(callsTo("PUT", "/api/base/mode")[0])).toEqual({ mode: "fixed", site: "roof" });
  });

  it("bounds the survey parameters and sends them with survey-in", async () => {
    renderPage();
    const panel = await findRegion("Position mode");
    await waitFor(() => expect(within(panel).getByRole("radio", { name: /^fixed/i })).toBeChecked());
    await userEvent.click(within(panel).getByRole("radio", { name: /survey-in/i }));
    const dur = within(panel).getByLabelText(/minimum duration/i);
    const acc = within(panel).getByLabelText(/accuracy limit/i);
    expect(dur).toHaveValue(300);
    expect(acc).toHaveValue(2);
    await userEvent.clear(dur);
    await userEvent.type(dur, "0");
    expect(within(panel).getByText(/1 and 86400/i)).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: /apply/i })).toBeDisabled();
    await userEvent.clear(dur);
    await userEvent.type(dur, "600");
    await userEvent.clear(acc);
    await userEvent.type(acc, "1.5");
    await userEvent.click(within(panel).getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(callsTo("PUT", "/api/base/mode")).toHaveLength(1));
    expect(bodyOf(callsTo("PUT", "/api/base/mode")[0])).toEqual({ mode: "survey-in", svin_min_duration_s: 600, svin_acc_limit_m: 1.5 });
    expect(await screen.findByRole("status", { name: /mode result/i })).toHaveTextContent(/survey-in/);
  });

  it("shows a PUT mode 409 verbatim under the form", async () => {
    mockFetch({ putMode: { status: 409, detail: "no site to sit on: activate one first" } });
    renderPage();
    const panel = await findRegion("Position mode");
    await waitFor(() => expect(within(panel).getByRole("radio", { name: /^fixed/i })).toBeChecked());
    await userEvent.click(within(panel).getByRole("radio", { name: /^off/i }));
    await userEvent.click(within(panel).getByRole("button", { name: /apply/i }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("no site to sit on: activate one first");
  });

  // ---- survey-in panel (rulings 2, 3) ------------------------------------------------------

  it("shows progress against the duration and the accuracy gate, and explains meanAcc", async () => {
    renderPage();
    const survey = await findRegion("Survey-in");
    const elapsed = await within(survey).findByRole("meter", { name: /elapsed/i });
    expect(elapsed).toHaveAttribute("aria-valuenow", "120");
    expect(elapsed).toHaveAttribute("aria-valuemax", "300");
    expect(elapsed).toHaveTextContent("2m 00s");
    expect(elapsed).toHaveTextContent("5m 00s");
    const gate = within(survey).getByRole("meter", { name: /accuracy/i });
    expect(gate).toHaveTextContent("1.90 m");
    expect(gate).toHaveTextContent("2.00 m");
    expect(survey).toHaveTextContent(/mean accuracy.*not.*hAcc/i);
    expect(survey).toHaveTextContent(/indoors/i);
  });

  it("restarts the survey-in through a confirmation and shows a 409 detail verbatim", async () => {
    const detail = "TMODE off was applied but the receiver refused or did not answer the new survey-in: the base is in TMODE off and is not surveying - retry to send both steps again";
    mockFetch({ mode: { ...mode, mode: "survey-in", site: null, verified: false, last_1005: null }, restart: { status: 409, detail } });
    renderPage();
    const survey = await findRegion("Survey-in");
    const trigger = within(survey).getByRole("button", { name: /restart survey-in/i });
    await waitFor(() => expect(trigger).toBeEnabled());
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: /restart/i });
    expect(dialog).toHaveTextContent(/same parameters/i);
    await userEvent.click(within(dialog).getByRole("button", { name: "Restart" }));
    await waitFor(() => expect(callsTo("POST", "/api/base/survey/restart")).toHaveLength(1));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(detail);
  });

  it("restart is disabled with a reason when the base is not surveying", async () => {
    renderPage();
    const survey = await findRegion("Survey-in");
    await waitFor(() => expect(within(survey).getByRole("button", { name: /restart survey-in/i })).toBeDisabled());
    expect(survey).toHaveTextContent(/fixed/i);
  });

  it("freezes a valid survey-in as a site, activates it on request and persists the mode", async () => {
    mockFetch({ mode: { ...mode, mode: "survey-in", site: null, verified: false, last_1005: null } });
    setLive({ survey: validSurvey });
    renderPage();
    const survey = await findRegion("Survey-in");
    const trigger = within(survey).getByRole("button", { name: /freeze as site/i });
    await waitFor(() => expect(trigger).toBeEnabled());
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: /freeze/i });
    const confirm = within(dialog).getByRole("button", { name: "Freeze" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByRole("textbox", { name: /site name/i }), "roof-2026");
    const activate = within(dialog).getByRole("checkbox", { name: /activate now/i });
    expect(activate).toBeChecked();
    await userEvent.click(confirm);
    await waitFor(() => expect(callsTo("POST", "/api/base/survey/freeze")).toHaveLength(1));
    expect(bodyOf(callsTo("POST", "/api/base/survey/freeze")[0])).toEqual({ name: "roof-2026", activate: true });
    await waitFor(() => expect(callsTo("PUT", "/api/base/mode")).toHaveLength(1));
    expect(bodyOf(callsTo("PUT", "/api/base/mode")[0])).toEqual({ mode: "fixed", site: "roof-2026" });
    expect(await screen.findByRole("status", { name: /site result/i })).toHaveTextContent(/roof-2026/);
  });

  // ---- sites table (ruling 5) --------------------------------------------------------------

  it("disables delete for the active site and deletes another after typing its name", async () => {
    renderPage();
    const table = await screen.findByRole("table", { name: /sites/i });
    const roof = within(table).getByText("roof").closest("tr")!;
    expect(within(roof).getByRole("button", { name: /delete/i })).toBeDisabled();
    expect(within(roof).queryByRole("button", { name: /activate/i })).toBeNull();
    const field = within(table).getByText("field").closest("tr")!;
    await userEvent.click(within(field).getByRole("button", { name: /delete/i }));
    const dialog = screen.getByRole("dialog", { name: /delete site field/i });
    await userEvent.type(within(dialog).getByRole("textbox", { name: /type field to continue/i }), "field");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(callsTo("DELETE", "/api/base/sites/field")).toHaveLength(1));
  });

  it("shows a raced delete 409 verbatim", async () => {
    mockFetch({ remove: { status: 409, detail: "site 'field' is active: activate another site before deleting it" } });
    renderPage();
    const table = await screen.findByRole("table", { name: /sites/i });
    const field = within(table).getByText("field").closest("tr")!;
    await userEvent.click(within(field).getByRole("button", { name: /delete/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByRole("textbox", { name: /type field to continue/i }), "field");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("site 'field' is active: activate another site before deleting it");
  });

  it("offers an empty state when there are no sites", async () => {
    mockFetch({ sites: [], mode: { ...mode, mode: "off", site: null, verified: false, last_1005: null } });
    renderPage();
    expect(await screen.findByText(/no sites yet/i)).toBeInTheDocument();
  });

  // ---- verification (ruling 6) -------------------------------------------------------------

  it("reports a 1005 that differs from the active site in millimetres", async () => {
    mockFetch({ mode: { ...mode, verified: false, last_1005: { station_id: 0, x: 1234567.8912 + 0.012, y: -987654.3234, z: 5555555.0 - 0.003 } } });
    useLive.setState({ base: { mode: "fixed", site: "roof", reason: null, verified: false, mismatch: { site: "roof", dx: 0.012, dy: 0, dz: -0.003 } } });
    renderPage();
    const panel = await findRegion("Verification");
    expect(await within(panel).findByText(/differs from the active site/i)).toBeInTheDocument();
    expect(panel).toHaveTextContent("12.0 mm");
    expect(panel).toHaveTextContent("-3.0 mm");
    expect(within(panel).getByText(/site mismatch/i)).toBeInTheDocument();
  });

  it("waits for a 1005 when none has been seen", async () => {
    mockFetch({ mode: { ...mode, verified: false, last_1005: null } });
    useLive.setState({ base: { mode: "fixed", site: "roof", reason: null, verified: null, mismatch: null } });
    renderPage();
    const panel = await findRegion("Verification");
    expect(await within(panel).findByText(/waiting for rtcm 1005/i)).toBeInTheDocument();
  });

  // ---- PPP steps (ruling 7) ----------------------------------------------------------------

  it("links the export step to the logs page and keeps the import for Phase 5", async () => {
    renderPage();
    const ppp = await findRegion(/PPP/);
    expect(within(ppp).getByRole("link", { name: /open logs/i })).toHaveAttribute("href", "/logs");
    expect(within(ppp).getByRole("button", { name: /import.*phase 5/i })).toBeDisabled();
    expect(within(ppp).getByRole("link", { name: /CSRS-PPP/ })).toHaveAttribute("target", "_blank");
    await userEvent.click(within(ppp).getByRole("button", { name: /enter ppp result/i }));
    const dialog = screen.getByRole("dialog", { name: /ppp/i });
    expect(within(dialog).getByLabelText(/^source/i)).toHaveValue("csrs-ppp");
    expect(within(dialog).getByLabelText(/^frame/i)).toHaveValue("ITRF2020");
  });

  // ---- page frame ----------------------------------------------------------------------------

  it("greys the figures when the live feed is stale", async () => {
    useLive.setState({ stale: true });
    renderPage();
    const grid = await screen.findByTestId("site-grid");
    expect(grid).toHaveAttribute("data-stale", "true");
    expect(grid.className).toContain("[&_.num]:text-ink-3");
  });

  it("reads only the routes it needs", async () => {
    renderPage();
    await screen.findByRole("table", { name: /sites/i });
    await findRegion("Position mode");
    const reads = calls.filter(([, i]) => !i?.method || i.method === "GET").map(([u]) => u);
    expect(reads.every((u) => u.endsWith("/api/base/sites") || u.endsWith("/api/base/mode") || u.endsWith("/api/config") || u.includes("/api/logs/availability"))).toBe(true);
  });
});
