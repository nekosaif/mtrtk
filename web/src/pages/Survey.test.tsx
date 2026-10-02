import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast } from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import { markers, resetMaplibreMock } from "@/test/maplibreMock";
import Survey from "./Survey";
import { preloadMaps } from "@/test/lazyMaps";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));
beforeAll(preloadMaps); // the lazy maps resolve from the module cache, not a cold transform

const points = [{ id: 1, session_id: 1, name: "BM-1", code: "BM", note: "brass", ts_utc: "2026-09-18T16:00:00+00:00", lat: 23.8373506, lon: 90.2625502, height_m: -36.268, hmsl_m: 13.363, n_epochs: 30, sd_n: 0.004, sd_e: 0.003, sd_u: 0.009, fix_type: 3, carr_soln: 2, h_acc_m: 0.012, v_acc_m: 0.018 }];
const openSession = { id: 1, name: "field-1", start_utc: "2026-09-18T15:00:00+00:00", end_utc: null, role: "rover", notes: null };
const IDLE = { state: "idle", name: null, target: 0, accepted: 0, skipped: 0, sd_n: null, sd_e: null, sd_u: null, mean_lat: null, mean_lon: null, mean_h: null, point_id: null, reason: null };
let calls: [string, RequestInit | undefined][] = [];
let sessions: unknown[] = [openSession];
let pointsBody: unknown = points;
let overview: unknown = {};
const pointsGets = () => calls.filter(([u, i]) => u.startsWith("/api/rover/points") && !u.includes("export") && !i?.method).length;

function renderSurvey() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><Survey /></MemoryRouter></QueryClientProvider>);
}

