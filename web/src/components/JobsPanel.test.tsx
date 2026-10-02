import type { ComponentProps } from "react";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { resetLiveForTests, useLive } from "@/lib/live";
import { bindLiveToQueries } from "@/lib/queries";
import type { Job } from "@/lib/types";
import { JobsPanel } from "./JobsPanel";

const job = (over: Partial<Job> = {}): Job => ({ id: "j1", kind: "export", status: "running", created_utc: "2026-09-18T11:30:00+00:00", updated_utc: null, progress: 0.5, message: null, params: {}, result: null, error: null, ...over });

let calls: string[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

interface Answers {
  jobs: Job[];
  files?: Record<string, { name: string; bytes: number }[]>;
  /** Status per result-file URL suffix; default 200. */
  file?: Record<string, { status: number; detail: string }>;
}

function renderPanel(listed: Job[] | Answers, props: ComponentProps<typeof JobsPanel> = {}) {
  calls = [];
  const a: Answers = Array.isArray(listed) ? { jobs: listed } : listed;
  globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
    const p = String(url);
    calls.push(p);
    if (p.includes("/api/jobs?")) return json(a.jobs);
    const files = /\/api\/jobs\/([^/]+)\/files$/.exec(p);
    if (files) return json(a.files?.[files[1]] ?? []);
    const one = /\/api\/jobs\/[^/]+\/files\/(.+)$/.exec(p);
    if (one) {
      const bad = a.file?.[one[1]];
      return bad ? json({ detail: bad.detail }, bad.status) : new Response("RINEX", { status: 200 });
    }
    return json({ detail: "not found" }, 404);
  }) as typeof fetch;
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <JobsPanel {...props} />
    </QueryClientProvider>,
  );
  return { qc, answers: a };
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

describe("JobsPanel merging the listing with the live slice", () => {
  beforeEach(() => resetLiveForTests());
  const at = (hms: string) => `2026-09-18T${hms}+00:00`;

  it("drops a job deleted in another tab once the next listing no longer has it", async () => {
    const { qc, answers } = renderPanel([job({ status: "done", updated_utc: at("11:31:00"), message: "finished" })]);
    expect(await screen.findByText("finished")).toBeInTheDocument();
    act(() => {
      useLive.setState({ jobs: { j1: job({ status: "done", updated_utc: at("11:31:00"), message: "finished" }) } });
    });
    answers.jobs = []; // deleted elsewhere, and its jobs.deleted lost on a bus that fell behind
    await act(() => qc.refetchQueries({ queryKey: ["jobs"] }));
    await waitFor(() => expect(screen.queryByText("finished")).not.toBeInTheDocument());
    expect(screen.getByText(/no jobs yet/i)).toBeInTheDocument();
  });

  it("lets a polled done beat a stale live running whose final update was lost", async () => {
    const running = job({ status: "running", progress: 0.5, updated_utc: at("11:30:30"), message: "converting with convbin" });
    const { qc, answers } = renderPanel({ jobs: [running], files: { j1: [{ name: "MTRK.crx.gz", bytes: 1234 }] } });
    expect(await screen.findByText(/converting with convbin/)).toBeInTheDocument();
    act(() => {
      useLive.setState({ jobs: { j1: running } });
    });
    answers.jobs = [job({ status: "done", progress: 1, updated_utc: at("11:31:00"), message: "finished" })];
    await act(() => qc.refetchQueries({ queryKey: ["jobs"] }));
    expect(await screen.findByText("Done")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(await screen.findByText("MTRK.crx.gz")).toBeInTheDocument(); // the files were asked for
  });

  it("drops a queued job deleted in another tab as soon as the socket says so", async () => {
    const queued = job({ id: "q1", status: "queued", progress: 0, updated_utc: at("11:30:00"), message: "waiting for a slot" });
    const onDeleted = vi.fn();
    const { qc, answers } = renderPanel([queued], { onDeleted });
    const off = bindLiveToQueries(qc);
    try {
      expect(await screen.findByText("waiting for a slot")).toBeInTheDocument();
      act(() => {
        useLive.setState({ jobs: { q1: queued } });
      });
      answers.jobs = []; // the daemon no longer has it
      act(() => {
        useLive.getState().applyMessage({ type: "update", topic: "jobs", source: "jobs.deleted", data: { id: "q1", deleted: true } });
      });
      // No refetch awaited: the live slice and the cached listing both let go of it at once.
      expect(screen.queryByText("waiting for a slot")).not.toBeInTheDocument();
      expect(await screen.findByText(/no jobs yet/i)).toBeInTheDocument();
      expect(onDeleted).toHaveBeenCalledWith("q1"); // the PPK page closes that job's result here too
    } finally {
      off();
    }
  });

  it("does not report a deletion it was mounted after", async () => {
    // Kept across a reconnect, so a panel opened later (another page) finds it already set.
    useLive.setState({ lastDeletedJobId: "q1" });
    const onDeleted = vi.fn();
    renderPanel([job({ id: "j1", message: "still listed" })], { onDeleted });
    expect(await screen.findByText("still listed")).toBeInTheDocument();
    expect(onDeleted).not.toHaveBeenCalled();
    act(() => {
      useLive.getState().applyMessage({ type: "update", topic: "jobs", source: "jobs.deleted", data: { id: "j9", deleted: true } });
    });
    expect(onDeleted).toHaveBeenCalledExactlyOnceWith("j9"); // a new one still is
  });

  it("keeps a job only the live slice has while it is queued", async () => {
    renderPanel([]);
    expect(await screen.findByText(/no jobs yet/i)).toBeInTheDocument();
    act(() => {
      useLive.setState({ jobs: { new1: job({ id: "new1", status: "queued", created_utc: at("10:00:00"), message: "just submitted" }) } });
    });
    expect(await screen.findByText("just submitted")).toBeInTheDocument();
  });
});

describe("JobsPanel rows", () => {
  beforeEach(() => resetLiveForTests());

  it("shows an export's window and words a failure as where it stopped", async () => {
    renderPanel([
      job({
        status: "failed",
        message: "converting with convbin",
        error: "ConvbinError: convbin exited 1",
        params: { preset: "csrs-ppp", start: "2026-09-18T00:00:00+00:00", end: "2026-09-18T06:00:00+00:00" },
      }),
    ]);
    expect(await screen.findByText("2026-09-18 00:00 → 06:00 UTC")).toBeInTheDocument();
    expect(screen.getByText(/Error while converting with convbin:/)).toBeInTheDocument();
    expect(screen.getByText(/ConvbinError: convbin exited 1/)).toBeInTheDocument();
    expect(screen.queryByText(/^converting with convbin$/)).not.toBeInTheDocument();
  });

  it("checks a result file is there before the browser saves it", async () => {
    renderPanel({
      jobs: [job({ status: "done", message: "finished" })],
      files: { j1: [{ name: "gone.crx.gz", bytes: 10 }, { name: "MTRK.crx.gz", bytes: 20 }] },
      file: { "gone.crx.gz": { status: 404, detail: "no job with that id" } },
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    try {
      await userEvent.click(await screen.findByText("gone.crx.gz"));
      expect(await screen.findByRole("alert")).toHaveTextContent(/no job with that id/);
      expect(click).not.toHaveBeenCalled(); // the 404's JSON is never saved as the RINEX file
      await userEvent.click(screen.getByText("MTRK.crx.gz"));
      await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
      const row = screen.getByText("MTRK.crx.gz").closest("li")!;
      expect(within(row).queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      click.mockRestore();
    }
  });
});
