import { act, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter } from "react-router";
import { routes } from "./router";
import { NAV, NAV_BASE, NAV_ROVER } from "./Rail";
import { resetLiveForTests, useLive } from "@/lib/live";

// Pages need the app's providers; the map is mocked (no WebGL in jsdom) and the only network a
// page may touch is stubbed.
vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("Shell", () => {
  beforeEach(() => {
    // A test that sets the role must not leak it into the next one, even when it fails midway.
    resetLiveForTests();
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("{}", { status: 200, headers: { "content-type": "application/json" } })) as typeof fetch;
  });

  it("renders the rail with every page link and marks the active one", () => {
    renderAt("/satellites");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toHaveTextContent("Dashboard");
    expect(nav).toHaveTextContent("Settings");
    expect(screen.getByRole("link", { name: /satellites/i })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Satellites");
    expect(document.title).toBe("Satellites · mtrtk");
  });

  // E3 — nine rail stops before the page at every width: the first Tab offers to skip them.
  it("offers a skip link as the first focusable element, landing on the page itself", async () => {
    renderAt("/satellites");
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveAttribute("href", "#main");
    expect(skip.className).toContain("sr-only");
    expect(skip.className).toContain("focus:not-sr-only");
    const focusable = [...document.querySelectorAll<HTMLElement>("a[href], button, input, select, [tabindex]:not([tabindex='-1'])")];
    expect(focusable[0]).toBe(skip);
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("id", "main");
    expect(main).toHaveAttribute("tabindex", "-1"); // so the fragment jump also moves focus there
  });

  it("shows the tape with a UTC clock and connection state", () => {
    renderAt("/");
    expect(screen.getByRole("region", { name: "Live status" })).toHaveTextContent(/UTC/);
    expect(screen.getByRole("region", { name: "Live status" })).toHaveTextContent(/connecting/);
  });

  it("links every page the plan lists, in rail order", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const hrefs = within(nav).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(NAV.map((item) => item.to));
    expect(hrefs).toEqual(["/", "/satellites", "/receiver", "/corrections", "/site", "/logs", "/history", "/ppk", "/events", "/settings"]);
  });

  it("collapses by breakpoint: 220 px rail, 64 px icon rail below lg, bottom tab bar below sm", () => {
    // jsdom has no layout engine, so this pins the class contract the CSS breakpoints act on.
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const frame = nav.closest("[data-slot=shell]");
    expect(frame).not.toBeNull();
    expect(frame!.className).toContain("grid-cols-[220px_1fr]");
    expect(frame!.className).toContain("max-lg:grid-cols-[64px_1fr]");
    expect(frame!.className).toContain("max-sm:grid-cols-1");
    expect(nav.className).toContain("max-sm:flex-row");
    // Below lg the labels leave the screen but not the accessible name: sr-only, never display:none.
    const label = within(nav).getByText("Dashboard");
    expect(label.className).toContain("max-lg:sr-only");
    expect(label.className).not.toContain("max-lg:hidden");
  });

  it("PageHeader sets the document title for every page", () => {
    renderAt("/settings");
    expect(document.title).toBe("Settings · mtrtk");
  });

  it("serves the login page outside the shell", () => {
    renderAt("/login");
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Sign in");
    expect(document.title).toBe("Sign in · mtrtk");
  });

  it("follows the daemon's role: the base list until the snapshot, the rover list on a rover", () => {
    resetLiveForTests();
    expect(NAV).toBe(NAV_BASE);
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const hrefs = () => within(nav).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs()).toEqual(NAV_BASE.map((item) => item.to));
    expect(nav).toHaveTextContent("base station");
    act(() => useLive.setState({ role: "rover" }));
    expect(hrefs()).toEqual(["/", "/satellites", "/receiver", "/rtk", "/survey", "/logs", "/history", "/ppk", "/events", "/settings"]);
    expect(hrefs()).toEqual(NAV_ROVER.map((item) => item.to));
    expect(nav).toHaveTextContent("rover");
    expect(nav).not.toHaveTextContent("base station");
    act(() => resetLiveForTests());
  });

  it("routes /ppk", () => {
    renderAt("/ppk");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("PPK");
  });

  it("routes /rtk", () => {
    renderAt("/rtk");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("RTK");
  });

  it("routes /survey", () => {
    renderAt("/survey");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Survey");
  });
});
