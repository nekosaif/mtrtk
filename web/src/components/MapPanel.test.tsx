import { act, fireEvent, render, screen } from "@testing-library/react";
import { sampleRover } from "@/test/fixtures";
import { maps, markers, resetMaplibreMock } from "@/test/maplibreMock";
import { MapPanel } from "./MapPanel";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

describe("MapPanel", () => {
  beforeEach(() => resetMaplibreMock());
  afterEach(() => {
    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
  });

  it("creates one map on OSM raster tiles with attribution, centred on the base", () => {
    render(<MapPanel lat={23.8373506} lon={90.2625502} hAcc={0.012} />);
    expect(maps).toHaveLength(1);
    const style = maps[0].style as { sources: Record<string, { tiles: string[]; attribution: string }> };
    expect(style.sources.basemap.tiles[0]).toContain("openstreetmap.org");
    expect(style.sources.basemap.attribution).toMatch(/OpenStreetMap/);
    expect(maps[0].options.attributionControl).not.toBe(false);
    expect(maps[0].easeCalls).toHaveLength(1);
    const base = document.querySelector('[data-marker="base"]');
    expect(base).not.toBeNull();
    expect(markers[0].lngLat).toEqual([90.2625502, 23.8373506]);
  });

  it("moves the base marker on a new position without recentring, and recentres on request", () => {
    const { rerender } = render(<MapPanel lat={23.8373506} lon={90.2625502} hAcc={0.012} />);
    rerender(<MapPanel lat={23.8373606} lon={90.2625602} hAcc={0.02} />);
    expect(markers).toHaveLength(1);
    expect(markers[0].lngLat).toEqual([90.2625602, 23.8373606]);
    expect(maps[0].easeCalls).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: /centre/i }));
    expect(maps[0].easeCalls).toHaveLength(2);
    expect((maps[0].easeCalls[1] as { center: [number, number] }).center).toEqual([90.2625602, 23.8373606]);
  });

  it("toggles between the map and imagery styles with a pressed state", () => {
    render(<MapPanel lat={1} lon={2} hAcc={null} />);
    const imagery = screen.getByRole("button", { name: "Imagery" });
    const map = screen.getByRole("button", { name: "Map" });
    expect(map).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(imagery);
    expect(imagery).toHaveAttribute("aria-pressed", "true");
    expect(map).toHaveAttribute("aria-pressed", "false");
    expect((maps[0].style as { sources: Record<string, { tiles: string[] }> }).sources.basemap.tiles[0]).toContain("arcgisonline");
    fireEvent.click(map);
    expect((maps[0].style as { sources: Record<string, { tiles: string[] }> }).sources.basemap.tiles[0]).toContain("openstreetmap");
  });

  it("draws a marker per rover with a GGA position and drops the ones that leave", () => {
    const { rerender } = render(<MapPanel lat={1} lon={2} hAcc={null} rovers={[sampleRover(), sampleRover({ id: 2, ip: "10.0.0.2", user_agent: "SNIP" })]} />);
    expect(document.querySelectorAll('[data-marker="rover"]')).toHaveLength(2);
    rerender(<MapPanel lat={1} lon={2} hAcc={null} rovers={[sampleRover({ id: 2, ip: "10.0.0.2", user_agent: "SNIP", last_gga_lat: 1.5 })]} />);
    const left = document.querySelectorAll('[data-marker="rover"]');
    expect(left).toHaveLength(1);
    expect(left[0].getAttribute("title")).toContain("SNIP");
    rerender(<MapPanel lat={1} lon={2} hAcc={null} rovers={[]} />);
    expect(document.querySelectorAll('[data-marker="rover"]')).toHaveLength(0);
  });

  it("fills a flex parent without an explicit height, and takes the height when given one", () => {
    const { rerender } = render(<MapPanel lat={1} lon={2} hAcc={null} />);
    const frame = screen.getByTestId("map-frame");
    expect(frame.className).toContain("flex-1");
    expect(frame.className).toContain("min-h-[320px]");
    expect(frame.style.height).toBe("");
    rerender(<MapPanel lat={1} lon={2} hAcc={null} height={240} />);
    expect(frame.style.height).toBe("240px");
  });

  it("says it is waiting for a fix without a position and draws no base marker", () => {
    render(<MapPanel lat={null} lon={null} hAcc={null} />);
    expect(screen.getByText(/waiting for a position fix/i)).toBeInTheDocument();
    expect(document.querySelector('[data-marker="base"]')).toBeNull();
  });

  it("starts on the grid when the browser is offline and recovers on the online event", () => {
    Object.defineProperty(navigator, "onLine", { value: false, configurable: true });
    render(<MapPanel lat={1} lon={2} hAcc={0.5} />);
    const frame = screen.getByTestId("map-frame");
    expect(frame).toHaveAttribute("data-offline", "true");
    expect(document.querySelector('[data-marker="base"]')).not.toBeNull();
    Object.defineProperty(navigator, "onLine", { value: true, configurable: true });
    act(() => { window.dispatchEvent(new Event("online")); });
    expect(frame).toHaveAttribute("data-offline", "false");
    act(() => { window.dispatchEvent(new Event("offline")); });
    expect(frame).toHaveAttribute("data-offline", "true");
  });

  it("ignores map errors that are not resource failures", () => {
    render(<MapPanel lat={1} lon={2} hAcc={0.5} />);
    act(() => maps[0].fire("error", { error: new Error("layer 'x' does not exist") }));
    expect(screen.getByTestId("map-frame")).toHaveAttribute("data-offline", "false");
    act(() => maps[0].fire("error", { error: { status: 503, url: "https://tile.openstreetmap.org/1/0/0.png" } }));
    expect(screen.getByTestId("map-frame")).toHaveAttribute("data-offline", "true");
  });

  it("removes the map and its listeners on unmount", () => {
    const { unmount } = render(<MapPanel lat={1} lon={2} hAcc={0.5} />);
    unmount();
    expect(maps[0].removed).toBe(true);
  });
});
