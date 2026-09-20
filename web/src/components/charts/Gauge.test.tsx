import { render, screen } from "@testing-library/react";
import { Gauge } from "./Gauge";

describe("Gauge", () => {
  it("is a meter whose bar width is the value's share of max", () => {
    render(<Gauge label="Jamming indicator" value={12} max={255} />);
    const meter = screen.getByRole("meter", { name: "Jamming indicator" });
    expect(meter).toHaveAttribute("aria-valuemin", "0");
    expect(meter).toHaveAttribute("aria-valuemax", "255");
    expect(meter).toHaveAttribute("aria-valuenow", "12");
    expect(meter.querySelector("[data-gauge-fill]")).toHaveStyle({ width: "4.7%" });
    expect(screen.getByText("12").className.split(/\s+/)).toContain("num");
  });

  it("scales to the AGC range and clamps past the ends", () => {
    const { rerender } = render(<Gauge label="AGC" value={4095.5} max={8191} />);
    expect(screen.getByRole("meter").querySelector("[data-gauge-fill]")).toHaveStyle({ width: "50.0%" });
    rerender(<Gauge label="AGC" value={9000} max={8191} />);
    expect(screen.getByRole("meter").querySelector("[data-gauge-fill]")).toHaveStyle({ width: "100.0%" });
    rerender(<Gauge label="AGC" value={-3} max={8191} />);
    expect(screen.getByRole("meter").querySelector("[data-gauge-fill]")).toHaveStyle({ width: "0.0%" });
  });

  it("is neutral ink without a level and takes the status colour with one; width only eases under motion-safe", () => {
    const { rerender } = render(<Gauge label="AGC" value={1} max={2} />);
    let fill = screen.getByRole("meter").querySelector("[data-gauge-fill]") as HTMLElement;
    expect(fill.style.background).toBe("var(--ink-2)");
    expect(fill.className).toContain("motion-safe:transition-[width]");
    expect(fill.style.transition).toBe("");
    rerender(<Gauge label="AGC" value={1} max={2} level="critical" />);
    fill = screen.getByRole("meter").querySelector("[data-gauge-fill]") as HTMLElement;
    expect(fill.style.background).toBe("var(--status-critical)");
  });

  it("formats the value text when asked", () => {
    render(<Gauge label="Noise" value={90} max={200} format={(v) => `${v} /ms`} />);
    expect(screen.getByText("90 /ms")).toBeInTheDocument();
  });
});
