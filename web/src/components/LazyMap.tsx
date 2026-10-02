import { lazy, Suspense, type ComponentProps } from "react";
import type { MapPanel as MapPanelImpl } from "./MapPanel";
import type { TrackMap as TrackMapImpl } from "./TrackMap";
import { cn } from "@/lib/utils";

/*
 * The maps behind React.lazy. maplibre-gl is about 1 MB of minified script plus its stylesheet;
 * imported statically it rode in the entry's critical path on every page, including the ten that
 * draw no map. Behind these wrappers it — and maplibre-gl.css — load only when a map first
 * renders. Pages import `MapPanel` / `TrackMap` from here; the type-only imports above pull in
 * nothing at runtime.
 */
const MapPanelLazy = lazy(() => import("./MapPanel").then((m) => ({ default: m.MapPanel })));
const TrackMapLazy = lazy(() => import("./TrackMap").then((m) => ({ default: m.TrackMap })));

/** The frame's own size and grid while the map's code loads, so nothing below it jumps. */
function MapLoading({ className, height }: { className?: string; height?: number }) {
  return (
    <div
      data-testid="map-loading"
      className={cn("map-grid relative flex min-h-[320px] flex-1 items-center justify-center rounded-md text-ink-2", className)}
      style={height != null ? { height, minHeight: height } : undefined}
    >
      Loading the map…
    </div>
  );
}

export function MapPanel(props: ComponentProps<typeof MapPanelImpl>) {
  return (
    <Suspense fallback={<MapLoading className={props.className} height={props.height} />}>
      <MapPanelLazy {...props} />
    </Suspense>
  );
}

export function TrackMap(props: ComponentProps<typeof TrackMapImpl>) {
  return (
    <Suspense fallback={<MapLoading height={props.height ?? 360} />}>
      <TrackMapLazy {...props} />
    </Suspense>
  );
}
