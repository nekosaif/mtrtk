import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { resetPrefsForTests } from "@/lib/prefs";
import type { HourSlot, Job, JobFile, LogFile, LogsResponse } from "@/lib/types";
import Logs from "./Logs";

// ---- fixtures -----------------------------------------------------------------------------

const files: LogFile[] = [
  { name: "MTRK_20260918_10.ubx", hour_utc: "2026-09-18T10:00:00+00:00", bytes: 14_000_000, complete: true, keep: false, open: false, msg_counts: { "RXM-RAWX": 3600 }, start_utc: null, end_utc: null },
  { name: "MTRK_20260918_11.ubx", hour_utc: "2026-09-18T11:00:00+00:00", bytes: 7_000_000, complete: false, keep: true, open: false, msg_counts: { "RXM-RAWX": 1800 }, start_utc: null, end_utc: null },
];
const listing = (over: Partial<LogsResponse> = {}): LogsResponse => ({ files, total_bytes: 21_000_000, hours: 2, disk_free_gb: 42.5, min_free_gb: 2, ...over });
const slots: HourSlot[] = [
  { hour_utc: "2026-09-18T10:00:00+00:00", available: true, bytes: 1, complete: true },
  { hour_utc: "2026-09-18T11:00:00+00:00", available: true, bytes: 1, complete: false },
  { hour_utc: "2026-09-18T12:00:00+00:00", available: false, bytes: 0, complete: false },
];
const job = (over: Partial<Job> = {}): Job => ({
  id: "j1",
  kind: "rinex_export",
  status: "done",
  created_utc: "2026-09-18T11:30:00+00:00",
  updated_utc: "2026-09-18T11:31:00+00:00",
  progress: 1,
  message: "3 files written",
  params: {},
  result: null,
  error: null,
  ...over,
});

let calls: [string, RequestInit | undefined][] = [];

describe("Logs page", () => {
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const p = String(url);
      calls.push([p, init]);
      if (p.endsWith("/api/logs")) return new Response(JSON.stringify({ files, total_bytes: 21_000_000, hours: 2, disk_free_gb: 42.5 }), { status: 200 });
      if (p.includes("/api/logs/availability")) return new Response(JSON.stringify([{ hour_utc: "2026-09-18T10:00:00+00:00", available: true, bytes: 1, complete: true }, { hour_utc: "2026-09-18T11:00:00+00:00", available: true, bytes: 1, complete: false }, { hour_utc: "2026-09-18T12:00:00+00:00", available: false, bytes: 0, complete: false }]), { status: 200 });
      if (init?.method === "PATCH") return new Response(JSON.stringify({ ...files[0], keep: true }), { status: 200 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
  });

  it("shows files, disk summary and toggles keep", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><MemoryRouter><Logs /></MemoryRouter></QueryClientProvider>);
    expect(await screen.findByText("MTRK_20260918_10.ubx")).toBeInTheDocument();
    expect(screen.getByText(/42\.5 GB free/)).toBeInTheDocument();
    // fmtBytes is decimal (1 MB = 1e6 B, the API's own convention), so 21 000 000 B reads 21.0 MB
    expect(screen.getByText(/21\.0 MB in 2 hours/)).toBeInTheDocument();
    const row = screen.getByText("MTRK_20260918_10.ubx").closest("tr")!;
    await userEvent.click(within(row).getByRole("switch", { name: /keep/i }));
    const patch = calls.find(([, i]) => i?.method === "PATCH")!;
    expect(patch[0]).toMatch(/\/api\/logs\/MTRK_20260918_10\.ubx$/);
    expect(JSON.parse(patch[1]!.body as string)).toEqual({ keep: true });
    expect(within(row).getByRole("link", { name: /download/i })).toHaveAttribute("href", "/api/logs/MTRK_20260918_10.ubx");
  });
});

// ---- the page against the real shapes -----------------------------------------------------

