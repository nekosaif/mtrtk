import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import type { ReceiverInfo } from "@/lib/types";
import { sampleState } from "@/test/fixtures";
import Receiver, { SPAN_WAIT_MS } from "./Receiver";

const receiverInfo: ReceiverInfo = {
  connected: true,
  passive: false,
  source: "auto",
  capabilities: { protver: "27.12", fw_version: "HPG 1.13", module: "ZED-F9P", supported: ["MON-COMMS"], unsupported: ["MON-SPAN"] },
  firmware: sampleState().firmware,
};

type Answer = { status: number; body: unknown };
type Answers = Partial<Record<"receiver" | "reapply" | "reset" | "poll", Answer>>;

/** The receiver routes the page may touch; everything else is a 404 and asserted against. */
function mockFetch(answers: Answers = {}) {
  const a: Required<Answers> = {
    receiver: { status: 200, body: receiverInfo },
    reapply: { status: 200, body: { ok: true, capabilities: receiverInfo.capabilities } },
    reset: { status: 200, body: { ok: true, kind: "warm" } },
    poll: { status: 200, body: { identity: "MON-VER", swVersion: "EXT CORE 1.00 (f10c36)", hwVersion: "00190000", extension: ["FWVER=HPG 1.13", "PROTVER=27.12"] } },
    ...answers,
  };
  const json = (r: Answer) => new Response(JSON.stringify(r.body), { status: r.status, headers: { "content-type": "application/json" } });
  globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const path = typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
    if (path.endsWith("/api/receiver") && !init?.method) return json(a.receiver);
    if (path.endsWith("/api/receiver/reapply")) return json(a.reapply);
    if (path.endsWith("/api/receiver/reset")) return json(a.reset);
    if (path.endsWith("/api/receiver/poll")) return json(a.poll);
    return json({ status: 404, body: { detail: "not found" } });
  }) as typeof fetch;
}

const calls = () => (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit | undefined][] } }).mock.calls;
const postsTo = (suffix: string) => calls().filter(([u, init]) => String(u).endsWith(suffix) && init?.method === "POST");

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}><MemoryRouter><Receiver /></MemoryRouter></QueryClientProvider>);
}

