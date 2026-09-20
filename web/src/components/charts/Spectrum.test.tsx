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
  it("draws one polyline per block, block 0 brass and block 1 GPS blue", () => {
    render(<Spectrum spectra={[block(0, 1_580_000_000), block(1, 1_230_000_000)]} />);
    const img = screen.getByRole("img", { name: /RF spectrum: 2 blocks/ });
    const lines = img.querySelectorAll("polyline[data-block]");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toHaveAttribute("data-block", "0");
    expect(lines[0]).toHaveAttribute("stroke", BLOCK_COLORS[0]);
    expect(BLOCK_COLORS[0]).toBe("var(--brass)");
    expect(lines[1]).toHaveAttribute("stroke", BLOCK_COLORS[1]);
    expect(BLOCK_COLORS[1]).toBe("var(--sys-gps)");
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
    fireEvent.mouseMove(img, { clientX: 333, clientY: 50 });
    const readout = screen.getByTestId("spectrum-readout");
    expect(readout).toHaveTextContent(/1580\.\d\d MHz · 90/);
    expect(readout).toHaveTextContent(/1230\.\d\d MHz · 6\d/);
    expect(img.querySelector("line[data-crosshair]")).not.toBeNull();
    fireEvent.mouseLeave(img);
    expect(img.querySelector("line[data-crosshair]")).toBeNull();
    expect(readout).toHaveTextContent(/amplitude 0–255/);
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
});
