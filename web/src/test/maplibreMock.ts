/**
 * A stand-in for `maplibre-gl` under jsdom (no WebGL, no tiles): plain classes that record what
 * the component asked for. Tests reach the instances through `maps` and drive events with
 * `map.fire(...)`. `Marker.addTo` appends the marker element to the map's container so a test
 * can see that markers are still drawn. Use: `vi.mock("maplibre-gl", () => import("@/test/maplibreMock"))`.
 */
type Handler = (e: unknown) => void;

export class FakeMap {
  container: HTMLElement;
  options: Record<string, unknown>;
  style: unknown;
  sources = new Map<string, { setData: (d: unknown) => void; spec: unknown }>();
  layers: string[] = [];
  controls: unknown[] = [];
  removed = false;
  easeCalls: unknown[] = [];
  private handlers = new Map<string, Handler[]>();
  private onceHandlers = new Set<Handler>();

  constructor(options: { container: HTMLElement; style?: unknown } & Record<string, unknown>) {
    this.container = options.container;
    this.options = options;
    this.style = options.style;
    maps.push(this);
  }
  on(type: string, fn: Handler) {
    const list = this.handlers.get(type) ?? [];
    list.push(fn);
    this.handlers.set(type, list);
    return this;
  }
  once(type: string, fn: Handler) {
    this.onceHandlers.add(fn);
    return this.on(type, fn);
  }
  off(type: string, fn: Handler) {
    this.handlers.set(type, (this.handlers.get(type) ?? []).filter((h) => h !== fn));
    return this;
  }
  /** Test helper: deliver an event to every listener registered for `type`. */
  fire(type: string, e: unknown = {}) {
    for (const h of [...(this.handlers.get(type) ?? [])]) {
      h(e);
      if (this.onceHandlers.has(h)) this.off(type, h);
    }
  }
  listenerCount(type: string) {
    return (this.handlers.get(type) ?? []).length;
  }
  addControl(control: unknown) {
    this.controls.push(control);
    return this;
  }
  remove() {
    this.removed = true;
  }
  getSource(id: string) {
    return this.sources.get(id);
  }
  addSource(id: string, spec: unknown) {
    this.sources.set(id, { spec, setData: () => {} });
  }
  addLayer(layer: { id: string }) {
    this.layers.push(layer.id);
  }
  getLayer(id: string) {
    return this.layers.includes(id) ? { id } : undefined;
  }
  setStyle(style: unknown) {
    this.style = style;
    this.sources.clear();
    this.layers = [];
  }
  easeTo(options: unknown) {
    this.easeCalls.push(options);
    return this;
  }
  resize() {
    return this;
  }
  isStyleLoaded() {
    return true;
  }
  getContainer() {
    return this.container;
  }
}

export class FakeMarker {
  el: HTMLElement;
  lngLat: [number, number] | null = null;
  constructor(options?: { element?: HTMLElement }) {
    this.el = options?.element ?? document.createElement("div");
    markers.push(this);
  }
  setLngLat(ll: [number, number]) {
    this.lngLat = ll;
    return this;
  }
  addTo(map: FakeMap) {
    map.container.appendChild(this.el);
    return this;
  }
  remove() {
    this.el.remove();
  }
  setPopup() {
    return this;
  }
  getElement() {
    return this.el;
  }
}

export class FakePopup {
  setText() {
    return this;
  }
  setHTML() {
    return this;
  }
}
export class FakeNavigationControl {}
export class FakeAttributionControl {}

/** Every map / marker constructed since the last `resetMaplibreMock()`. */
export const maps: FakeMap[] = [];
export const markers: FakeMarker[] = [];
export function resetMaplibreMock(): void {
  maps.length = 0;
  markers.length = 0;
}

export { FakeMap as Map, FakeMarker as Marker, FakePopup as Popup, FakeNavigationControl as NavigationControl, FakeAttributionControl as AttributionControl };
export default { Map: FakeMap, Marker: FakeMarker, Popup: FakePopup, NavigationControl: FakeNavigationControl, AttributionControl: FakeAttributionControl };
