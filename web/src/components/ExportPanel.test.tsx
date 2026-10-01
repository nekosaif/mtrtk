import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Job, Preset } from "@/lib/types";
import { ExportPanel } from "./ExportPanel";

const presets: Preset[] = [
  { id: "csrs-ppp", name: "CSRS-PPP (NRCan)", service_url: "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php", description: "Free global PPP.", version: "3.04", interval_s: 30, exclude_systems: [], hatanaka: true, gzip: true, constraints: ["24 h of data recommended (a few hours minimum)", "Choose 'Static' and ITRF outside Canada"], adjustable: false },
  { id: "opus", name: "OPUS (NGS, USA)", service_url: "https://geodesy.noaa.gov/OPUS/", description: "US only.", version: "2.11", interval_s: 30, exclude_systems: ["R", "E", "J", "C", "S", "I"], hatanaka: false, gzip: false, constraints: ["Only for sites in the USA"], adjustable: false },
  { id: "generic", name: "Generic RINEX 3.04", service_url: "", description: "Full-rate.", version: "3.04", interval_s: null, exclude_systems: [], hatanaka: false, gzip: false, constraints: ["All constellations, native interval"], adjustable: true },
];
const job: Job = { id: "job1", kind: "export", status: "queued", created_utc: "2026-09-18T12:00:00+00:00", updated_utc: null, progress: 0, message: null, params: {}, result: null, error: null };

let calls: [string, RequestInit | undefined][] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(submit?: { status: number; detail: unknown }, presetsAnswer?: { status: number; detail: unknown }) {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    calls.push([String(url), init]);
    if (String(url).endsWith("/api/export/presets")) return presetsAnswer ? json({ detail: presetsAnswer.detail }, presetsAnswer.status) : json(presets);
    if (init?.method === "POST" && String(url).endsWith("/api/export")) return submit ? json({ detail: submit.detail }, submit.status) : json(job);
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
}

const posts = () => calls.filter(([, i]) => i?.method === "POST");
const bodyOf = (call: [string, RequestInit | undefined]) => JSON.parse(String(call[1]!.body)) as Record<string, unknown>;

function renderPanel(props: Parameters<typeof ExportPanel>[0] = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const ui = (p: Parameters<typeof ExportPanel>[0]) => (
    <QueryClientProvider client={qc}>
      <ExportPanel {...p} />
    </QueryClientProvider>
  );
  const r = render(ui(props));
  return { ...r, rerenderWith: (p: Parameters<typeof ExportPanel>[0]) => r.rerender(ui(p)) };
}

