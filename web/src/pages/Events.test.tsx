import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { act } from "react";
import { resetLiveForTests, useLive } from "@/lib/live";
import type { EventItem } from "@/lib/types";
import Events from "./Events";

// ---- fixtures -----------------------------------------------------------------------------

const rows: EventItem[] = [
  {
    id: 2,
    ts_utc: "2026-09-18T16:40:00+00:00",
    level: "warning",
    kind: "jamming",
    message: "RF interference: jam_ind=210",
    meta: { jam_ind: 210 },
    acked: false,
  },
  {
    id: 1,
    ts_utc: "2026-09-18T16:00:00+00:00",
    level: "info",
    kind: "survey_in_valid",
    message: "survey-in complete",
    meta: {},
    acked: true,
  },
];

const liveError: EventItem = {
  id: 3,
  ts_utc: "2026-09-18T16:50:00+00:00",
  level: "error",
  kind: "receiver_disconnected",
  message: "receiver disconnected: unplugged",
  meta: {},
  acked: false,
};

interface Answers {
  list?: EventItem[];
  warning?: EventItem[];
  /** Status + detail for POST /api/events/{id}/ack; default 200 {"ok": true}. */
  ack?: { status: number; detail: string };
}

let calls: [string, RequestInit | undefined][] = [];

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    calls.push([p, init]);
    if (/\/ack$/.test(p)) {
      if (a.ack && a.ack.status >= 400) return json({ detail: a.ack.detail }, a.ack.status);
      return json({ ok: true });
    }
    if (p.includes("/api/events")) {
      if (p.includes("level=warning")) return json(a.warning ?? (a.list ?? rows).filter((e) => e.level === "warning"));
      if (p.includes("level=")) return json((a.list ?? rows).filter((e) => p.includes(`level=${e.level}`)));
      return json(a.list ?? rows);
    }
    return json({ detail: `unexpected route ${p}` }, 404);
  }) as typeof fetch;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Events />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const callsTo = (method: string, suffix: string) => calls.filter(([u, i]) => (i?.method ?? "GET") === method && u.split("?")[0].endsWith(suffix));

describe("Events page", () => {
  beforeEach(() => {
    resetLiveForTests();
    mockFetch();
  });

  it("lists events newest first, filters by level and acknowledges one", async () => {
    renderPage();
    expect(await screen.findByText(/RF interference/)).toBeInTheDocument();
    expect(screen.getByText(/survey-in complete/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /^warning$/i }));
    expect(await screen.findByText(/RF interference/)).toBeInTheDocument();
    expect(screen.queryByText(/survey-in complete/)).not.toBeInTheDocument();
    expect(calls.some(([u]) => u.includes("level=warning"))).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: /acknowledge/i }));
    expect(callsTo("POST", "/api/events/2/ack")).toHaveLength(1);
  });

  it("prepends live events and never shows one twice", async () => {
    renderPage();
    await screen.findByText(/RF interference/);

    act(() => useLive.setState({ events: [liveError] }));
    expect(await screen.findByText(/receiver disconnected/)).toBeInTheDocument();

    // The same event, now also in the fetched page: one row, not two.
    act(() => useLive.setState({ events: [liveError, rows[0]] }));
    expect(screen.getAllByText(/RF interference/)).toHaveLength(1);

    const list = screen.getByRole("list", { name: /events/i });
    const messages = within(list).getAllByTestId("event-message").map((n) => n.textContent);
    expect(messages).toEqual(["receiver disconnected: unplugged", "RF interference: jam_ind=210", "survey-in complete"]);
  });

  it("renders a live event that has no id yet and cannot acknowledge it", async () => {
    renderPage();
    await screen.findByText(/RF interference/);
    const noId = { ...liveError, id: null } as unknown as EventItem;
    act(() => useLive.setState({ events: [noId] }));

    const row = (await screen.findByText(/receiver disconnected/)).closest("li")!;
    expect(within(row).getByRole("button", { name: /acknowledge/i })).toBeDisabled();
  });

  it("shows the daemon's 404 detail verbatim when the event is already gone", async () => {
    mockFetch({ ack: { status: 404, detail: "no event with id 2" } });
    renderPage();
    await screen.findByText(/RF interference/);
    await userEvent.click(screen.getAllByRole("button", { name: /acknowledge/i })[0]);
    expect(await screen.findByRole("alert")).toHaveTextContent("no event with id 2");
  });

  it("puts the event's meta behind a disclosure and shows UTC with a local tooltip", async () => {
    renderPage();
    const row = (await screen.findByText(/RF interference/)).closest("li")!;
    const details = within(row).getByText(/details/i).closest("details")!;
    expect(details.open).toBe(false);
    await userEvent.click(within(row).getByText(/details/i));
    expect(details.open).toBe(true);
    expect(details).toHaveTextContent(/jam_ind/);

    const time = within(row).getByText(/2026-09-18 16:40:00 UTC/);
    expect(time.getAttribute("title")).toMatch(/UTC[+-]\d\d:\d\d/);
  });

  it("wraps the message onto its own line on a phone instead of squeezing it", async () => {
    renderPage();
    const message = await screen.findByText(/RF interference/);
    // jsdom has no layout engine: pin the class contract the breakpoint acts on.
    const column = message.parentElement!;
    expect(column.className).toContain("basis-[240px]");
    expect(column.className).toContain("max-sm:basis-full");
    expect(column.closest("li")!.className).toContain("flex-wrap");
  });

  it("says so when the log is empty, naming the filter", async () => {
    mockFetch({ list: [], warning: [] });
    renderPage();
    expect(await screen.findByText(/nothing has been logged/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^error$/i }));
    expect(await screen.findByText(/no error events/i)).toBeInTheDocument();
  });
});
