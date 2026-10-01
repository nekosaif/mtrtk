import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { resetLiveForTests, useLive } from "@/lib/live";
import type { Job } from "@/lib/types";
import { JobsPanel } from "./JobsPanel";

const job = (over: Partial<Job> = {}): Job => ({ id: "j1", kind: "export", status: "running", created_utc: "2026-09-18T11:30:00+00:00", updated_utc: null, progress: 0.5, message: null, params: {}, result: null, error: null, ...over });

let calls: string[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function renderPanel(listed: Job[]) {
  calls = [];
  globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
    const p = String(url);
    calls.push(p);
    if (p.includes("/api/jobs?")) return json(listed);
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <JobsPanel />
    </QueryClientProvider>,
  );
}

describe("JobsPanel without a kind", () => {
  beforeEach(() => resetLiveForTests());

  it("asks for every kind and shows the generic empty state", async () => {
    renderPanel([]);
    expect(await screen.findByText(/no jobs yet/i)).toBeInTheDocument();
    expect(screen.getByText(/exports and other background work appear here/i)).toBeInTheDocument();
    const req = calls.find((u) => u.includes("/api/jobs?"))!;
    expect(new URL(req, "http://x").searchParams.has("kind")).toBe(false);
  });

  it("shows live jobs of any kind over the listing", async () => {
    renderPanel([job({ id: "exp", message: "listed export" })]);
    expect(await screen.findByText(/listed export/)).toBeInTheDocument();
    act(() => {
      useLive.setState({ jobs: { other: job({ id: "other", kind: "ppk", message: "a ppk job" }) } });
    });
    await waitFor(() => expect(screen.getByText(/a ppk job/)).toBeInTheDocument());
    expect(screen.getByText(/listed export/)).toBeInTheDocument();
  });
});
