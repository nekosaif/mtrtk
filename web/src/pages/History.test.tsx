import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetPrefsForTests } from "@/lib/prefs";
import type { HistoryMetrics, HistoryResponse } from "@/lib/types";
import History, { HISTORY_METRICS } from "./History";

// The daemon's catalogue, minus `temp_c` (a host without a thermal sensor never samples it here).
const catalogue: HistoryMetrics = {
  "1s": ["lat", "lon", "height_m", "hmsl_m", "h_acc_m", "v_acc_m", "fix_type", "carr_soln", "nsat_used", "nsat_tracked", "pdop", "hdop", "vdop", "cno_mean", "jam_ind", "agc_cnt", "noise_per_ms", "corr_age_s", "baseline_m", "rtcm_bytes_per_s", "ntrip_clients", "cpu_pct", "mem_pct", "disk_free_gb"],
  "1m": ["h_acc_avg", "h_acc_max", "nsat_used_avg", "cno_mean_avg", "cpu_pct_avg"],
};

const T0 = Date.parse("2026-09-18T00:00:00Z") / 1000;
const answer = (over: Partial<HistoryResponse> = {}): HistoryResponse => ({
  res: "1m",
  columns: ["ts", "h_acc_m", "nsat_used"],
  rows: Array.from({ length: 30 }, (_, i) => [T0 + i * 60, i === 10 ? null : 0.01 + i * 0.001, 20 + (i % 3)]),
  ...over,
});

interface Answers {
  metrics?: HistoryMetrics;
  history?: HistoryResponse | { status: number; detail: string };
}

let calls: string[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    calls.push(p);
    if ((init?.method ?? "GET") !== "GET") return json({ detail: "not found" }, 404);
    if (p.endsWith("/api/history/metrics")) return json(a.metrics ?? catalogue);
    if (p.includes("/api/history?")) {
      const h = a.history ?? answer();
      return "status" in h && "detail" in h ? json({ detail: h.detail }, h.status) : json(h);
    }
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <History />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const historyCalls = () => calls.filter((u) => u.includes("/api/history?")).map((u) => new URL(u, "http://x").searchParams);

describe("History page", () => {
  beforeEach(() => {
    resetPrefsForTests();
    localStorage.clear();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-18T12:00:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("offers only the metrics the daemon lists, grouped, and keeps the catalogue's labels", async () => {
    mockFetch();
    renderPage();
    const metrics = await waitFor(() => screen.getByRole("region", { name: /^metrics$/i }));
    await within(metrics).findByRole("checkbox", { name: /horizontal accuracy/i });
    expect(within(metrics).queryByRole("checkbox", { name: /temperature/i })).toBeNull();
    for (const g of ["Position", "Satellites", "RF", "Corrections", "System"]) expect(within(metrics).getByText(g)).toBeInTheDocument();
    // one checkbox per catalogue entry the API knows, no more
    const offered = HISTORY_METRICS.filter((m) => catalogue["1s"].includes(m.key));
    expect(within(metrics).getAllByRole("checkbox")).toHaveLength(offered.length);
    expect(within(metrics).getByRole("checkbox", { name: /satellites used/i })).toBeInTheDocument();
  });

  it("asks with res=auto for the chosen range, keys each series on the requested name and shows the resolution", async () => {
    mockFetch();
    renderPage();
    expect(await screen.findByRole("img", { name: /horizontal accuracy over time/i })).toBeInTheDocument();
    const q = historyCalls()[0];
    expect(q.get("metrics")!.split(",")).toEqual(expect.arrayContaining(["h_acc_m", "nsat_used", "cno_mean"]));
    expect(q.get("res")).toBe("auto");
    expect(q.get("to")).toBe("2026-09-18T12:00:00.000Z");
    expect(q.get("from")).toBe("2026-09-17T12:00:00.000Z");
    expect(screen.getByText(/30 samples · 1 min rollups/i)).toBeInTheDocument();
    // the answer carried no cno_mean column: that chart says so rather than borrowing another series
    expect(screen.getByText(/mean c\/n0: no data in this range/i)).toBeInTheDocument();
    const acc = screen.getByRole("img", { name: /horizontal accuracy over time/i });
    expect(acc.querySelector("path[data-series]")!.getAttribute("d")!.match(/M/g)).toHaveLength(2); // the null at row 10
    // never `h_acc_avg`
    expect(q.get("metrics")).not.toMatch(/_avg/);
  });

  it("switches range with the chips, marks the active one and shows 1 s samples when the daemon picks them", async () => {
    mockFetch({ history: answer({ res: "1s" }) });
    renderPage();
    await screen.findByRole("img", { name: /horizontal accuracy over time/i });
    const chip24 = screen.getByRole("button", { name: /^24 h$/ });
    expect(chip24).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: /^1 h$/ }));
    await waitFor(() => expect(historyCalls().length).toBeGreaterThan(1));
    const q = historyCalls().at(-1)!;
    expect(q.get("from")).toBe("2026-09-18T11:00:00.000Z");
    expect(screen.getByRole("button", { name: /^1 h$/ })).toHaveAttribute("aria-pressed", "true");
    expect(chip24).toHaveAttribute("aria-pressed", "false");
    expect(await screen.findByText(/1 s samples/i)).toBeInTheDocument();
  });

  it("shows the 422 window-cap detail verbatim", async () => {
    const detail = "that range is 1 day, 0:00:00; at res=1s ask for at most 24 hours per request (res=1m covers up to 90 days)";
    mockFetch({ history: { status: 422, detail } });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });

  it("toggles a metric, persists the choice and drops a selected metric the daemon does not know", async () => {
    localStorage.setItem("mtrtk:historyMetrics", JSON.stringify(["h_acc_m", "temp_c"]));
    mockFetch({ history: answer({ columns: ["ts", "h_acc_m"], rows: [[T0, 0.01], [T0 + 60, 0.02]] }) });
    renderPage();
    await screen.findByRole("img", { name: /horizontal accuracy over time/i });
    expect(historyCalls()[0].get("metrics")).toBe("h_acc_m");
    const metrics = screen.getByRole("region", { name: /^metrics$/i });
    await userEvent.click(within(metrics).getByRole("checkbox", { name: /pdop/i }));
    await waitFor(() => expect(historyCalls().at(-1)!.get("metrics")).toBe("h_acc_m,pdop"));
    expect(JSON.parse(localStorage.getItem("mtrtk:historyMetrics")!)).toEqual(["h_acc_m", "temp_c", "pdop"]);
  });

  it("asks for nothing and says so when no metric is selected", async () => {
    localStorage.setItem("mtrtk:historyMetrics", JSON.stringify([]));
    mockFetch();
    renderPage();
    expect(await screen.findByText(/pick a metric/i)).toBeInTheDocument();
    expect(historyCalls()).toHaveLength(0);
  });
});
