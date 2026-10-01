import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { sampleState } from "@/test/fixtures";
import { markers, resetMaplibreMock } from "@/test/maplibreMock";
import Survey from "./Survey";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

const points = [{ id: 1, session_id: 1, name: "BM-1", code: "BM", note: "brass", ts_utc: "2026-09-18T16:00:00+00:00", lat: 23.8373506, lon: 90.2625502, height_m: -36.268, hmsl_m: 13.363, n_epochs: 30, sd_n: 0.004, sd_e: 0.003, sd_u: 0.009, fix_type: 3, carr_soln: 2, h_acc_m: 0.012, v_acc_m: 0.018 }];
const openSession = { id: 1, name: "field-1", start_utc: "2026-09-18T15:00:00+00:00", end_utc: null, role: "rover", notes: null };
let calls: [string, RequestInit | undefined][] = [];
let sessions: unknown[] = [openSession];

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
    useLive.setState({ state: sampleState(), role: "rover", status: "open", lastEpochAt: Date.now(), collect: null });
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      calls.push([String(url), init]);
      const u = String(url);
      if (u.endsWith("/api/rover/sessions") && !init?.method) return new Response(JSON.stringify(sessions), { status: 200 });
      if (u.includes("/api/rover/points") && !init?.method) return new Response(JSON.stringify(points), { status: 200 });
      if (u.endsWith("/api/rover/collect") && init?.method === "POST") return new Response(JSON.stringify({ state: "collecting", name: "BM-2", target: 30, accepted: 0, skipped: 0 }), { status: 200 });
      if (u.endsWith("/api/rover/collect") && init?.method === "DELETE") return new Response(JSON.stringify({ state: "aborted", reason: "cancelled" }), { status: 200 });
      if (u.endsWith("/api/rover/collect") && !init?.method) return new Response(JSON.stringify({ state: "idle", name: null, target: 0, accepted: 0, skipped: 0 }), { status: 200 });
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
    expect(markers.some((m) => m.el.dataset.marker === "point" && m.el.title === "BM-1")).toBe(true);
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