interface Answers {
  logs?: LogsResponse;
  slots?: HourSlot[];
  jobs?: Job[];
  jobFiles?: JobFile[];
  /** Status + detail for PATCH; default 200 echoing the file with the new flag. */
  keep?: { status: number; detail: string };
  /** Status + detail for DELETE; default 200. */
  remove?: { status: number; detail: string };
  /** Status + detail for GET /api/logs/window; default 200 with an empty body. */
  window?: { status: number; detail: string };
  deleteJob?: { status: number; detail: string };
}

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    calls.push([p, init]);
    const m = init?.method ?? "GET";
    if (m === "GET") {
      if (p.endsWith("/api/logs")) return json(a.logs ?? listing());
      if (p.includes("/api/logs/availability")) return json(a.slots ?? slots);
      if (p.includes("/api/logs/window")) return a.window ? json({ detail: a.window.detail }, a.window.status) : new Response(new Uint8Array([0xb5, 0x62]), { status: 200 });
      if (p.includes("/api/jobs?")) return json(a.jobs ?? []);
      if (/\/api\/jobs\/[^/]+\/files$/.test(p)) return json(a.jobFiles ?? []);
      return json({ detail: "not found" }, 404);
    }
    if (m === "PATCH" && p.includes("/api/logs/")) {
      if (a.keep) return json({ detail: a.keep.detail }, a.keep.status);
      const name = decodeURIComponent(p.split("/").at(-1)!);
      const body = JSON.parse(String(init!.body)) as { keep: boolean };
      return json({ ...(a.logs ?? listing()).files.find((f) => f.name === name)!, keep: body.keep });
    }
    if (m === "DELETE" && p.includes("/api/logs/")) return a.remove ? json({ detail: a.remove.detail }, a.remove.status) : json({ ok: true });
    if (m === "DELETE" && p.includes("/api/jobs/")) return a.deleteJob ? json({ detail: a.deleteJob.detail }, a.deleteJob.status) : json({ ok: true });
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
}

const callsTo = (method: string, re: RegExp) => calls.filter(([u, i]) => (i?.method ?? "GET") === method && re.test(u));

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Logs />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const region = (name: string | RegExp) => screen.getByRole("region", { name });
const rowOf = (text: string) => screen.getByText(text).closest("tr")!;

describe("Logs page — files", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    localStorage.clear();
  });

  it("marks the hour being written from `open`, lets it download and refuses to delete it with the reason", async () => {
    mockFetch({ logs: listing({ files: [files[0], { ...files[1], keep: false, open: true }] }) });
    renderPage();
    const row = await waitFor(() => rowOf("MTRK_20260918_11.ubx"));
    expect(row).toHaveTextContent(/writing/i);
    expect(within(row).getByRole("link", { name: /download/i })).toHaveAttribute("href", "/api/logs/MTRK_20260918_11.ubx");
    const del = within(row).getByRole("button", { name: /delete/i });
    expect(del).toBeDisabled();
    expect(del.closest("[title]")).toHaveAttribute("title", expect.stringMatching(/being written/i));
    // the closed hour deletes normally, with a confirmation
    const other = rowOf("MTRK_20260918_10.ubx");
    expect(other).toHaveTextContent(/complete/i);
    await userEvent.click(within(other).getByRole("button", { name: /delete/i }));
    const dialog = screen.getByRole("dialog", { name: /delete MTRK_20260918_10\.ubx/i });
    await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(callsTo("DELETE", /\/api\/logs\/MTRK_20260918_10\.ubx$/)).toHaveLength(1));
    expect(callsTo("DELETE", /force=1/)).toHaveLength(0);
  });

  it("disables delete on a keep-protected hour with the reason and shows a partial state", async () => {
    mockFetch();
    renderPage();
    const row = await waitFor(() => rowOf("MTRK_20260918_11.ubx"));
    expect(row).toHaveTextContent(/partial/i);
    const del = within(row).getByRole("button", { name: /delete/i });
    expect(del).toBeDisabled();
    expect(del.closest("[title]")).toHaveAttribute("title", expect.stringMatching(/keep/i));
    expect(within(row).getByRole("switch", { name: /keep/i })).toHaveAttribute("aria-checked", "true");
  });

  it("shows a keep 409 verbatim and leaves the switch where the server says", async () => {
    const detail = "could not write a sidecar for MTRK_20260918_10.ubx: [Errno 30] Read-only file system";
    mockFetch({ keep: { status: 409, detail } });
    renderPage();
    const row = await waitFor(() => rowOf("MTRK_20260918_10.ubx"));
    await userEvent.click(within(row).getByRole("switch", { name: /keep/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(within(row).getByRole("switch", { name: /keep/i })).toHaveAttribute("aria-checked", "false");
  });

  it("shows a delete 409 verbatim inside the dialog", async () => {
    const detail = 'this file is marked keep; clear the mark with PATCH {"keep": false} before deleting it';
    mockFetch({ remove: { status: 409, detail } });
    renderPage();
    const row = await waitFor(() => rowOf("MTRK_20260918_10.ubx"));
    await userEvent.click(within(row).getByRole("button", { name: /delete/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(detail);
  });

  it("offers force only for the newest hour when the daemon reports no open hour", async () => {
    mockFetch();
    renderPage();
    const newest = await waitFor(() => rowOf("MTRK_20260918_11.ubx"));
    // the newest hour here is keep-protected, so delete is held; clear keep first via a listing without it
    expect(within(newest).getByRole("button", { name: /delete/i })).toBeDisabled();
    mockFetch({ logs: listing({ files: [files[0], { ...files[1], keep: false }] }) });
    cleanupAndRender();
    const row = await waitFor(() => rowOf("MTRK_20260918_11.ubx"));
    await userEvent.click(within(row).getByRole("button", { name: /delete/i }));
    const dialog = screen.getByRole("dialog");
    const force = within(dialog).getByRole("checkbox", { name: /newest hour/i });
    await userEvent.click(force);
    await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(callsTo("DELETE", /MTRK_20260918_11\.ubx\?force=1$/)).toHaveLength(1));
  });

  it("says when there are no raw logs yet", async () => {
    mockFetch({ logs: listing({ files: [], total_bytes: 0, hours: 0 }), slots: [] });
    renderPage();
    expect(await screen.findByText(/no raw logs yet/i)).toBeInTheDocument();
  });
});

function cleanupAndRender() {
  // a second render in the same test: unmount the first tree so the roles are unambiguous
  cleanup();
  renderPage();
}

describe("Logs page — disk", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
  });

  it("stays quiet while free space is above the retention floor", async () => {
    mockFetch({ logs: listing({ disk_free_gb: 42.5, min_free_gb: 2 }) });
    renderPage();
    await screen.findByText(/42\.5 GB free/);
    expect(screen.queryByText(/disk (low|warning)/i)).toBeNull();
  });

  it("warns (serious) just under the floor and escalates (critical) under half of it", async () => {
    mockFetch({ logs: listing({ disk_free_gb: 1.6, min_free_gb: 2 }) });
    renderPage();
    const warn = await screen.findByText(/disk warning/i);
    expect(warn.closest("[data-level]")).toHaveAttribute("data-level", "serious");
    expect(screen.getByText(/1\.6 GB free/)).toBeInTheDocument();
    expect(screen.getByText(/retention prunes below 2\.0 GB/i)).toBeInTheDocument();

    mockFetch({ logs: listing({ disk_free_gb: 0.7, min_free_gb: 2 }) });
    cleanupAndRender();
    const low = await screen.findByText(/disk low/i);
    expect(low.closest("[data-level]")).toHaveAttribute("data-level", "critical");
  });
});

