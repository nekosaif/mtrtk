import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { FeatureCollection } from "geojson";
import { maps, resetMaplibreMock } from "@/test/maplibreMock";
import { TrackMap } from "./TrackMap";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

const TRACK: FeatureCollection = {
  type: "FeatureCollection",
  features: [
    { type: "Feature", geometry: { type: "LineString", coordinates: [[90.26, 23.83, 10], [90.27, 23.84, 11]] }, properties: { q: 2, epochs: 2 } },
    { type: "Feature", geometry: { type: "LineString", coordinates: [[90.27, 23.84, 11], [90.28, 23.85, 12]] }, properties: { q: 1, epochs: 58 } },
  ],
};
const EVENTS: FeatureCollection = {
  type: "FeatureCollection",
  features: [{ type: "Feature", geometry: { type: "Point", coordinates: [90.265, 23.835, 10] }, properties: { count: 7, status: "ok" } }],
};

describe("TrackMap", () => {
  beforeEach(() => resetMaplibreMock());

  it("colours the track by Q with the status palette and draws the events as circles", () => {
    render(<TrackMap track={TRACK} events={EVENTS} />);
    maps[0].fire("style.load");
    expect(maps[0].layers).toEqual(["track", "events"]);
    const track = maps[0].layerSpecs.get("track")!;
    // index.css's fixed status values: good, warning, serious (DGPS/SBAS), sys-galileo (PPP), critical.
    expect((track.paint as Record<string, unknown>)["line-color"]).toEqual(["match", ["get", "q"], 1, "#0ca30c", 2, "#fab219", 3, "#ec835a", 4, "#ec835a", 6, "#199e70", "#d03b3b"]);
    expect(track.filter).toEqual(["==", ["geometry-type"], "LineString"]);
    expect(maps[0].layerSpecs.get("events")!.paint).toMatchObject({ "circle-radius": 4, "circle-color": "#e0b25a", "circle-stroke-color": "#0f1420" });
    expect(maps[0].getSource("events")!.data).toBe(EVENTS);
  });

  it("draws events that arrive after the style has loaded", () => {
    const { rerender } = render(<TrackMap track={TRACK} events={null} />);
    maps[0].fire("style.load");
    expect(maps[0].layers).toEqual(["track"]);
    rerender(<TrackMap track={TRACK} events={EVENTS} />);
    expect(maps[0].layers).toEqual(["track", "events"]);
    expect(maps[0].getSource("events")!.data).toBe(EVENTS);
  });

  it("waits for the style before adding events, and redraws everything after a Map/Imagery switch", async () => {
    const { rerender } = render(<TrackMap track={TRACK} events={null} />);
    rerender(<TrackMap track={TRACK} events={EVENTS} />);
    expect(maps[0].layers).toEqual([]); // maplibre refuses a source before its style has loaded
    maps[0].fire("style.load");
    expect(maps[0].layers).toEqual(["track", "events"]);
    await userEvent.click(screen.getByRole("button", { name: "Imagery" }));
    expect(maps[0].layers).toEqual([]);
    maps[0].fire("style.load");
    expect(maps[0].layers).toEqual(["track", "events"]);
  });
});
