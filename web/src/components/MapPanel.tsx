import maplibregl, { type GeoJSONSource, type Map as MlMap, type StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { Feature, Polygon } from "geojson";
import { LocateFixed, WifiOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { NtripClient } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Both raster styles use this source id, so a loaded tile can be told from the GeoJSON overlay. */
export const BASEMAP_SOURCE = "basemap";

const OSM: StyleSpecification = {
  version: 8,
  sources: {
    [BASEMAP_SOURCE]: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  layers: [{ id: BASEMAP_SOURCE, type: "raster", source: BASEMAP_SOURCE }],
};
const IMAGERY: StyleSpecification = {
  version: 8,
  sources: {
    [BASEMAP_SOURCE]: {
      type: "raster",
      tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
      tileSize: 256,
      maxzoom: 19,
      attribution: "Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    },
  },
  layers: [{ id: BASEMAP_SOURCE, type: "raster", source: BASEMAP_SOURCE }],
};

// The map is not a themed surface (tiles have their own colours), so the overlay uses fixed
// ink/navy pairs that read on both OSM's light tiles and imagery's dark ones.
const NAVY = "#0f1420";
const OFF_WHITE = "#f2eee6";

function circlePolygon(lat: number, lon: number, radiusM: number): Feature<Polygon> {
  const pts: [number, number][] = [];
  const dLat = radiusM / 111_320;
  const dLon = radiusM / (111_320 * Math.cos((lat * Math.PI) / 180));
  for (let i = 0; i <= 64; i++) {
    const a = (i / 64) * 2 * Math.PI;
    pts.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]);
  }
  return { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [pts] } };
}

/** A tile or style request that failed: maplibre wraps these as AJAXError (status 0 when the network is down). */
function isResourceFailure(e: unknown): boolean {
  const err = (e as { error?: unknown } | null)?.error;
  return !!err && typeof err === "object" && ("status" in err || "url" in err);
}

function markerElement(kind: "base" | "rover", title?: string): HTMLDivElement {
  const el = document.createElement("div");
  el.dataset.marker = kind;
  if (kind === "base") {
    el.style.cssText = `width:14px;height:14px;border-radius:50%;background:${NAVY};border:2px solid ${OFF_WHITE};box-sizing:border-box`;
  } else {
    el.style.cssText = `width:11px;height:11px;border-radius:2px;background:${OFF_WHITE};border:2px solid ${NAVY};box-sizing:border-box`;
  }
  if (title) el.title = title;
  return el;
}

/**
 * The base on a map: OSM raster tiles with an Esri imagery alternative, the base position as a
 * marker with its horizontal-accuracy circle, and every NTRIP rover that has sent a GGA. When
 * tiles cannot load (the browser is offline, or the LAN has no route out) the frame shows a
 * grid instead and the markers keep drawing; it clears itself when a basemap tile arrives.
 */
export function MapPanel({
  lat,
  lon,
  hAcc,
  rovers = [],
  height,
  className,
}: {
  lat: number | null;
  lon: number | null;
  hAcc: number | null;
  rovers?: NtripClient[];
  /** Fixed height in px; without one the frame grows to fill a flex parent (320 px floor). */
  height?: number;
  className?: string;
}) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MlMap | null>(null);
  const baseMarker = useRef<maplibregl.Marker | null>(null);
  const roverMarkers = useRef<Map<number, maplibregl.Marker>>(new Map());
  const styleReady = useRef(false);
  const drawAccuracy = useRef<() => void>(() => {});
  const [imagery, setImagery] = useState(false);
  const [offline, setOffline] = useState(() => typeof navigator !== "undefined" && navigator.onLine === false);

  // Browser-level connectivity, independent of any tile request.
  useEffect(() => {
    const on = () => setOffline(false);
    const off = () => setOffline(true);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);

  // One map per mount.
  useEffect(() => {
    if (!container.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: OSM,
      center: [lon ?? 0, lat ?? 0],
      zoom: lat != null ? 17 : 1,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("error", (e) => {
      if (isResourceFailure(e)) setOffline(true);
    });
    map.on("sourcedata", (e) => {
      if (e.sourceId === BASEMAP_SOURCE && e.tile) setOffline(false);
    });
    map.on("style.load", () => {
      styleReady.current = true;
      drawAccuracy.current();
    });
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      baseMarker.current = null;
      roverMarkers.current.clear();
      styleReady.current = false;
      styleShown.current = false;
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps -- the initial centre only seeds the view

  // Style switch. The map is created on OSM, so the first run (mount) must not call setStyle:
  // doing so while the initial style is still loading makes maplibre rebuild it from scratch.
  const styleShown = useRef(false);
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (styleShown.current === imagery) return;
    styleShown.current = imagery;
    styleReady.current = false;
    map.setStyle(imagery ? IMAGERY : OSM); // style.load re-adds the accuracy layers
  }, [imagery]);

  // Base marker + accuracy circle follow the position.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || lat == null || lon == null) return;
    if (!baseMarker.current) {
      baseMarker.current = new maplibregl.Marker({ element: markerElement("base", "Base position") }).setLngLat([lon, lat]).addTo(map);
      map.easeTo({ center: [lon, lat], zoom: 17, duration: 0 });
    } else {
      baseMarker.current.setLngLat([lon, lat]);
    }
    drawAccuracy.current = () => {
      const m = mapRef.current;
      if (!m) return;
      const data = circlePolygon(lat, lon, Math.max(hAcc ?? 0, 0.05));
      const src = m.getSource("acc") as GeoJSONSource | undefined;
      if (src) {
        src.setData(data);
        return;
      }
      m.addSource("acc", { type: "geojson", data });
      m.addLayer({ id: "acc-fill", type: "fill", source: "acc", paint: { "fill-color": NAVY, "fill-opacity": 0.12 } });
      m.addLayer({ id: "acc-halo", type: "line", source: "acc", paint: { "line-color": OFF_WHITE, "line-width": 3.5, "line-opacity": 0.8 } });
      m.addLayer({ id: "acc-line", type: "line", source: "acc", paint: { "line-color": NAVY, "line-width": 1.5 } });
    };
    if (styleReady.current || map.isStyleLoaded()) drawAccuracy.current();
  }, [lat, lon, hAcc]);

  // Rover markers keyed by client id.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const seen = new Set<number>();
    for (const r of rovers) {
      if (r.last_gga_lat == null || r.last_gga_lon == null) continue;
      seen.add(r.id);
      let m = roverMarkers.current.get(r.id);
      if (!m) {
        m = new maplibregl.Marker({ element: markerElement("rover", `${r.user_agent || "rover"} (${r.ip})`) }).setLngLat([r.last_gga_lon, r.last_gga_lat]).addTo(map);
        roverMarkers.current.set(r.id, m);
      } else {
        m.setLngLat([r.last_gga_lon, r.last_gga_lat]);
      }
    }
    for (const [id, m] of roverMarkers.current) {
      if (!seen.has(id)) {
        m.remove();
        roverMarkers.current.delete(id);
      }
    }
  }, [rovers]);

  const recentre = () => {
    if (mapRef.current && lat != null && lon != null) mapRef.current.easeTo({ center: [lon, lat], zoom: 17 });
  };

  return (
    <div
      data-testid="map-frame"
      data-offline={offline}
      className={cn("relative min-h-[320px] flex-1 overflow-hidden rounded-md", offline && "map-grid", className)}
      style={height != null ? { height, minHeight: height } : undefined}
    >
      <div ref={container} className="h-full w-full" data-testid="map" />
      <div className="absolute top-2 left-2 flex gap-1">
        <Button type="button" size="sm" variant={imagery ? "outline" : "default"} aria-pressed={!imagery} onClick={() => setImagery(false)}>
          Map
        </Button>
        <Button type="button" size="sm" variant={imagery ? "default" : "outline"} aria-pressed={imagery} onClick={() => setImagery(true)}>
          Imagery
        </Button>
        {lat != null ? (
          <Button type="button" size="icon-sm" variant="outline" aria-label="Centre on the base" title="Centre on the base" onClick={recentre}>
            <LocateFixed className="size-4" aria-hidden />
          </Button>
        ) : null}
      </div>
      {offline ? (
        <div role="status" className="pointer-events-none absolute inset-x-2 bottom-2 flex items-center gap-2 rounded-md border border-line bg-panel/90 px-3 py-1.5 text-[12px] leading-4 text-ink-2">
          <WifiOff className="size-3.5 shrink-0" aria-hidden />
          Map tiles unavailable (offline). Position and rover markers still update.
        </div>
      ) : null}
      {lat == null ? <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-ink-2">Waiting for a position fix</div> : null}
    </div>
  );
}