describe("Logs page — window download and availability", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-18T12:20:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("asks for the last 48 whole hours and draws one cell per hour with the state in its name", async () => {
    mockFetch();
    renderPage();
    const strip = await screen.findByRole("group", { name: /hourly raw log availability/i });
    const req = callsTo("GET", /\/api\/logs\/availability/)[0][0];
    const q = new URL(req, "http://x").searchParams;
    expect(q.get("from")).toBe("2026-09-16T13:00:00.000Z");
    expect(q.get("to")).toBe("2026-09-18T13:00:00.000Z");
    const cells = within(strip).getAllByRole("button");
    expect(cells).toHaveLength(3);
    expect(cells[0]).toHaveAccessibleName(/10:00 UTC · complete/);
    expect(cells[0]).toHaveAttribute("data-state", "complete");
    expect(cells[1]).toHaveAttribute("data-state", "partial");
    expect(cells[2]).toHaveAttribute("data-state", "missing");
    expect(within(region(/last 48 hours/i)).getByText(/brass = complete/i)).toBeInTheDocument();
  });

  it("fills the window form from a clicked hour", async () => {
    mockFetch();
    renderPage();
    const strip = await screen.findByRole("group", { name: /hourly raw log availability/i });
    await userEvent.click(within(strip).getByRole("button", { name: /11:00 UTC · partial/ }));
    expect(screen.getByLabelText(/from \(utc\)/i)).toHaveValue("2026-09-18T11:00");
    expect(screen.getByLabelText(/to \(utc\)/i)).toHaveValue("2026-09-18T12:00");
    // the selection is a bar under the hour, not a change to the cell itself
    const cells = within(strip).getAllByRole("button");
    expect(cells.map((c) => c.getAttribute("aria-pressed"))).toEqual(["false", "true", "false"]);
    expect(cells[1].className).not.toMatch(/ring/);
    expect(strip.parentElement!.querySelector("[data-selection]")).toHaveStyle({ gridColumn: "2 / 3" });
    const win = region(/download a raw window/i);
    expect(within(win).getByRole("button", { name: /download \.ubx/i })).toBeEnabled();
  });

  it("defaults to the last 24 hours and refuses a window over 48 hours client-side", async () => {
    mockFetch();
    renderPage();
    const from = await screen.findByLabelText(/from \(utc\)/i);
    const to = screen.getByLabelText(/to \(utc\)/i);
    expect(from).toHaveValue("2026-09-17T12:20");
    expect(to).toHaveValue("2026-09-18T12:20");
    fireEvent.change(from, { target: { value: "2026-09-16T00:00" } });
    const win = region(/download a raw window/i);
    expect(within(win).getByRole("button", { name: /download \.ubx/i })).toBeDisabled();
    expect(within(win).getByText(/at most 48 hours/i)).toBeInTheDocument();
    fireEvent.change(from, { target: { value: "2026-09-18T13:00" } });
    expect(within(win).getByRole("button", { name: /download \.ubx/i })).toBeDisabled();
    expect(within(win).getByText(/before/i)).toBeInTheDocument();
    expect(callsTo("GET", /\/api\/logs\/window/)).toHaveLength(0);
  });

  it("checks the window with the daemon first and shows a 422 detail verbatim", async () => {
    const detail = "that range is 2 days, 1:00:00; ask for at most 48 hours per request";
    mockFetch({ window: { status: 422, detail } });
    renderPage();
    await screen.findByLabelText(/from \(utc\)/i);
    const win = region(/download a raw window/i);
    await userEvent.click(within(win).getByRole("button", { name: /download \.ubx/i }));
    expect(await within(win).findByRole("alert")).toHaveTextContent(detail);
    const req = callsTo("GET", /\/api\/logs\/window/)[0][0];
    const q = new URL(req, "http://x").searchParams;
    expect(q.get("from")).toBe("2026-09-17T12:20:00Z");
    expect(q.get("to")).toBe("2026-09-18T12:20:00Z");
  });
});

