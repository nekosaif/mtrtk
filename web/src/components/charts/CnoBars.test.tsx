import { render, screen } from "@testing-library/react";
import { sampleState, sat } from "@/test/fixtures";
import type { Satellite } from "@/lib/types";
import { CnoBars } from "./CnoBars";

/** A satellite with two signals (L1C/A at `cno`, L2C at `cno2`). */
function dualSat(sv: number, cno: number, cno2: number): Satellite {
  const s = sat(0, "GPS", sv, cno, 40, 100);
  s.signals.push({ ...s.signals[0], sig_id: 3, name: "L2C", cno: cno2 });
  return s;
}

const img = () => screen.getByRole("img", { name: /signal strength/i });
const bars = () => Array.from(img().querySelectorAll("rect[data-signal]"));

describe("CnoBars", () => {
  it("draws one bar per signal, grouped by system order", () => {
    render(<CnoBars sats={sampleState().sats} />);
    const found = img().querySelectorAll("rect[data-signal]");
    expect(found).toHaveLength(8); // one L1 signal per fixture satellite
    const order = Array.from(found).map((b) => b.getAttribute("data-signal")!.split(":")[0]);
    expect(order.slice(0, 3)).toEqual(["GPS", "GPS", "GPS"]);
    expect(order[3]).toBe("GLONASS");
    expect(order).toEqual(["GPS", "GPS", "GPS", "GLONASS", "GLONASS", "Galileo", "BeiDou", "QZSS"]);
  });

  it("scales height by C/N0", () => {
    const sats = sampleState().sats;
    render(<CnoBars sats={sats} height={200} />);
    const strong = img().querySelector('rect[data-signal="GPS:5:L1C/A"]')!;
    const weak = img().querySelector('rect[data-signal="GPS:25:L1C/A"]')!;
    expect(Number(strong.getAttribute("height"))).toBeGreaterThan(Number(weak.getAttribute("height")));
  });

  it("uses a fixed 0–55 dB-Hz scale, so 55 fills the plot and 27.5 is half of it", () => {
    const sats = [sat(0, "GPS", 1, 55, 40, 100), sat(0, "GPS", 2, 27.5, 40, 100), sat(0, "GPS", 3, 80, 40, 100)];
    render(<CnoBars sats={sats} height={200} />);
    const full = Number(img().querySelector('rect[data-signal="GPS:1:L1C/A"]')!.getAttribute("height"));
    const half = Number(img().querySelector('rect[data-signal="GPS:2:L1C/A"]')!.getAttribute("height"));
    const over = Number(img().querySelector('rect[data-signal="GPS:3:L1C/A"]')!.getAttribute("height"));
    expect(half).toBeCloseTo(full / 2, 5);
    expect(over).toBe(full); // clamped
  });

  it("counts every signal of a satellite, ordered by SV id within a system", () => {
    const sats = [dualSat(12, 40, 35), dualSat(5, 45, 41), sat(6, "GLONASS", 3, 40, 50, 40)];
    render(<CnoBars sats={sats} />);
    expect(bars().map((b) => b.getAttribute("data-signal"))).toEqual(["GPS:5:L1C/A", "GPS:5:L2C", "GPS:12:L1C/A", "GPS:12:L2C", "GLONASS:3:L1C/A"]);
  });

  it("fills every bar with the system colour and dims second and later signals to 55 %", () => {
    render(<CnoBars sats={[dualSat(5, 45, 41)]} />);
    const [l1, l2] = bars();
    expect(l1.getAttribute("fill")).toBe("var(--sys-gps)");
    expect(l2.getAttribute("fill")).toBe("var(--sys-gps)");
    expect(l1.getAttribute("fill-opacity")).toBe("1");
    expect(l2.getAttribute("fill-opacity")).toBe("0.55");
    // a 2 px surface gap between neighbouring bars
    const gap = Number(l2.getAttribute("x")) - (Number(l1.getAttribute("x")) + Number(l1.getAttribute("width")));
    expect(gap).toBeCloseTo(2, 5);
  });

  it("draws 20 and 40 dB-Hz reference lines in ink, labelled, above the baseline", () => {
    render(<CnoBars sats={sampleState().sats} height={200} />);
    const refs = Array.from(img().querySelectorAll("line[data-ref]"));
    expect(refs.map((l) => l.getAttribute("data-ref"))).toEqual(["20", "40"]);
    for (const l of refs) {
      expect(l.getAttribute("stroke")).toBe("var(--line)");
      expect(l.getAttribute("stroke")).not.toMatch(/brass/);
    }
    const y20 = Number(refs[0].getAttribute("y1"));
    const y40 = Number(refs[1].getAttribute("y1"));
    expect(y40).toBeLessThan(y20); // higher C/N0 sits higher up the plot
    expect(img()).toHaveTextContent("20");
    expect(img()).toHaveTextContent("40");
    // the reference lines are the only brass-free hairlines; nothing in the chart is brass
    expect(img().innerHTML).not.toMatch(/brass/);
  });

  it("names the image with a summary of signals, satellites and used count", () => {
    render(<CnoBars sats={sampleState().sats} />);
    expect(img()).toHaveAttribute("aria-label", "Signal strength: 8 signals from 8 satellites, 6 used");
  });

  it("gives every bar a title with satellite, signal, C/N0 and whether it is used", () => {
    render(<CnoBars sats={sampleState().sats} />);
    const strong = img().querySelector('rect[data-signal="GPS:5:L1C/A"]')!;
    expect(strong.querySelector("title")).toHaveTextContent("G5 L1C/A · 45 dB-Hz · used");
    const tracked = img().querySelector('rect[data-signal="GPS:25:L1C/A"]')!;
    expect(tracked.querySelector("title")).toHaveTextContent("G25 L1C/A · 22 dB-Hz · tracked");
  });

  it("renders a tracked signal with no C/N0 as a hairline and still counts it", () => {
    const sats = [sat(0, "GPS", 5, 45, 72, 120), sat(0, "GPS", 7, 0, 10, 200, false)];
    render(<CnoBars sats={sats} height={200} />);
    const none = img().querySelector('rect[data-signal="GPS:7:L1C/A"]')!;
    expect(none).not.toBeNull();
    expect(Number(none.getAttribute("height"))).toBe(1);
    expect(none).toHaveAttribute("data-no-signal", "true");
    expect(none.querySelector("title")).toHaveTextContent("no signal");
    expect(img()).toHaveAttribute("aria-label", "Signal strength: 2 signals from 2 satellites, 1 used");
  });

  it("shows a legend of the systems present and a table alternative", () => {
    render(<CnoBars sats={sampleState().sats} />);
    const legend = screen.getByRole("list", { name: "Systems" });
    expect(legend).toHaveTextContent("GPS");
    expect(legend).toHaveTextContent("QZSS");
    expect(legend).not.toHaveTextContent("SBAS");
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("G5");
    expect(table).toHaveTextContent("45 dB-Hz");
  });

  it("transitions bar geometry only when motion is allowed", () => {
    render(<CnoBars sats={sampleState().sats} />);
    const bar = bars()[0];
    expect(bar.getAttribute("class")).toMatch(/motion-safe:/);
    expect(bar.getAttribute("style") ?? "").not.toMatch(/transition/);
  });

  it("says so plainly when there is nothing to draw", () => {
    render(<CnoBars sats={[]} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText(/no signals tracked yet/i)).toBeInTheDocument();
  });
});
