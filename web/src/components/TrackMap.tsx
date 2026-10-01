import maplibregl, { type GeoJSONSource, type Map as MlMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { FeatureCollection } from "geojson";
import { WifiOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { BASEMAP_SOURCE, IMAGERY, OSM, isResourceFailure } from "@/components/MapPanel";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// The map is not a themed surface: the status palette's fixed values (index.css), by RTKLIB Q,
// as QualityStrip draws them: 1 fixed good, 2 float warning, 3/4 SBAS/DGPS serious, 6 PPP the
// Galileo colour, anything else (5 single) critical.
const LINE_COLOR = ["match", ["get", "q"], 1, "#0ca30c", 2, "#fab219", 3, "#ec835a", 4, "#ec835a", 6, "#199e70", "#d03b3b"] as const;
const NAVY = "#0f1420";

/** The camera events on `map`, whose style has loaded: drawn, or their data replaced. */
function drawEvents(map: MlMap, ev: FeatureCollection | null | undefined): void {
  if (!ev || ev.features.length === 0) return;
  const source = map.getSource("events") as GeoJSONSource | undefined;
  if (source) {
    source.setData(ev);
    return;
  }
  map.addSource("events", { type: "geojson", data: ev });
  map.addLayer({ id: "events", type: "circle", source: "events", paint: { "circle-radius": 4, "circle-color": "#e0b25a", "circle-stroke-color": NAVY, "circle-stroke-width": 1 } });
}

/** [west, south, east, north] of every coordinate in `fc`, or null when it has none. */
export function trackBounds(fc: FeatureCollection | null | undefined): [number, number, number, number] | null {
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  const visit = (c: unknown): void => {
    if (!Array.isArray(c)) return;
    if (typeof c[0] === "number" && typeof c[1] === "number") {
      w = Math.min(w, c[0]);
      e = Math.max(e, c[0]);
      s = Math.min(s, c[1]);
      n = Math.max(n, c[1]);
      return;
    }
    for (const x of c) visit(x);
  };
  for (const f of fc?.features ?? []) if (f.geometry && "coordinates" in f.geometry) visit(f.geometry.coordinates);
  return Number.isFinite(w) ? [w, s, e, n] : null;
}

/**
 * A PPK track on a map: the solution's same-quality runs as a line coloured by Q (fixed green,
 * float amber, DGPS/SBAS orange, else red), camera events as small circles, the view fitted to the track. OSM
 * raster tiles with an Esri imagery alternative; without tiles (offline field laptop) the frame
 * shows a grid and the track still draws. Mounted once per job (`key` it by the job id): a
 * finished job's track never changes, so it is read once. The events come from their own
 * request and may arrive after the style has loaded: they are drawn whenever they come.
 */
export function TrackMap({ track, events, height = 360 }: { track: FeatureCollection; events?: FeatureCollection | null; height?: number }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const data = useRef({ track, events });
  data.current = { track, events };
  // Set by each `style.load`, cleared by each style switch: maplibre refuses sources before it.
  // Not `map.isStyleLoaded()`, which stays false while the basemap's tiles are still loading.
  const styleReady = useRef(false);
  const [imagery, setImagery] = useState(false);
  const [offline, setOffline] = useState(() => typeof navigator !== "undefined" && navigator.onLine === false);

  useEffect(() => {
    if (!container.current) return;
    const map = new maplibregl.Map({ container: container.current, style: OSM, center: [0, 0], zoom: 1, attributionControl: { compact: true } });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("error", (e) => {
      if (isResourceFailure(e)) setOffline(true);
    });
    map.on("sourcedata", (e) => {
      if (e.sourceId === BASEMAP_SOURCE && e.tile) setOffline(false);
    });
    // Every style load (the first, and each Map/Imagery switch) starts without overlays.
    map.on("style.load", () => {
      styleReady.current = true;
      const { track: t, events: ev } = data.current;
      if (!map.getSource("track")) map.addSource("track", { type: "geojson", data: t });
      if (!map.getLayer("track")) {
        map.addLayer({
          id: "track",
          type: "line",
          source: "track",
          filter: ["==", ["geometry-type"], "LineString"],
          layout: { "line-join": "round", "line-cap": "round" },
          paint: { "line-width": 3, "line-color": LINE_COLOR as unknown as string },
        });
      }
      drawEvents(map, ev);
    });
    mapRef.current = map;
    const bounds = trackBounds(data.current.track);
    if (bounds) map.fitBounds(bounds, { padding: 40, duration: 0, maxZoom: 19 });
    return () => {
      map.remove();
      mapRef.current = null;
      styleReady.current = false;
    };
  }, []); // one map per mount: the page keys this component by job, whose files never change

  useEffect(() => {
    const map = mapRef.current;
    if (map && styleReady.current) drawEvents(map, events);
  }, [events]);

  const firstStyle = useRef(true);
  useEffect(() => {
    if (firstStyle.current) {
      firstStyle.current = false; // the map was created on OSM
      return;
    }
    styleReady.current = false; // until the new style's `style.load` redraws the overlays
    mapRef.current?.setStyle(imagery ? IMAGERY : OSM);
  }, [imagery]);

  return (
    <div data-testid="track-map" data-offline={offline} className={cn("relative overflow-hidden rounded-md", offline && "map-grid")} style={{ height, minHeight: height }}>
      <div ref={container} className="h-full w-full" />
      <div className="absolute top-2 left-2 flex gap-1">
        <Button type="button" size="sm" variant={imagery ? "outline" : "default"} aria-pressed={!imagery} onClick={() => setImagery(false)}>
          Map
        </Button>
        <Button type="button" size="sm" variant={imagery ? "default" : "outline"} aria-pressed={imagery} onClick={() => setImagery(true)}>
          Imagery
        </Button>
      </div>
      {offline ? (
        <div role="status" className="pointer-events-none absolute inset-x-2 bottom-2 flex items-center gap-2 rounded-md border border-line bg-panel/90 px-3 py-1.5 text-[12px] leading-4 text-ink-2">
          <WifiOff className="size-3.5 shrink-0" aria-hidden />
          Map tiles unavailable (offline). The track still draws.
        </div>
      ) : null}
    </div>
  );
}
