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
    fireEvent.pointerMove(img, { clientX: 2, clientY: 10, pointerType: "mouse" });
    expect(screen.getByText("40 dB-Hz")).toBeInTheDocument();
    expect(img.querySelector("line[data-crosshair]")).not.toBeNull();
    fireEvent.pointerLeave(img, { pointerType: "mouse" });
    expect(img.querySelector("line[data-crosshair]")).toBeNull();
    expect(screen.getByText("44 dB-Hz")).toBeInTheDocument();
  });

  // D5 — the Receiver's jam/AGC trends sample on MON-RF arrival, so a reconnect gap drawn by ring
  // index read as a smooth slope in the very panel opened to diagnose interference.
  it("spaces the points by their timestamps when given them", () => {
    render(<Sparkline label="Jamming trend" values={[10, 20, 30]} times={[0, 1000, 4000]} format={String} width={240} />);
    const xs = screen.getByRole("img").querySelector("polyline")!.getAttribute("points")!.split(" ").map((p) => Number(p.split(",")[0]));
    expect(xs).toEqual([0, 60, 240]);
  });

  it("breaks the line where the series went silent instead of drawing a slope across the gap", () => {
    const times = [0, 1000, 2000, 3000, 60_000, 61_000, 62_000];
    render(<Sparkline label="AGC trend" values={[1, 2, 3, 4, 5, 6, 7]} times={times} format={String} />);
    const lines = screen.getByRole("img").querySelectorAll("polyline");
    expect(lines).toHaveLength(2);
    expect(lines[0].getAttribute("points")!.split(" ")).toHaveLength(4);
    expect(lines[1].getAttribute("points")!.split(" ")).toHaveLength(3);
  });

  const xsOf = () => screen.getByRole("img").querySelector("polyline")!.getAttribute("points")!.split(" ").map((p) => Number(p.split(",")[0]));

  it("falls back to even spacing when the timestamps span no time (a frozen clock), never NaN", () => {
    render(<Sparkline label="Jamming trend" values={[10, 20, 30]} times={[5000, 5000, 5000]} format={String} width={240} />);
    expect(xsOf()).toEqual([0, 120, 240]);
    expect(screen.getAllByRole("img")[0].querySelectorAll("polyline")).toHaveLength(1);
  });

  it("falls back to even spacing when the times do not line up with the values", () => {
    render(<Sparkline label="Jamming trend" values={[10, 20, 30]} times={[0, 4000]} format={String} width={240} />);
    expect(xsOf()).toEqual([0, 120, 240]);
  });

  it("draws a lone sample between two silences as a dot", () => {
    const times = [0, 1000, 2000, 60_000, 120_000, 121_000, 122_000];
    render(<Sparkline label="AGC trend" values={[1, 2, 3, 4, 5, 6, 7]} times={times} format={String} width={244} />);
    const img = screen.getByRole("img");
    expect(img.querySelectorAll("polyline")).toHaveLength(2);
    const dots = img.querySelectorAll("circle");
    expect(dots).toHaveLength(1);
    expect(Number(dots[0].getAttribute("cx"))).toBeCloseTo((60_000 / 122_000) * 244, 5);
  });
});
