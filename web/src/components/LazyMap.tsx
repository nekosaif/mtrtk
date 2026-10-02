import { lazy, Suspense, useState, type ComponentProps, type ComponentType } from "react";
import { Button } from "@/components/ui/button";
import { RenderBoundary } from "./RenderBoundary";
import type { MapPanel as MapPanelImpl } from "./MapPanel";
import type { TrackMap as TrackMapImpl } from "./TrackMap";
import { cn } from "@/lib/utils";

/*
 * The maps behind React.lazy. maplibre-gl is about 1 MB of minified script plus its stylesheet;
 * imported statically it rode in the entry's critical path on every page, including the ten that
 * draw no map. Behind these wrappers it — and maplibre-gl.css — load only when a map first
 * renders. Pages import `MapPanel` / `TrackMap` from here; the type-only imports above pull in
 * nothing at runtime.
 *
 * Loading on demand means the load can fail while the app is open: a network blip, or a daemon
 * upgrade that removed the hashed chunk this tab still points at. React.lazy keeps a rejected
 * load for good, so each map sits in its own boundary whose Retry swaps in a fresh lazy wrapper
 * and asks for the code again; the rest of the page keeps working meanwhile.
 */
function retryableLazy<P extends object>(load: () => Promise<ComponentType<P>>) {
  const make = () => lazy(async () => ({ default: await load() }));
  let current = make();
  return {
    get: () => current,
    retry: () => {
      current = make();
    },
  };
}

const MapPanelLazy = retryableLazy(() => import("./MapPanel").then((m) => m.MapPanel));
const TrackMapLazy = retryableLazy(() => import("./TrackMap").then((m) => m.TrackMap));

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

/** The same frame when the map's code could not be fetched, with a way to ask again. */
function MapFailed({ className, height, error, onRetry }: { className?: string; height?: number; error: Error; onRetry: () => void }) {
  return (
    <div
      data-testid="map-failed"
      role="alert"
      className={cn("map-grid relative flex min-h-[320px] flex-1 flex-col items-center justify-center gap-2 rounded-md p-4 text-center", className)}
      style={height != null ? { height, minHeight: height } : undefined}
    >
      <p className="text-ink">The map could not load</p>
      <p className="text-xs text-ink-2">{error.message}</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}

function LazyFrame<P extends object>({ loader, props, className, height }: { loader: ReturnType<typeof retryableLazy<P>>; props: P; className?: string; height?: number }) {
  // Bumped on Retry so this frame re-renders with the fresh wrapper `loader.retry()` made.
  const [, setAttempt] = useState(0);
  const Impl = loader.get();
  return (
    <RenderBoundary
      fallback={(error, reset) => (
        <MapFailed
          className={className}
          height={height}
          error={error}
          onRetry={() => {
            loader.retry();
            setAttempt((n) => n + 1);
            reset();
          }}
        />
      )}
    >
      <Suspense fallback={<MapLoading className={className} height={height} />}>
        <Impl {...props} />
      </Suspense>
    </RenderBoundary>
  );
}

export function MapPanel(props: ComponentProps<typeof MapPanelImpl>) {
  return <LazyFrame loader={MapPanelLazy} props={props} className={props.className} height={props.height} />;
}

export function TrackMap(props: ComponentProps<typeof TrackMapImpl>) {
  return <LazyFrame loader={TrackMapLazy} props={props} height={props.height ?? 360} />;
}
