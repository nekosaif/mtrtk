import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { resetLiveForTests, useLive } from "@/lib/live";
import { resetPrefsForTests } from "@/lib/prefs";
import { sampleState } from "@/test/fixtures";
import Satellites from "./Satellites";

function renderPage() {
  return render(<MemoryRouter><Satellites /></MemoryRouter>);
}
const table = () => screen.getByRole("table");
const bodyRows = () => within(table()).getAllByRole("row").slice(1);
const cellsOf = (row: HTMLElement) => within(row).getAllByRole("cell");

describe("Satellites page", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    localStorage.clear();
    globalThis.fetch = vi.fn() as typeof fetch;
    useLive.setState({ state: sampleState(), status: "open", connected: true, stale: false, lastEpochAt: Date.now(), receiverConnected: true });
  });

  it("shows the counts in the header and three tabs, sky first", () => {
    renderPage();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Satellites");
    expect(screen.getByText("6 used of 8 tracked")).toBeInTheDocument();
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual(["Sky", "Signals", "Table"]);
    expect(screen.getByRole("tab", { name: "Sky" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("img", { name: /sky plot: 8 satellites/i })).toBeInTheDocument();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("switches to the signal bars and the table", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Signals" }));
    expect(screen.getByRole("img", { name: /signal strength: 8 signals/i })).toBeInTheDocument();
    expect(screen.queryByRole("img", { name: /sky plot/i })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    expect(screen.getByRole("tab", { name: "Table" })).toHaveAttribute("aria-selected", "true");
    expect(within(table()).getAllByRole("row")).toHaveLength(9);
  });

  it("lists satellites in the table tab and sorts by C/N0", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    expect(within(table()).getAllByRole("row")).toHaveLength(9); // header + 8
    // default order: system (fixed order) then SV id
    expect(bodyRows().map((r) => cellsOf(r)[0].textContent)).toEqual(["G5", "G12", "G25", "R3", "R9", "E4", "C21", "J194"]);
    const cno = within(table()).getByRole("columnheader", { name: /C\/N0/ });
    await userEvent.click(within(cno).getByRole("button"));
    expect(cno).toHaveAttribute("aria-sort", "descending");
    const firstCells = cellsOf(bodyRows()[0]);
    expect(firstCells[0]).toHaveTextContent("G5"); // satId first (ruling 3)
    expect(firstCells[1]).toHaveTextContent("GPS");
    expect(firstCells[3]).toHaveTextContent("45 dB-Hz"); // the strongest
    await userEvent.click(within(cno).getByRole("button"));
    expect(cno).toHaveAttribute("aria-sort", "ascending");
    expect(cellsOf(bodyRows()[0])[0]).toHaveTextContent("G25"); // 22 dB-Hz is the weakest
  });

  it("shows plain words for used, health and quality, and every figure in .num", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    const g25 = bodyRows()[2];
    const cells = cellsOf(g25);
    expect(cells[0]).toHaveTextContent("G25");
    expect(cells[6]).toHaveTextContent("no");
    expect(cells[7]).toHaveTextContent("healthy");
    expect(cells[8]).toHaveTextContent("Code+carrier");
    expect(cells[9]).toHaveTextContent("0.3 m");
    for (const i of [0, 2, 3, 4, 5, 9]) expect(cells[i].className.split(/\s+/)).toContain("num");
  });

  it("filters by system", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    await userEvent.click(screen.getByRole("button", { name: /GPS/ }));
    expect(within(table()).getAllByRole("row")).toHaveLength(6); // header + 5 non-GPS
    expect(screen.getByRole("button", { name: /GPS/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("6 used of 8 tracked")).toBeInTheDocument();
    expect(screen.getByText("5 shown")).toBeInTheDocument();
  });

  it("persists the system filter under a mtrtk: pref and reads it back", async () => {
    const first = renderPage();
    await userEvent.click(screen.getByRole("button", { name: /GLONASS/ }));
    expect(localStorage.getItem("mtrtk:satSystemsHidden")).toBe(JSON.stringify(["GLONASS"]));
    first.unmount();
    resetPrefsForTests(); // a new tab: only storage remains
    renderPage();
    expect(screen.getByRole("button", { name: /GLONASS/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("img", { name: /sky plot: 6 satellites/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /GLONASS/ }));
    expect(localStorage.getItem("mtrtk:satSystemsHidden")).toBe("[]");
  });

  it("filters to used satellites and persists that too", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    const usedOnly = screen.getByRole("button", { name: "Used only" });
    expect(usedOnly).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(usedOnly);
    expect(usedOnly).toHaveAttribute("aria-pressed", "true");
    expect(within(table()).getAllByRole("row")).toHaveLength(7); // header + 6 used
    expect(localStorage.getItem("mtrtk:satUsedOnly")).toBe("true");
  });

  it("offers a way back when the filters hide everything", async () => {
    renderPage();
    await userEvent.click(screen.getByRole("tab", { name: "Table" }));
    for (const name of ["GPS", "GLONASS", "Galileo", "BeiDou", "QZSS"]) await userEvent.click(screen.getByRole("button", { name: new RegExp(`^${name} `) }));
    expect(screen.getByText(/no satellites match the filters/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show all" }));
    expect(within(table()).getAllByRole("row")).toHaveLength(9);
    expect(localStorage.getItem("mtrtk:satSystemsHidden")).toBe("[]");
  });

  it("greys the figures when the data is stale", () => {
    useLive.setState({ stale: true });
    renderPage();
    const grid = screen.getByTestId("satellites-body");
    expect(grid).toHaveAttribute("data-stale", "true");
    expect(grid.className).toContain("[&_.num]:text-ink-3");
  });

  it("shows an empty state without a receiver state and without satellites", () => {
    useLive.setState({ state: null });
    renderPage();
    expect(screen.getByText("Waiting for the receiver")).toBeInTheDocument();
    expect(screen.queryByRole("tab")).toBeNull();
    const s = sampleState();
    s.sats = [];
    s.sat_summary = { tracked: 0, used: 0, per_gnss: {} };
    act(() => useLive.setState({ state: s }));
    expect(screen.getByText("No satellites tracked yet")).toBeInTheDocument();
    expect(screen.queryByRole("tab")).toBeNull();
  });
});
