import { fireEvent, render, screen, within } from "@testing-library/react";
import { TimeSeries, niceTicks, timeTicks } from "./TimeSeries";

describe("TimeSeries", () => {
  const points = Array.from({ length: 10 }, (_, i) => ({ t: 1_700_000_000 + i * 60, v: i === 5 ? null : i * 0.1 }));

  it("renders a path with a gap and a table alternative", () => {
    render(<TimeSeries points={points} label="Horizontal accuracy" unit="m" format={(v) => v.toFixed(2)} />);
    const img = screen.getByRole("img", { name: /horizontal accuracy/i });
    const d = img.querySelector("path[data-series]")!.getAttribute("d")!;
    expect(d.match(/M/g)).toHaveLength(2); // two segments around the null
    expect(screen.getByText(/show as table/i)).toBeInTheDocument();
    expect(img.querySelectorAll("text").length).toBeGreaterThan(4); // axis ticks
  });

  it("shows a message with too little data", () => {
    render(<TimeSeries points={[]} label="x" unit="" format={String} />);
    expect(screen.getByText(/no data in this range/i)).toBeInTheDocument();
  });

  it("places x ticks on round UTC minutes taken from the real timestamps, labelled HH:MM", () => {
    render(<TimeSeries points={points} label="Sats" unit="" format={(v) => v.toFixed(0)} />);
    const img = screen.getByRole("img", { name: /sats/i });
    const ticks = [...img.querySelectorAll("text[data-x-tick]")];
    expect(ticks.length).toBeGreaterThanOrEqual(3);
    // 1_700_000_000 is 2023-11-14 22:13:20 UTC; the span is 9 minutes, so ticks land on even minutes
    const labels = ticks.map((t) => t.textContent);
    expect(labels).toEqual(["22:14", "22:16", "22:18", "22:20", "22:22"]);
    for (const t of ticks) expect(Number(t.getAttribute("data-x-tick")) % 120).toBe(0);
  });

  it("labels a tick on a day boundary with the date and keeps HH:MM elsewhere", () => {
    const midnight = 1_700_006_400; // 2023-11-15 00:00:00 UTC
    const pts = Array.from({ length: 25 }, (_, i) => ({ t: midnight - 3600 * 12 + i * 3600, v: i }));
    render(<TimeSeries points={pts} label="Day" unit="" format={(v) => v.toFixed(0)} />);
    const labels = [...screen.getByRole("img", { name: /day/i }).querySelectorAll("text[data-x-tick]")].map((t) => t.textContent);
    expect(labels).toContain("15 Nov");
    expect(labels.some((l) => /^\d\d:\d\d$/.test(l ?? ""))).toBe(true);
  });

  it("spans the given domain and breaks the line across a long silence", () => {
    const pts = [
      { t: 1000, v: 1 },
      { t: 1001, v: 2 },
      { t: 1002, v: 3 },
      { t: 1600, v: 4 }, // ten minutes later: a gap, not a slope
      { t: 1601, v: 5 },
    ];
    render(<TimeSeries points={pts} label="Gap" unit="" format={String} domain={[0, 3600]} gapS={10} />);
    const img = screen.getByRole("img", { name: /gap/i });
    const d = img.querySelector("path[data-series]")!.getAttribute("d")!;
    expect(d.match(/M/g)).toHaveLength(2);
    expect(img.getAttribute("data-domain")).toBe("0,3600");
  });

  it("draws four-ish y ticks on round values and a crosshair readout on hover", () => {
    render(<TimeSeries points={points} label="Acc" unit="m" format={(v) => v.toFixed(1)} />);
    const img = screen.getByRole("img", { name: /acc/i });
    const y = [...img.querySelectorAll("text[data-y-tick]")].map((t) => t.textContent);
    expect(y.length).toBeGreaterThanOrEqual(3);
    expect(y.length).toBeLessThanOrEqual(6);
    expect(y).toEqual([...y].sort((a, b) => Number(a) - Number(b))); // drawn bottom-up, in DOM order
    expect(img.querySelector("[data-crosshair]")).toBeNull();
    fireEvent.mouseMove(img, { clientX: 400 });
    expect(img.querySelector("[data-crosshair]")).not.toBeNull();
    expect(screen.getByTestId("timeseries-readout")).toHaveTextContent(/UTC · \d\.\d/);
    fireEvent.mouseLeave(img);
    expect(img.querySelector("[data-crosshair]")).toBeNull();
  });

  it("prints each y label once when the format is coarser than the tick step", () => {
    const pts = [
      { t: 0, v: 25 },
      { t: 60, v: 26 },
      { t: 120, v: 25 },
    ];
    render(<TimeSeries points={pts} label="Sats" unit="" format={(v) => v.toFixed(0)} />);
    const y = [...screen.getByRole("img", { name: /sats/i }).querySelectorAll("text[data-y-tick]")].map((t) => t.textContent);
    expect(y).toEqual(["25", "26"]);
  });

  it("lists the samples in the table alternative with UTC times", () => {
    render(<TimeSeries points={points} label="Acc" unit="m" format={(v) => v.toFixed(2)} />);
    const table = screen.getByRole("table", { name: /acc/i });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(1 + 9); // header + the nine non-null samples
    expect(rows[1]).toHaveTextContent("2023-11-14 22:13:20");
    expect(rows[1]).toHaveTextContent("0.00");
  });
});

describe("tick helpers", () => {
  it("niceTicks covers the range with round steps", () => {
    expect(niceTicks(0.03, 0.91)).toEqual([0, 0.2, 0.4, 0.6, 0.8, 1]);
    expect(niceTicks(5, 5)).toEqual([5, 6]);
    expect(niceTicks(12, 47)).toEqual([10, 20, 30, 40, 50]);
  });
  it("timeTicks picks a step that keeps the count within the budget", () => {
    const t = timeTicks(0, 86400, 6);
    expect(t.length).toBeLessThanOrEqual(7);
    expect(t.every((x) => x % 21600 === 0)).toBe(true);
    expect(timeTicks(0, 90 * 86400, 6).every((x) => x % 86400 === 0)).toBe(true);
  });
});
