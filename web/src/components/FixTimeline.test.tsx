import { render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { STATUS } from "@/lib/palette";
import { FixTimeline, fixState } from "./FixTimeline";

// A fixed 60 s window; every row below sits inside it.
const FROM = "2026-09-18T16:00:00.000Z";
const TO = "2026-09-18T16:01:00.000Z";
const T0 = Date.parse(FROM) / 1000;
const COLUMNS = ["ts", "carr_soln", "fix_type"];
// One second per state, with a gap (t0+5..t0+9 missing) before the last row.
const ROWS = [
  [T0 + 0, 2, 3], // fixed
  [T0 + 1, 1, 3], // float
  [T0 + 2, 0, 3], // 3D
  [T0 + 3, 0, 2], // 2D
  [T0 + 4, 0, 0], // no fix
  [T0 + 10, 2, 3], // fixed again, after a gap
];

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function renderTimeline(from = FROM, to = TO) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <FixTimeline from={from} to={to} />
    </QueryClientProvider>,
  );
  return {
    ...view,
    move: (f: string, t: string) =>
      view.rerender(
        <QueryClientProvider client={qc}>
          <FixTimeline from={f} to={t} />
        </QueryClientProvider>,
      ),
  };
}

describe("fixState", () => {
  it("reads the carrier solution first, then the fix type", () => {
    expect(fixState(2, 3)).toBe("fixed");
    expect(fixState(1, 3)).toBe("float");
    expect(fixState(0, 3)).toBe("3d");
    expect(fixState(0, 4)).toBe("3d"); // GNSS + dead reckoning is still a 3D solution
    expect(fixState(0, 2)).toBe("2d");
    expect(fixState(0, 1)).toBe("none"); // dead reckoning only
    expect(fixState(0, 5)).toBe("none"); // time only: no position
    expect(fixState(0, 0)).toBe("none");
    expect(fixState(null, null)).toBe("none");
  });
});

describe("FixTimeline", () => {
  let calls: string[] = [];
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request) => {
      calls.push(String(url));
      return json({ res: "1s", columns: COLUMNS, rows: ROWS });
    }) as typeof fetch;
  });

  it("puts each cell at its own second, coloured and titled by state, with a legend", async () => {
    renderTimeline();
    const strip = await screen.findByRole("img", { name: /fix state/i });
    expect(strip).toHaveAttribute("viewBox", "0 0 60 10");
    // 2 of 6 sampled seconds were fixed
    expect(strip).toHaveAccessibleName("Fix state over time: 33% RTK fixed");
    const rects = [...strip.querySelectorAll("rect")];
    expect(rects.map((r) => r.getAttribute("x"))).toEqual(["0", "1", "2", "3", "4", "10"]);
    expect(rects.map((r) => r.getAttribute("fill"))).toEqual([STATUS.good, STATUS.warning, "var(--ink-3)", STATUS.serious, STATUS.critical, STATUS.good]);
    expect(rects.map((r) => r.querySelector("title")!.textContent)).toEqual([
      "16:00:00 UTC · RTK fixed",
      "16:00:01 UTC · RTK float",
      "16:00:02 UTC · 3D",
      "16:00:03 UTC · 2D",
      "16:00:04 UTC · No fix",
      "16:00:10 UTC · RTK fixed",
    ]);
    const legend = screen.getByRole("list", { name: "Fix states" });
    expect(within(legend).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["RTK fixed", "RTK float", "3D", "2D", "No fix"]);
    expect(screen.getByText("33% fixed over 6 s")).toBeInTheDocument();
    const q = new URL(calls[0], "http://x").searchParams;
    expect(q.get("metrics")).toBe("carr_soln,fix_type");
    expect(q.get("res")).toBe("1s");
    expect(q.get("from")).toBe(FROM);
    expect(q.get("to")).toBe(TO);
  });

  it("says so when the window holds no samples", async () => {
    globalThis.fetch = vi.fn(async () => json({ res: "1s", columns: COLUMNS, rows: [] })) as typeof fetch;
    renderTimeline();
    expect(await screen.findByText("No samples in this range.")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the server's error", async () => {
    globalThis.fetch = vi.fn(async () => json({ detail: "history store is closed" }, 503)) as typeof fetch;
    renderTimeline();
    expect(await screen.findByText(/Fix history unavailable: history store is closed/)).toBeInTheDocument();
  });

  it("keeps the strip on screen while the next window loads", async () => {
    const view = renderTimeline();
    await screen.findByRole("img", { name: /fix state/i });
    // the next window's fetch is held open: the old strip must stay rather than blink to "Loading…"
    let release: (r: Response) => void = () => {};
    globalThis.fetch = vi.fn(
      (url: string | URL | Request) =>
        new Promise<Response>((resolve) => {
          calls.push(String(url));
          release = resolve;
        }),
    ) as typeof fetch;
    view.move("2026-09-18T16:00:30.000Z", "2026-09-18T16:01:30.000Z");
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(screen.getByRole("img", { name: /fix state/i })).toBeInTheDocument();
    expect(screen.queryByText(/loading/i)).toBeNull();
    release(json({ res: "1s", columns: COLUMNS, rows: [[T0 + 40, 1, 3]] }));
    await waitFor(() => expect(screen.getByRole("img", { name: /fix state/i })).toHaveAccessibleName("Fix state over time: 0% RTK fixed"));
    // the new rows are placed against the new window's start
    expect(screen.getByRole("img").querySelector("rect")).toHaveAttribute("x", "10");
  });
});
