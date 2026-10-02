import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import { maps, resetMaplibreMock } from "@/test/maplibreMock";
import { MapPanel } from "./LazyMap";
import { preloadMaps } from "@/test/lazyMaps";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));
beforeAll(preloadMaps); // the lazy maps resolve from the module cache, not a cold transform

const SRC = ["src", "web/src"].map((p) => resolve(process.cwd(), p)).find((p) => existsSync(join(p, "components/LazyMap.tsx")))!;

/** Every non-test module under src, the lazy wrapper and the two maps themselves excepted. */
function modules(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return modules(p);
    if (!/\.tsx?$/.test(e.name) || e.name.includes(".test.")) return [];
    return /^(LazyMap|MapPanel|TrackMap)\.tsx$/.test(e.name) && dir.endsWith("components") ? [] : [p];
  });
}

/**
 * The static imports (and re-exports) of MapPanel or TrackMap in `text`, aliased or relative.
 * `import type` is erased; `import { type X }` is not (it keeps a side-effect import), so only
 * the former is allowed. A dynamic `import()` is the lazy path and is not matched.
 */
function staticMapImports(text: string): string[] {
  const out: string[] = [];
  for (const m of text.matchAll(/^\s*(import|export)\s+(type\s+)?([^;]*?)\s*from\s*["']([^"']+)["']/gm)) {
    if (m[2]) continue;
    if (/(^|\/)(MapPanel|TrackMap)$/.test(m[4])) out.push(m[0].trim());
  }
  for (const m of text.matchAll(/^\s*import\s*["']([^"']+)["']/gm)) if (/(^|\/)(MapPanel|TrackMap)$/.test(m[1])) out.push(m[0].trim());
  return out;
}

describe("LazyMap", () => {
  beforeEach(() => resetMaplibreMock());

  // F2 — maplibre-gl (~1 MB) was in the entry chunk's critical path on every page.
  it("holds the panel's place with a sized grid until the map's code has loaded", async () => {
    render(<MapPanel lat={23.8} lon={90.2} hAcc={0.02} height={280} />);
    const loading = screen.getByTestId("map-loading");
    expect(loading).toHaveStyle({ height: "280px" });
    expect(loading.className).toContain("map-grid");
    expect(await screen.findByTestId("map-frame")).toBeInTheDocument();
    expect(screen.queryByTestId("map-loading")).toBeNull();
    await waitFor(() => expect(maps).toHaveLength(1));
  });

  it("recognises a static map import in every spelling, and lets type-only and lazy ones through", () => {
    for (const src of [
      'import { MapPanel } from "@/components/MapPanel";',
      "import { TrackMap } from '../components/TrackMap';",
      'import { MapPanel } from "./MapPanel";',
      'import {\n  trackBounds,\n} from "@/components/TrackMap";',
      'import { type MapPoint } from "@/components/MapPanel";',
      'export { MapPanel } from "./MapPanel";',
      'import "@/components/MapPanel";',
    ])
      expect(staticMapImports(src), src).toHaveLength(1);
    for (const src of ['import type { MapPoint } from "@/components/MapPanel";', 'const m = await import("@/components/MapPanel");', 'import { MapPanel } from "@/components/LazyMap";', 'import { X } from "@/components/MapPanelLegend";'])
      expect(staticMapImports(src), src).toEqual([]);
  });

  it("is the only way the app reaches a map, so nothing pulls maplibre into the entry chunk", () => {
    const files = modules(SRC);
    expect(files.some((f) => f.endsWith(join("pages", "Dashboard.tsx")))).toBe(true);
    const direct = files.flatMap((f) => staticMapImports(readFileSync(f, "utf8")).map((imp) => `${relative(SRC, f)}: ${imp}`));
    expect(direct).toEqual([]);
  });
});
