import { fireEvent, render, screen, within } from "@testing-library/react";
import type { Spectrum as SpectrumT } from "@/lib/types";
import { Spectrum, BLOCK_COLORS } from "./Spectrum";

const block = (block_id: number, center_hz: number, peakAt = 128): SpectrumT => ({
  block_id,
  span_hz: 100_000_000,
  res_hz: 390_625,
  center_hz,
  pga_db: 20 + block_id,
  bins: Array.from({ length: 256 }, (_, i) => 60 + Math.round(30 * Math.exp(-((i - peakAt) ** 2) / 800))),
});

describe("Spectrum", () => {
  // B5 — brass is the accent and the constellation hues mean a constellation: an RF block is
  // neither, so the strips take the series palette.
  it("draws one polyline per block in the series palette, never brass or a constellation colour", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000), block(1, 1_230_000_000)]} />);
    const img = screen.getByRole("img", { name: /RF spectrum: 2 blocks/ });
    const lines = img.querySelectorAll("polyline[data-block]");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toHaveAttribute("data-block", "0");
    expect(lines[0]).toHaveAttribute("stroke", BLOCK_COLORS[0]);
    expect(BLOCK_COLORS[0]).toBe("var(--series-1)");
    expect(lines[1]).toHaveAttribute("stroke", BLOCK_COLORS[1]);
    expect(BLOCK_COLORS[1]).toBe("var(--series-2)");
    expect(img.innerHTML).not.toMatch(/brass|--sys-|--status-/);
    // every polyline has its own title, and the whole chart has a legend for two series
    expect(within(img).getByText(/RF block 0: 1530\.0–1630\.0 MHz/)).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "RF blocks" })).toHaveTextContent("RF block 0");
    expect(screen.getByRole("list", { name: "RF blocks" })).toHaveTextContent("PGA 21 dB");
  });

  it("labels the x axis in MHz from centre minus half the span to centre plus half the span", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000)]} />);
    const img = screen.getByRole("img");
    const ticks = [...img.querySelectorAll("text[data-tick]")].map((t) => t.textContent);
    expect(ticks).toEqual(["1530.0 MHz", "1580.0 MHz", "1630.0 MHz"]);
    // the y grid is the 0–255 bin scale
    expect([...img.querySelectorAll("text[data-level]")].map((t) => t.textContent)).toEqual(["64", "128", "192"]);
  });

  it("shows a crosshair readout with MHz and level on hover, per block", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000), block(1, 1_230_000_000, 64)]} />);
    const img = screen.getByRole("img");
    // jsdom has no layout: getBoundingClientRect is all zeros, so the component falls back to its drawn width
    // (640 px, plot from x=34 to x=632); the plot's middle is the centre bin
    fireEvent.pointerMove(img, { clientX: 333, clientY: 50, pointerType: "mouse" });
    const readout = screen.getByTestId("spectrum-readout");
    expect(readout).toHaveTextContent(/1580\.\d\d MHz · 90/);
    expect(readout).toHaveTextContent(/1230\.\d\d MHz · 6\d/);
    expect(img.querySelector("line[data-crosshair]")).not.toBeNull();
    fireEvent.pointerLeave(img, { pointerType: "mouse" });
    expect(img.querySelector("line[data-crosshair]")).toBeNull();
    expect(readout).toHaveTextContent(/amplitude 0–255/);
  });

  // D1 — u-blox MON-SPAN: f(i) = center + span · (i − 128) / 256, so the bin step is `res_hz`
  // (span / 256), not span / (bins − 1), and bin 128 sits exactly on the centre frequency.
  it("places bin i at centre + span · (i − 128) / 256, stepping by res_hz", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000, 200)]} />);
    const img = screen.getByRole("img");
    // the first bin at the peak level is 197 (rounding flattens the top): 1530 + 197 × 0.390625 =
    // 1606.95 MHz, where span / 255 would say 1607.25
    expect(within(img).getByText(/RF block 0: .*peak 90 at 1606\.95 MHz/)).toBeInTheDocument();
    // the centre bin is drawn on the centre tick, half-way across the plot
    const pts = img.querySelector("polyline[data-block]")!.getAttribute("points")!.split(" ");
    const [x128] = pts[128].split(",").map(Number);
    expect(x128).toBeCloseTo(34 + (640 - 34 - 8) / 2, 0);
  });

  // A message without a resolution steps by span / bins (still span / 256), never span / (bins − 1)
  // and never zero (which would stack every bin on the start frequency).
  it("steps by span / bins when the message carries no res_hz", () => {
    render(<Spectrum spectra={[{ ...block(0, 1_580_000_000, 200), res_hz: 0 }]} />);
    const img = screen.getByRole("img");
    expect(within(img).getByText(/RF block 0: .*peak 90 at 1606\.95 MHz/)).toBeInTheDocument();
    const pts = img.querySelector("polyline[data-block]")!.getAttribute("points")!.split(" ");
    const [x128] = pts[128].split(",").map(Number);
    expect(x128).toBeCloseTo(34 + (640 - 34 - 8) / 2, 0);
  });

  it("offers the bins as a table", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000)]} />);
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows.length).toBeGreaterThan(10);
    expect(rows[1]).toHaveTextContent("1530.0");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Block", "MHz", "Level"]);
  });

  it("says so when there is nothing to draw", () => {
    render(<Spectrum spectra={[]} />);
    expect(screen.getByText(/no spectrum data yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("answers a finger as well as a mouse, and keeps the reading after the finger lifts", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000)]} />);
    const img = screen.getByRole("img");
    expect(img.getAttribute("class")).toContain("touch-pan-y");
    fireEvent.pointerDown(img, { clientX: 333, clientY: 50, pointerType: "touch" });
    fireEvent.pointerLeave(img, { pointerType: "touch" });
    expect(screen.getByTestId("spectrum-readout")).toHaveTextContent(/1580\.\d\d MHz · 90/);
  });
});
