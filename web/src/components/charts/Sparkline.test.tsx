import { fireEvent, render, screen } from "@testing-library/react";
import { Sparkline } from "./Sparkline";

describe("Sparkline", () => {
  it("draws one polyline and titles it with min, max and last", () => {
    render(<Sparkline label="Horizontal accuracy" values={[0.012, 0.015, 0.011, 0.013]} format={(v) => `${(v * 100).toFixed(1)} cm`} />);
    const img = screen.getByRole("img", { name: /horizontal accuracy/i });
    expect(img.querySelectorAll("polyline")).toHaveLength(1);
    expect(img.querySelector("title")).toHaveTextContent("Horizontal accuracy: min 1.1 cm, max 1.5 cm, last 1.3 cm");
    expect(img.querySelectorAll("line, text")).toHaveLength(0); // an inline readout: no axes
    expect(screen.getByText("1.3 cm").className).toContain("num");
  });

  it("says it is collecting until two points exist", () => {
    render(<Sparkline label="Satellites used" values={[6]} format={(v) => v.toFixed(0)} />);
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText(/collecting/i)).toBeInTheDocument();
  });

  it("ignores gaps and shows the hovered value with a crosshair", () => {
    render(<Sparkline label="Mean C/N0" values={[40, null, 42, 44]} format={(v) => `${v.toFixed(0)} dB-Hz`} />);
    const img = screen.getByRole("img");
    img.getBoundingClientRect = () => ({ left: 0, top: 0, width: 240, height: 40, right: 240, bottom: 40, x: 0, y: 0, toJSON: () => ({}) });
    expect(img.querySelector("polyline")!.getAttribute("points")!.split(" ")).toHaveLength(3);
    fireEvent.mouseMove(img, { clientX: 2, clientY: 10 });
    expect(screen.getByText("40 dB-Hz")).toBeInTheDocument();
    expect(img.querySelector("line[data-crosshair]")).not.toBeNull();
    fireEvent.mouseLeave(img);
    expect(img.querySelector("line[data-crosshair]")).toBeNull();
    expect(screen.getByText("44 dB-Hz")).toBeInTheDocument();
  });
});
