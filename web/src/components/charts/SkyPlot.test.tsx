import { render, screen, within } from "@testing-library/react";
import { sampleState, sat } from "@/test/fixtures";
import { SkyPlot, radiusForCno } from "./SkyPlot";

describe("SkyPlot", () => {
  it("draws one disc per satellite with elevation mapped to radius", () => {
    const sats = sampleState().sats;
    render(<SkyPlot sats={sats} size={300} />);
    const img = screen.getByRole("img", { name: /8 satellites, 6 used/ });
    const discs = img.querySelectorAll("circle[data-sat]");
    expect(discs).toHaveLength(8);
    const zenith = img.querySelector('circle[data-sat="GPS-5"]')!; // elev 72 → close to centre
    const horizon = img.querySelector('circle[data-sat="GPS-25"]')!; // elev 8 → near the outer ring
    const dist = (c: Element) => Math.hypot(Number(c.getAttribute("cx")) - 150, Number(c.getAttribute("cy")) - 150);
    expect(dist(zenith)).toBeLessThan(dist(horizon));
    expect(horizon.getAttribute("fill")).toBe("none"); // tracked only → hollow
    expect(zenith.getAttribute("fill")).toBe("var(--sys-gps)");
  });

  it("places azimuth 90° to the east (right of centre)", () => {
    const sats = [{ ...sampleState().sats[0], elev: 45, azim: 90, gnss: "GPS", sv_id: 1 }];
    render(<SkyPlot sats={sats} size={200} />);
    const disc = screen.getByRole("img").querySelector("circle[data-sat]")!;
    expect(Number(disc.getAttribute("cx"))).toBeGreaterThan(100);
    expect(Math.abs(Number(disc.getAttribute("cy")) - 100)).toBeLessThan(1);
  });

  it("shows an empty message without satellites", () => {
    render(<SkyPlot sats={[]} />);
    expect(screen.getByText(/no satellites tracked/i)).toBeInTheDocument();
  });

  it("maps C/N0 0–55 dB-Hz onto a 3.5–8 px disc radius, clamped", () => {
    expect(radiusForCno(0)).toBeCloseTo(3.5, 6);
    expect(radiusForCno(55)).toBeCloseTo(8, 6);
    expect(radiusForCno(27.5)).toBeCloseTo(5.75, 6);
    expect(radiusForCno(-5)).toBeCloseTo(3.5, 6);
    expect(radiusForCno(70)).toBeCloseTo(8, 6);
    render(<SkyPlot sats={sampleState().sats} size={300} />);
    const img = screen.getByRole("img");
    expect(Number(img.querySelector('circle[data-sat="GPS-5"]')!.getAttribute("r"))).toBeCloseTo(3.5 + 4.5 * (45 / 55), 3);
    expect(Number(img.querySelector('circle[data-sat="GPS-25"]')!.getAttribute("r"))).toBeCloseTo(3.5 + 4.5 * (22 / 55), 3);
  });

  it("titles every disc with the RINEX id, elevation, azimuth, C/N0 and use", () => {
    render(<SkyPlot sats={sampleState().sats} size={300} />);
    const img = screen.getByRole("img");
    expect(img.querySelector('circle[data-sat="GPS-12"] title')).toHaveTextContent("G12 · 35° el · 210° az · 38 dB-Hz · used");
    expect(img.querySelector('circle[data-sat="QZSS-194"] title')).toHaveTextContent("J194 · 15° el · 95° az · 28 dB-Hz · tracked");
    expect(img.querySelector('circle[data-sat="BeiDou-21"] title')).toHaveTextContent(/^C21 /);
  });

  it("draws the three elevation rings in brass and nothing else in brass", () => {
    render(<SkyPlot sats={sampleState().sats} size={300} />);
    const img = screen.getByRole("img");
    const rings = img.querySelectorAll("circle[data-ring]");
    expect([...rings].map((r) => r.getAttribute("data-ring"))).toEqual(["0", "30", "60"]);
    for (const r of rings) expect(r.getAttribute("stroke")).toBe("var(--brass)");
    const brassElsewhere = [...img.querySelectorAll("*:not([data-ring])")].filter((el) => (el.getAttribute("stroke") ?? "").includes("brass") || (el.getAttribute("fill") ?? "").includes("brass"));
    expect(brassElsewhere).toHaveLength(0);
    expect(img).toHaveTextContent("N");
    expect(img).toHaveTextContent("30°");
  });

  it("skips satellites without an elevation or azimuth but still counts them", () => {
    const sats = [...sampleState().sats, sat(0, "GPS", 31, 20, null, null, false), sat(6, "GLONASS", 17, 25, -91, 12, false)];
    render(<SkyPlot sats={sats} size={300} />);
    const img = screen.getByRole("img", { name: /10 satellites, 6 used/ });
    expect(img.querySelectorAll("circle[data-sat]")).toHaveLength(8);
    expect(screen.getByText(/2 tracked without a position are counted, not drawn/)).toBeInTheDocument();
  });

  it("animates disc moves only when motion is allowed", () => {
    render(<SkyPlot sats={sampleState().sats} size={300} />);
    const disc = screen.getByRole("img").querySelector("circle[data-sat]")!;
    expect(disc.getAttribute("class")).toMatch(/motion-safe:/);
    expect(disc.getAttribute("style") ?? "").not.toMatch(/transition/);
  });

  it("offers a legend with per-system counts and a table alternative", () => {
    render(<SkyPlot sats={sampleState().sats} size={300} />);
    const legend = screen.getByRole("list", { name: /systems/i });
    expect(within(legend).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["GPS2/3", "GLONASS2/2", "Galileo1/1", "BeiDou1/1", "QZSS0/1"]);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(9); // header + 8
    expect(within(table).getByText("G5")).toBeInTheDocument();
  });
});
