import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MapPanel } from "./LazyMap";

// The map's chunk fails to load the first time (a network blip, or a redeploy that removed the
// hashed file this tab still points at) and loads on the next attempt.
const chunk = vi.hoisted(() => ({ loads: 0 }));
vi.mock("@/components/MapPanel", () => {
  chunk.loads += 1;
  if (chunk.loads === 1) throw new TypeError("Failed to fetch dynamically imported module");
  return { MapPanel: () => <div data-testid="map-ok" /> };
});

describe("LazyMap when the map's code fails to load", () => {
  // React reports the caught load error through console.error; it is expected here.
  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("keeps the panel's frame, says so, and loads the map again on Retry", async () => {
    render(<MapPanel lat={23.8} lon={90.2} hAcc={0.02} height={280} />);
    const failed = await screen.findByTestId("map-failed");
    expect(failed).toHaveTextContent("The map could not load");
    expect(failed).toHaveStyle({ height: "280px" });
    expect(failed.className).toContain("map-grid");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("map-ok")).toBeInTheDocument();
    expect(screen.queryByTestId("map-failed")).toBeNull();
    expect(chunk.loads).toBe(2);
  });
});