describe("Receiver page", () => {
  beforeEach(() => {
    resetLiveForTests();
    useLive.setState({ state: sampleState(), status: "open", connected: true, stale: false, lastEpochAt: Date.now(), receiverConnected: true });
    mockFetch();
  });

  // ---- the brief's acceptance tests --------------------------------------------------------

  it("shows RF blocks, firmware and the spectrum-unsupported notice", async () => {
    renderPage();
    expect(await screen.findByText("HPG 1.13")).toBeInTheDocument();
    expect(screen.getAllByText(/RF block/)).toHaveLength(2);
    expect(screen.getAllByText(/jamming indicator/i)).toHaveLength(2); // one gauge per RF block
    expect(await screen.findByText(/spectrum .*not supported by this firmware/i)).toBeInTheDocument();
    expect(screen.getByText(/GPS week/)).toBeInTheDocument();
    expect(screen.getByText("2436")).toBeInTheDocument();
  });

  it("reset requires confirmation and posts the kind", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /reset/i }));
    await userEvent.selectOptions(screen.getByLabelText(/reset type/i), "warm");
    await userEvent.click(screen.getByRole("button", { name: /confirm reset/i }));
    const reset = calls().find(([u]) => String(u).endsWith("/api/receiver/reset"))!;
    expect(JSON.parse(reset[1]!.body as string)).toEqual({ kind: "warm" });
  });

  // ---- rulings ----------------------------------------------------------------------------

  it("decodes antenna and interference to words with the code in a tooltip, colour paired with the word", async () => {
    renderPage();
    await screen.findByText("HPG 1.13");
    const block0 = screen.getByRole("region", { name: "RF block 0" });
    const antenna = within(block0).getByText("Antenna").parentElement!.querySelector("[data-stat-value]")!;
    expect(antenna).toHaveTextContent("OK");
    expect(antenna).toHaveAttribute("title", "antStatus 2");
    expect((antenna as HTMLElement).style.color).toBe("var(--status-good)");
    expect(within(block0).getByText("Antenna power").parentElement!.querySelector("[data-stat-value]")).toHaveTextContent("On");
    expect(within(block0).getByText(/Interference OK/)).toBeInTheDocument();
    // gauges scale: jam 10/255, AGC 4000/8191
    const meters = within(block0).getAllByRole("meter");
    expect(meters[0]).toHaveAttribute("aria-valuemax", "255");
    expect(meters[0].querySelector("[data-gauge-fill]")).toHaveStyle({ width: "3.9%" });
    expect(meters[1]).toHaveAttribute("aria-valuemax", "8191");
    expect(meters[1].querySelector("[data-gauge-fill]")).toHaveStyle({ width: "48.8%" });
    // a short circuit is critical and says so in words
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, rf: prev.rf.map((b, i) => (i === 0 ? { ...b, ant_status: 3, jamming_state: 3 } : b)) } });
    });
    const shorted = within(block0).getByText("Antenna").parentElement!.querySelector("[data-stat-value]")!;
    expect(shorted).toHaveTextContent("Short circuit");
    expect((shorted as HTMLElement).style.color).toBe("var(--status-critical)");
    expect(within(block0).getByText(/Interference critical/)).toBeInTheDocument();
  });

  it("keeps a jam/AGC trend per block and shows the hardware panel words", async () => {
    renderPage();
    await screen.findByText("HPG 1.13");
    const block1 = screen.getByRole("region", { name: "RF block 1" });
    expect(within(block1).getAllByText("Collecting…")).toHaveLength(2); // one sample so far
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, rf: prev.rf.map((b) => ({ ...b, jam_ind: b.jam_ind + 3 })) } });
    });
    expect(within(block1).getByRole("img", { name: /Jamming trend: min 5, max 8, last 8/ })).toBeInTheDocument();
    expect(within(block1).getByRole("img", { name: /AGC trend/ })).toBeInTheDocument();
    const hw = screen.getByRole("region", { name: "Antenna & hardware" });
    expect(within(hw).getByText("RTC calibrated").parentElement).toHaveTextContent("yes");
    expect(within(hw).getByText("Safe boot").parentElement).toHaveTextContent("no");
    expect(within(hw).getByText("Crystal").parentElement).toHaveTextContent("present");
    expect(within(hw).queryByRole("meter")).toBeNull(); // the gauges live on the RF blocks when there are any
  });

  it("numbers RF blocks by position when the daemon repeats a block id, and keeps each trend on its own block", async () => {
    const s = sampleState();
    s.rf = s.rf.map((b) => ({ ...b, block_id: 0 })); // what the HPG 1.13 replay reports today
    s.ports = [{ ...s.ports[0], port_id: 0x0200 }, { ...s.ports[0], port_id: 0x0101 }];
    useLive.setState({ state: s });
    renderPage();
    await screen.findByText("HPG 1.13");
    expect(screen.getAllByRole("region", { name: /^RF block \d$/ }).map((r) => r.getAttribute("aria-labelledby") && within(r).getByRole("heading", { level: 2 }).textContent)).toEqual(["RF block 0", "RF block 1"]);
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, rf: prev.rf.map((b, i) => ({ ...b, jam_ind: i === 1 ? 50 : b.jam_ind })) } });
    });
    const second = screen.getByRole("region", { name: "RF block 1" });
    expect(within(second).getByRole("img", { name: /Jamming trend: min 5, max 50, last 50/ })).toBeInTheDocument();
    const ports = screen.getByRole("region", { name: "Ports" });
    expect(within(ports).getByText("UART2")).toBeInTheDocument();
    expect(within(ports).getByText("UART1")).toBeInTheDocument();
    expect(within(ports).getByText("0x0101")).toBeInTheDocument();
  });

  it("draws the spectrum when the firmware supports it, and waits, then gives up, when nothing arrives", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mockFetch({ receiver: { status: 200, body: { ...receiverInfo, capabilities: { ...receiverInfo.capabilities!, unsupported: [] } } } });
      renderPage();
      expect(await screen.findByRole("img", { name: /RF spectrum: 1 block/ })).toBeInTheDocument();
      expect(screen.queryByText(/not supported by this firmware/i)).toBeNull();
      // no spectra, connected: wait SPAN_WAIT_MS before concluding
      act(() => useLive.setState({ state: { ...useLive.getState().state!, spectrum: [] } }));
      expect(screen.getByText(/waiting for spectrum data/i)).toBeInTheDocument();
      act(() => vi.advanceTimersByTime(SPAN_WAIT_MS - 100));
      expect(screen.queryByText(/not supported by this firmware/i)).toBeNull();
      act(() => vi.advanceTimersByTime(200));
      expect(screen.getByText(/spectrum .*not supported by this firmware/i)).toBeInTheDocument();
      // a spectrum arriving later still wins
      act(() => useLive.setState({ state: { ...useLive.getState().state!, spectrum: sampleState().spectrum } }));
      expect(screen.getByRole("img", { name: /RF spectrum: 1 block/ })).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows firmware from the capabilities, the source, the time words and the ports", async () => {
    renderPage();
    const fw = await screen.findByRole("region", { name: "Firmware" });
    await within(fw).findByText(/^Supported/); // the capabilities have answered
    expect(within(fw).getByText("Module").parentElement).toHaveTextContent("ZED-F9P");
    expect(within(fw).getByText("Protocol version").parentElement).toHaveTextContent("27.12");
    expect(within(fw).getByText("Source").parentElement).toHaveTextContent("auto");
    expect(within(fw).getByText("serial:/dev/ttyACM0")).toBeInTheDocument(); // what "auto" resolved to, as the hint
    expect(within(fw).getByText(/Supported/)).toHaveTextContent("MON-COMMS");
    expect(within(fw).getByText(/Unsupported/)).toHaveTextContent("MON-SPAN");
    const time = screen.getByRole("region", { name: "Time" });
    const utc = within(time).getByText("UTC").parentElement!.querySelector("[data-stat-value]")!;
    expect(utc).toHaveTextContent("2026-09-18 16:47:34 UTC");
    expect(utc.getAttribute("title")).toMatch(/^2026-09-18 \d\d:\d\d:\d\d UTC[+-]\d\d:\d\d local$/);
    expect(within(time).getByText("Time of week").parentElement).toHaveTextContent("492472.000 s");
    expect(within(time).getByText("Leap seconds").parentElement).toHaveTextContent("18 s");
    expect(within(time).getByText("Valid").parentElement).toHaveTextContent("date · time · UTC · fully resolved");
    expect(within(time).getByText("UTC standard").parentElement).toHaveTextContent("USNO");
    const ports = screen.getByRole("region", { name: "Ports" });
    expect(within(ports).getByText("USB")).toBeInTheDocument();
    expect(within(ports).getByText("TX").parentElement).toHaveTextContent("123.5 kB · 5% (peak 40%)");
  });

  it("disables the actions with a note when the source is passive", async () => {
    mockFetch({ receiver: { status: 200, body: { ...receiverInfo, passive: true, source: "file:tests/fixtures/f9p.ubx" } } });
    renderPage();
    const actions = await screen.findByRole("region", { name: "Actions" });
    await waitFor(() => expect(within(actions).getByRole("button", { name: "Reset…" })).toBeDisabled());
    expect(within(actions).getByRole("button", { name: "Re-apply profile" })).toBeDisabled();
    expect(within(actions).getByRole("button", { name: "Poll a message…" })).toBeDisabled();
    expect(within(actions).getByText(/passive/i)).toHaveTextContent(/replay/i);
    await userEvent.click(within(actions).getByRole("button", { name: "Reset…" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(postsTo("/api/receiver/reset")).toHaveLength(0);
  });

  it("disables the actions when the receiver is not connected", async () => {
    mockFetch({ receiver: { status: 200, body: { ...receiverInfo, connected: false } } });
    useLive.setState({ receiverConnected: false });
    renderPage();
    const actions = await screen.findByRole("region", { name: "Actions" });
    await waitFor(() => expect(within(actions).getByRole("button", { name: "Re-apply profile" })).toBeDisabled());
    expect(within(actions).getByText(/not connected/i)).toBeInTheDocument();
  });

  it("factory reset needs the word typed, says what it clears, and shows a reconnecting state until the receiver is back", async () => {
    mockFetch({ reset: { status: 200, body: { ok: true, kind: "factory" } } });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Reset…" }));
    await userEvent.selectOptions(screen.getByLabelText(/reset type/i), "factory");
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent(/clears BBR and flash/i);
    expect(dialog).toHaveTextContent(/re-persists the profile/i);
    const confirm = screen.getByRole("button", { name: /confirm reset/i });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: /type factory to continue/i }), "factory");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(JSON.parse(postsTo("/api/receiver/reset")[0][1]!.body as string)).toEqual({ kind: "factory" });
    const status = await screen.findByRole("status", { name: /reset/i });
    expect(status).toHaveTextContent(/factory reset sent/i);
    expect(status).toHaveTextContent(/reconnect/i);
    // the USB device drops off the bus and comes back: receiver.disconnected then receiver.connected
    act(() => useLive.setState({ receiverConnected: false }));
    expect(screen.getByRole("status", { name: /reset/i })).toBeInTheDocument();
    act(() => useLive.setState({ receiverConnected: true }));
    await waitFor(() => expect(screen.queryByRole("status", { name: /reset/i })).toBeNull());
  });

  it("cold reset also needs the word typed", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Reset…" }));
    await userEvent.selectOptions(screen.getByLabelText(/reset type/i), "cold");
    expect(screen.getByRole("button", { name: /confirm reset/i })).toBeDisabled();
    expect(screen.getByRole("textbox", { name: /type cold to continue/i })).toBeInTheDocument();
  });

  it("surfaces a 504 detail verbatim inside the re-apply dialog", async () => {
    mockFetch({ reapply: { status: 504, body: { detail: "receiver did not answer: no reply within 2.0 s" } } });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Re-apply profile" }));
    await userEvent.click(screen.getByRole("button", { name: "Re-apply" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("receiver did not answer: no reply within 2.0 s");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("surfaces a 409 detail verbatim when a reset is refused", async () => {
    mockFetch({ reset: { status: 409, body: { detail: "receiver is in passive mode: mtrtk only listens and writes no configuration" } } });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Reset…" }));
    await userEvent.click(screen.getByRole("button", { name: /confirm reset/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("receiver is in passive mode: mtrtk only listens and writes no configuration");
  });

  it("re-applies the profile and reports it", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Re-apply profile" }));
    await userEvent.click(screen.getByRole("button", { name: "Re-apply" }));
    expect(postsTo("/api/receiver/reapply")).toHaveLength(1);
    expect(await screen.findByRole("status", { name: /re-apply/i })).toHaveTextContent(/profile re-applied/i);
  });

  it("polls a message from free text or a quick pick and renders the reply as a table", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Poll a message…" }));
    const dialog = screen.getByRole("dialog", { name: /poll a UBX message/i });
    const cls = within(dialog).getByRole("textbox", { name: "Message class" });
    const id = within(dialog).getByRole("textbox", { name: "Message id" });
    // a quick pick fills both fields
    await userEvent.click(within(dialog).getByRole("button", { name: "MON-HW" }));
    expect(cls).toHaveValue("MON");
    expect(id).toHaveValue("MON-HW");
    expect(within(dialog).queryByRole("button", { name: /VALGET/ })).toBeNull();
    // free text is upper-cased
    await userEvent.clear(id);
    await userEvent.type(id, "mon-ver");
    expect(id).toHaveValue("MON-VER");
    await userEvent.click(within(dialog).getByRole("button", { name: "Poll" }));
    const posted = postsTo("/api/receiver/poll");
    expect(posted).toHaveLength(1);
    expect(JSON.parse(posted[0][1]!.body as string)).toEqual({ msg_class: "MON", msg_id: "MON-VER" });
    const table = await within(dialog).findByRole("table");
    const rows = within(table).getAllByRole("row").map((r) => within(r).queryAllByRole("cell").map((c) => c.textContent));
    expect(rows).toContainEqual(["identity", "MON-VER"]);
    expect(rows).toContainEqual(["swVersion", "EXT CORE 1.00 (f10c36)"]);
    expect(rows).toContainEqual(["extension", '["FWVER=HPG 1.13","PROTVER=27.12"]']);
    expect(within(dialog).getByRole("button", { name: /copy/i })).toBeInTheDocument();
  });

  it("shows a poll 422 verbatim and names an ACK-NAK for what it is", async () => {
    mockFetch({ poll: { status: 422, body: { detail: "cannot poll MON-XYZ: Unknown message type" } } });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Poll a message…" }));
    await userEvent.click(screen.getByRole("button", { name: "Poll" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("cannot poll MON-XYZ: Unknown message type");
    mockFetch({ poll: { status: 200, body: { identity: "ACK-NAK", clsID: 10, msgID: 4 } } });
    await userEvent.click(screen.getByRole("button", { name: "Poll" }));
    expect(await screen.findByText(/receiver refused this poll/i)).toBeInTheDocument();
  });

  it("greys the figures when the data is stale and shows an empty state without a receiver state", async () => {
    useLive.setState({ stale: true });
    renderPage();
    const grid = await screen.findByTestId("receiver-grid");
    expect(grid).toHaveAttribute("data-stale", "true");
    expect(grid.className).toContain("[&_.num]:text-ink-3");
    act(() => useLive.setState({ state: null }));
    expect(screen.getByText("Waiting for the receiver")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Receiver");
  });

  it("touches only the receiver routes", async () => {
    renderPage();
    await screen.findByText("HPG 1.13");
    expect(calls().every(([u]) => String(u).includes("/api/receiver"))).toBe(true);
  });
});