describe("Logs page — jobs", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-18T12:20:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("says exports arrive in Phase 5 when there are none", async () => {
    mockFetch({ jobs: [] });
    renderPage();
    const jobs = await waitFor(() => region(/jobs/i));
    expect(await within(jobs).findByText(/no jobs yet/i)).toBeInTheDocument();
    expect(within(jobs).getByText(/phase 5/i)).toBeInTheDocument();
  });

  it("lists jobs with a worded status, progress, kind, message, age, result files and a guarded delete", async () => {
    mockFetch({
      jobs: [job({ id: "run", status: "running", progress: 0.42, message: "converting hour 3 of 7", created_utc: "2026-09-18T12:10:00+00:00" }), job({ id: "done", status: "done" }), job({ id: "bad", status: "failed", message: null, error: "RuntimeError: convbin exited 1", progress: 0.1 })],
      jobFiles: [{ name: "MTRK_20260918.obs", bytes: 12_345_678 }],
    });
    renderPage();
    const jobs = await waitFor(() => region(/jobs/i));
    const running = await within(jobs).findByText(/converting hour 3 of 7/);
    const runRow = running.closest("li")!;
    expect(within(runRow).getByText(/^running$/i).closest("[data-level]")).toHaveAttribute("data-level", "warning");
    expect(within(runRow).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "42");
    expect(runRow).toHaveTextContent(/rinex export/i);
    expect(runRow).toHaveTextContent(/10 min ago/);
    expect(within(runRow).getByRole("button", { name: /delete/i })).toBeDisabled();

    const doneRow = within(jobs).getByText(/3 files written/).closest("li")!;
    expect(within(doneRow).getByText(/^done$/i).closest("[data-level]")).toHaveAttribute("data-level", "good");
    const file = await within(doneRow).findByRole("link", { name: /MTRK_20260918\.obs/ });
    expect(file).toHaveAttribute("href", "/api/jobs/done/files/MTRK_20260918.obs");
    expect(doneRow).toHaveTextContent(/12\.3 MB/);

    const badRow = within(jobs).getByText(/convbin exited 1/).closest("li")!;
    expect(within(badRow).getByText(/^failed$/i).closest("[data-level]")).toHaveAttribute("data-level", "critical");

    await userEvent.click(within(badRow).getByRole("button", { name: /delete/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(callsTo("DELETE", /\/api\/jobs\/bad$/)).toHaveLength(1));
  });

  it("takes a live jobs.update over the listing", async () => {
    mockFetch({ jobs: [job({ id: "run", status: "running", progress: 0.1, message: "starting" })] });
    renderPage();
    const jobs = await waitFor(() => region(/jobs/i));
    await within(jobs).findByText(/starting/);
    act(() => {
      useLive.setState({ jobs: { run: job({ id: "run", status: "running", progress: 0.8, message: "hour 6 of 7" }) } });
    });
    expect(within(jobs).getByText(/hour 6 of 7/)).toBeInTheDocument();
    expect(within(jobs).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "80");
  });
});
