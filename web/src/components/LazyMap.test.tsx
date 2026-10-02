import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { render, screen, waitFor } from "@testing-library/react";
import { maps, resetMaplibreMock } from "@/test/maplibreMock";
import { MapPanel } from "./LazyMap";

vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

const PAGES = ["src/pages", "web/src/pages"].map((p) => resolve(process.cwd(), p)).find(existsSync)!;

describe("LazyMap", () => {
  beforeEach(() => resetMaplibreMock());

  // F2 — maplibre-gl (~1 MB) was in the entry chunk's critical path on every page.
  it("holds the panel's place with a sized grid until the map's code has loaded", async () => {
    render(<MapPanel lat={23.8} lon={90.2} hAcc={0.02} height={280} />);
    const loading = screen.getByTestId("map-loading");
    expect(loading).toHaveStyle({ height: "280px" });
    expect(loading.className).toContain("map-grid");
    // the first dynamic import in a test file is a cold transform: give it more than a second
    expect(await screen.findByTestId("map-frame", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByTestId("map-loading")).toBeNull();
    await waitFor(() => expect(maps).toHaveLength(1));
  });

  it("is the only way a page reaches a map, so no page pulls maplibre into the entry chunk", () => {
    const direct = readdirSync(PAGES)
      .filter((f) => f.endsWith(".tsx") && !f.includes(".test."))
      .filter((f) => /from "@\/components\/(MapPanel|TrackMap)"/.test(readFileSync(join(PAGES, f), "utf8")));
    expect(direct).toEqual([]);
  });
});