describe("Survey page", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetMaplibreMock();
    calls = [];
    sessions = [openSession];
    pointsBody = points;
    overview = {};
    useLive.setState({ state: sampleState(), role: "rover", status: "open", lastEpochAt: Date.now(), collect: null });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      const u = String(url);
      if (u.endsWith("/api/rover") && !init?.method) return new Response(JSON.stringify(overview), { status: 200 });
      if (u.endsWith("/api/rover/sessions") && !init?.method) return new Response(JSON.stringify(sessions), { status: 200 });
      if (u.includes("/api/rover/points") && !init?.method) return new Response(JSON.stringify(pointsBody), { status: 200 });
      if (u === "/api/rover/points/1" && init?.method === "DELETE") return new Response(JSON.stringify({ ok: true }), { status: 200 });
      if (u.endsWith("/api/rover/sessions/stop") && init?.method === "POST") return new Response(JSON.stringify({ ...openSession, end_utc: "2026-09-18T17:00:00+00:00" }), { status: 200 });
      if (u.endsWith("/api/rover/collect") && init?.method === "POST") return new Response(JSON.stringify({ ...IDLE, state: "collecting", name: "BM-2", target: 30 }), { status: 200 });
      if (u.endsWith("/api/rover/collect") && init?.method === "DELETE") return new Response(JSON.stringify({ ...IDLE, state: "aborted", name: "BM-2", target: 30, accepted: 3, reason: "cancelled" }), { status: 200 });
      if (u.endsWith("/api/rover/collect") && !init?.method) return new Response(JSON.stringify(IDLE), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("lists points, shows the open session and starts a collection", async () => {
    renderSurvey();
    expect(await screen.findByText("BM-1")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Session" })).getByText(/field-1/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/point name/i), "BM-2");
    await userEvent.click(screen.getByRole("button", { name: /collect point/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/rover/collect") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toMatchObject({ name: "BM-2", fixed_only: true });
    act(() => useLive.setState({ collect: { state: "collecting", name: "BM-2", target: 30, accepted: 12, skipped: 1, sd_n: 0.004, sd_e: 0.003, sd_u: 0.01, mean_lat: 23.8, mean_lon: 90.2, mean_h: -36, point_id: null, reason: null } }));
    expect(await screen.findByText(/12 of 30/)).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40");
    expect(screen.getByRole("button", { name: /collect point/i })).toBeDisabled();
    expect(screen.getByRole("link", { name: /csv/i })).toHaveAttribute("href", "/api/rover/points/export?fmt=csv");
  });

  it("sends the epoch count and the fixed-only switch as typed", async () => {
    renderSurvey();
    await screen.findByText("BM-1");
    await userEvent.type(screen.getByLabelText(/point name/i), "FENCE-7");
    await userEvent.type(screen.getByLabelText(/^code$/i), "FENCE");
    const epochs = screen.getByLabelText(/^epochs$/i);
    await userEvent.clear(epochs);
    await userEvent.type(epochs, "10");
    await userEvent.click(screen.getByRole("switch", { name: /rtk fixed epochs only/i }));
    await userEvent.click(screen.getByRole("button", { name: /collect point/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/rover/collect") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toEqual({ name: "FENCE-7", code: "FENCE", note: null, epochs: 10, fixed_only: false });
  });

  it("starts the form from POINT_EPOCHS and POINT_FIXED_ONLY", async () => {
    overview = { session: openSession, collect_defaults: { epochs: 120, fixed_only: false } };
    renderSurvey();
    await screen.findByText("BM-1");
    await waitFor(() => expect(screen.getByLabelText(/^epochs$/i)).toHaveValue("120"));
    expect(screen.getByRole("switch", { name: /rtk fixed epochs only/i })).not.toBeChecked();
    await userEvent.type(screen.getByLabelText(/point name/i), "BM-3");
    await userEvent.click(screen.getByRole("button", { name: /collect point/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/rover/collect") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toMatchObject({ name: "BM-3", epochs: 120, fixed_only: false });
  });

  it("shows a session another client opened, from the polled rover overview", async () => {
    sessions = [{ ...openSession, end_utc: "2026-09-18T16:30:00+00:00" }]; // the list this page fetched
    overview = { session: { ...openSession, id: 2, name: "opened-elsewhere" }, collect_defaults: { epochs: 30, fixed_only: true } };
    renderSurvey();
    expect(await within(screen.getByRole("region", { name: "Session" })).findByText(/opened-elsewhere/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^start$/i })).not.toBeInTheDocument();
  });

  it("cancels a collection and shows why it stopped", async () => {
    useLive.setState({ collect: { state: "collecting", name: "BM-2", target: 30, accepted: 3, skipped: 0, sd_n: null, sd_e: null, sd_u: null, mean_lat: null, mean_lon: null, mean_h: null, point_id: null, reason: null } });
    renderSurvey();
    await userEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
    expect(calls.some(([u, i]) => u.endsWith("/api/rover/collect") && i?.method === "DELETE")).toBe(true);
    act(() => useLive.setState({ collect: { ...useLive.getState().collect!, state: "aborted", reason: "no RTK fixed epochs for 150 epochs" } }));
    expect(await screen.findByText(/no RTK fixed epochs/)).toBeInTheDocument();
  });

  it("renames a point and offers every export format, scoped to the chosen session", async () => {
    renderSurvey();
    await screen.findByText("BM-1");
    await userEvent.click(screen.getByRole("button", { name: /edit bm-1/i }));
    const name = screen.getByLabelText(/name of point 1/i);
    await userEvent.clear(name);
    await userEvent.type(name, "BM-1A");
    await userEvent.click(screen.getByRole("button", { name: /^save$/i }));
    const patch = calls.find(([, i]) => i?.method === "PATCH")!;
    expect(patch[0]).toBe("/api/rover/points/1");
    expect(JSON.parse(patch[1]!.body as string)).toEqual({ name: "BM-1A", code: "BM", note: "brass" });

    await userEvent.selectOptions(screen.getByLabelText(/session filter/i), "1");
    for (const fmt of ["csv", "geojson", "kml", "gpx"]) {
      expect(screen.getByRole("link", { name: new RegExp(`^${fmt}$`, "i") })).toHaveAttribute("href", `/api/rover/points/export?fmt=${fmt}&session_id=1`);
    }
    expect(calls.some(([u]) => u === "/api/rover/points?session_id=1")).toBe(true);
  });

  it("puts every point on the map", async () => {
    renderSurvey();
    await screen.findByText("BM-1");
    await screen.findByTestId("map-frame");
    await waitFor(() => expect(markers.some((m) => m.el.dataset.marker === "point" && m.el.title === "BM-1")).toBe(true));
  });

  it("deletes a point only once the dialog is confirmed, then refreshes the list", async () => {
    renderSurvey();
    await screen.findByText("BM-1");
    const deletes = () => calls.filter(([, i]) => i?.method === "DELETE");
    await userEvent.click(screen.getByRole("button", { name: /delete bm-1/i }));
    let dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Delete BM-1?");
    await userEvent.click(within(dialog).getByRole("button", { name: /^cancel$/i }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(deletes()).toEqual([]);

    const before = pointsGets();
    await userEvent.click(screen.getByRole("button", { name: /delete bm-1/i }));
    dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(deletes().map(([u]) => u)).toEqual(["/api/rover/points/1"]);
    await waitFor(() => expect(pointsGets()).toBeGreaterThan(before));
  });

  it("stops the open session", async () => {
    renderSurvey();
    const region = await screen.findByRole("region", { name: "Session" });
    await userEvent.click(await within(region).findByRole("button", { name: /stop session/i }));
    await waitFor(() => expect(calls.some(([u, i]) => u === "/api/rover/sessions/stop" && i?.method === "POST")).toBe(true));
  });

  it("refuses an epoch count outside 1–3600 before posting", async () => {
    renderSurvey();
    await screen.findByText("BM-1");
    await userEvent.type(screen.getByLabelText(/point name/i), "BM-3");
    const epochs = screen.getByLabelText(/^epochs$/i);
    const collect = screen.getByRole("button", { name: /collect point/i });
    for (const bad of ["0", "3601", "2.5"]) {
      await userEvent.clear(epochs);
      await userEvent.type(epochs, bad);
      expect(epochs).toHaveAttribute("aria-invalid", "true");
      expect(collect).toBeDisabled();
      await userEvent.type(epochs, "{Enter}");
    }
    expect(screen.getByText("A whole number from 1 to 3600.")).toBeVisible();
    expect(calls.some(([u, i]) => u.endsWith("/api/rover/collect") && i?.method === "POST")).toBe(false);
    for (const good of ["1", "3600"]) {
      await userEvent.clear(epochs);
      await userEvent.type(epochs, good);
      expect(epochs).toHaveAttribute("aria-invalid", "false");
      expect(collect).toBeEnabled();
    }
  });

  it("names the saved point once a collection is done", async () => {
    useLive.setState({ collect: { state: "done", name: "BM-2", target: 30, accepted: 30, skipped: 0, sd_n: 0.004, sd_e: 0.003, sd_u: 0.01, mean_lat: 23.8, mean_lon: 90.2, mean_h: -36, point_id: 5, reason: null } });
    renderSurvey();
    expect(await screen.findByText("Saved as point 5.")).toBeInTheDocument();
  });

  it("shows the collection the server just started, without waiting for the first progress update", async () => {
    // a finished collection is still on screen from before
    useLive.setState({ collect: { state: "done", name: "OLD", target: 10, accepted: 10, skipped: 0, sd_n: null, sd_e: null, sd_u: null, mean_lat: null, mean_lon: null, mean_h: null, point_id: 4, reason: null } });
    renderSurvey();
    await screen.findByText("BM-1");
    expect(screen.getByText("Saved as point 4.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/point name/i), "BM-2");
    await userEvent.click(screen.getByRole("button", { name: /collect point/i }));
    // the POST's answer replaces the stale banner and holds the form until the collection ends
    expect(await screen.findByText(/0 of 30 epochs/)).toBeInTheDocument();
    expect(screen.queryByText(/saved as point 4/i)).toBeNull();
    expect(screen.getByRole("button", { name: /collect point/i })).toBeDisabled();
    expect(useLive.getState().collect?.state).toBe("collecting");
  });

  it("keeps a collection that ended before the POST answered", async () => {
    // One epoch, so the collection is done on the next one: the WebSocket's "done" can land
    // before the HTTP answer to the POST that started it.
    let release: (r: Response) => void = () => {};
    const answer = new Promise<Response>((resolve) => (release = resolve));
    const collected = vi.spyOn(toast, "success");
    const base = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      if (String(url).endsWith("/api/rover/collect") && init?.method === "POST") {
        calls.push([String(url), init]);
        return answer;
      }
      return base(url, init);
    }) as typeof fetch;
    renderSurvey();
    await screen.findByText("BM-1");
    await userEvent.type(screen.getByLabelText(/point name/i), "BM-3");
    await userEvent.click(screen.getByRole("button", { name: /collect point/i }));
    act(() => useLive.setState({ collect: { ...IDLE, state: "done", name: "BM-3", target: 1, accepted: 1, point_id: 9 } }));
    expect(await screen.findByText("Saved as point 9.")).toBeInTheDocument();
    await act(async () => release(new Response(JSON.stringify({ ...IDLE, state: "collecting", name: "BM-3", target: 1 }), { status: 200 })));
    // Wait for the mutation's onSuccess itself (its toast), not for anything sent before it ran.
    await waitFor(() => expect(collected).toHaveBeenCalledWith("Collecting BM-3"));
    expect(screen.getByText("Saved as point 9.")).toBeInTheDocument(); // not "0 of 1 epochs"
    expect(useLive.getState().collect?.state).toBe("done");
    collected.mockRestore();
  });

  it("shows the server's answer to a cancel at once", async () => {
    useLive.setState({ collect: { state: "collecting", name: "BM-2", target: 30, accepted: 3, skipped: 0, sd_n: null, sd_e: null, sd_u: null, mean_lat: null, mean_lon: null, mean_h: null, point_id: null, reason: null } });
    renderSurvey();
    await userEvent.click(await screen.findByRole("button", { name: /^cancel$/i }));
    expect(await screen.findByText(/Stopped: cancelled/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).toBeNull(); // no longer collecting
  });

  it("treats a points answer that is not a list as no points", async () => {
    pointsBody = {};
    renderSurvey();
    expect(await screen.findByText("No points yet.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Points (0)" })).toBeInTheDocument();
  });

  it("on a base explains itself and never asks the rover API", async () => {
    useLive.setState({ role: "base" });
    renderSurvey();
    expect(screen.getByText(/runs as a base station/i)).toBeInTheDocument();
    await act(async () => {});
    expect(calls.filter(([u]) => u.startsWith("/api/rover"))).toEqual([]);
  });

  it("before the role is known waits, and never asks the rover API", async () => {
    useLive.setState({ role: null });
    renderSurvey();
    expect(screen.getByText(/waiting for the receiver/i)).toBeInTheDocument();
    await act(async () => {});
    expect(calls.filter(([u]) => u.startsWith("/api/rover"))).toEqual([]);
  });

  it("starts a session when none is open", async () => {
    sessions = [{ ...openSession, end_utc: "2026-09-18T17:00:00+00:00" }];
    renderSurvey();
    const region = await screen.findByRole("region", { name: "Session" });
    await userEvent.type(within(region).getByLabelText(/session name/i), "site-2");
    await userEvent.click(within(region).getByRole("button", { name: /^start$/i }));
    const post = calls.find(([u, i]) => u.endsWith("/api/rover/sessions") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toEqual({ name: "site-2" });
  });
});