describe("ExportPanel", () => {
  beforeEach(() => mockFetch());

  it("submits a csrs-ppp export for the last 24 h and hides generic options", async () => {
    const onSubmitted = vi.fn();
    renderPanel({ initialPreset: "csrs-ppp", initialHours: 24, onSubmitted });
    expect(await screen.findByText(/24 h of data recommended/)).toBeInTheDocument();
    expect(screen.getByText(/Choose 'Static' and ITRF outside Canada/)).toBeInTheDocument();
    expect(screen.getByText(/RINEX 3\.04, 30 s interval, Hatanaka \+ gzip/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /open CSRS-PPP/i })).toHaveAttribute("target", "_blank");
    expect(screen.queryByLabelText(/interval/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/hatanaka/i)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    await waitFor(() => expect(onSubmitted).toHaveBeenCalledWith("job1"));
    const body = bodyOf(posts()[0]);
    expect(posts()[0][0]).toBe("/api/export");
    expect(body.preset).toBe("csrs-ppp");
    expect(Date.parse(String(body.end)) - Date.parse(String(body.start))).toBe(24 * 3600 * 1000);
    expect(String(body.start)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:00:00Z$/);
    // a fixed preset takes no overrides: the daemon refuses them
    expect(Object.keys(body).sort()).toEqual(["end", "preset", "start"]);
    // and the operator is pointed at the job that was queued
    expect(screen.getByRole("link", { name: /export jobs/i })).toHaveAttribute("href", "#export-jobs");
  });

  it("shows generic options when the generic preset is picked and sends them", async () => {
    renderPanel({ initialPreset: "generic" });
    expect(await screen.findByLabelText(/interval/i)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/interval/i), "30");
    await userEvent.click(screen.getByLabelText(/hatanaka/i));
    await userEvent.click(screen.getByLabelText(/gzip/i));
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(bodyOf(posts()[0])).toMatchObject({ preset: "generic", interval_s: 30, hatanaka: true, gzip: true });
  });

  it("switches preset from the select and words the fixed options", async () => {
    renderPanel({ initialPreset: "csrs-ppp" });
    await screen.findByText(/24 h of data recommended/);
    await userEvent.selectOptions(screen.getByLabelText(/target/i), "opus");
    expect(screen.getByText(/Only for sites in the USA/)).toBeInTheDocument();
    expect(screen.getByText(/RINEX 2\.11, 30 s interval, GPS only/)).toBeInTheDocument();
    expect(screen.queryByText(/24 h of data recommended/)).not.toBeInTheDocument();
  });

  it("holds the button on a bad window or interval", async () => {
    renderPanel({ initialPreset: "generic", window: ["2026-09-18T12:00", "2026-09-18T11:00"] });
    expect(await screen.findByText(/from must be before to/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start export/i })).toBeDisabled();
  });

  it("refuses an interval that is not a positive number", async () => {
    renderPanel({ initialPreset: "generic", window: ["2026-09-18T10:00", "2026-09-18T11:00"] });
    await userEvent.type(await screen.findByLabelText(/interval/i), "-5");
    expect(screen.getByText(/interval is a positive number of seconds/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start export/i })).toBeDisabled();
  });

  it("follows a window picked elsewhere on the page", async () => {
    const { rerenderWith } = renderPanel({ initialPreset: "csrs-ppp" });
    await screen.findByText(/24 h of data recommended/);
    rerenderWith({ initialPreset: "csrs-ppp", window: ["2026-09-18T10:00", "2026-09-18T11:00"] });
    expect(screen.getByLabelText(/from/i)).toHaveValue("2026-09-18T10:00");
    expect(screen.getByLabelText(/^to/i)).toHaveValue("2026-09-18T11:00");
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(bodyOf(posts()[0])).toMatchObject({ start: "2026-09-18T10:00:00Z", end: "2026-09-18T11:00:00Z" });
  });

  it("shows the daemon's refusal verbatim (one export at a time)", async () => {
    const detail = "another export is running: a synchronous download is being made; wait for it to finish, then try again";
    mockFetch({ status: 409, detail });
    const onSubmitted = vi.fn();
    renderPanel({ initialPreset: "csrs-ppp", onSubmitted });
    await screen.findByText(/24 h of data recommended/);
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(onSubmitted).not.toHaveBeenCalled();
  });

  it("falls back to the first preset for an unknown id from the URL", async () => {
    renderPanel({ initialPreset: "nope" });
    expect(await screen.findByLabelText(/target/i)).toHaveValue("csrs-ppp");
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(bodyOf(posts()[0]).preset).toBe("csrs-ppp");
  });

  it("holds the button past the 7-day cap", async () => {
    renderPanel({ initialPreset: "csrs-ppp", window: ["2026-09-10T00:00", "2026-09-18T00:00"] });
    expect(await screen.findByText(/at most 7 days per export/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start export/i })).toBeDisabled();
  });

  it("asks for both times when one is cleared", async () => {
    renderPanel({ initialPreset: "csrs-ppp", window: ["2026-09-18T10:00", "2026-09-18T11:00"] });
    await userEvent.clear(await screen.findByLabelText(/^to/i));
    expect(screen.getByText(/enter both times/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /start export/i })).toBeDisabled();
  });

  it("says verbatim why the presets could not be read", async () => {
    mockFetch(undefined, { status: 500, detail: "presets exploded" });
    renderPanel();
    expect(await screen.findByText("The export presets could not be read: presets exploded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /start export/i })).not.toBeInTheDocument();
  });

  it("sends interval_s null for an empty generic interval (the native rate)", async () => {
    renderPanel({ initialPreset: "generic", window: ["2026-09-18T10:00", "2026-09-18T11:00"] });
    await screen.findByLabelText(/interval/i);
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(bodyOf(posts()[0])).toMatchObject({ preset: "generic", interval_s: null, hatanaka: false, gzip: false });
  });

  it("does not hold a fixed preset on a bad interval typed for generic", async () => {
    renderPanel({ initialPreset: "generic", window: ["2026-09-18T10:00", "2026-09-18T11:00"] });
    await userEvent.type(await screen.findByLabelText(/interval/i), "abc");
    expect(screen.getByRole("button", { name: /start export/i })).toBeDisabled();
    await userEvent.selectOptions(screen.getByLabelText(/target/i), "csrs-ppp");
    expect(screen.getByRole("button", { name: /start export/i })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: /start export/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(Object.keys(bodyOf(posts()[0])).sort()).toEqual(["end", "preset", "start"]);
  });
});
