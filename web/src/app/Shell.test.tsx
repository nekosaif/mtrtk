import { render, screen, within } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router";
import { routes } from "./router";
import { NAV } from "./Rail";

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(<RouterProvider router={router} />);
}

describe("Shell", () => {
  it("renders the rail with every page link and marks the active one", () => {
    renderAt("/satellites");
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(nav).toHaveTextContent("Dashboard");
    expect(nav).toHaveTextContent("Settings");
    expect(screen.getByRole("link", { name: /satellites/i })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Satellites");
    expect(document.title).toBe("Satellites · mtrtk");
  });

  it("shows the tape with a UTC clock and connection state", () => {
    renderAt("/");
    expect(screen.getByRole("status")).toHaveTextContent(/UTC/);
    expect(screen.getByRole("status")).toHaveTextContent(/connecting/);
  });

  it("links every page the plan lists, in rail order", () => {
    renderAt("/");
    const nav = screen.getByRole("navigation", { name: "Main" });
    const hrefs = within(nav).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(NAV.map((item) => item.to));
    expect(hrefs).toEqual(["/", "/satellites", "/receiver", "/corrections", "/site", "/logs", "/history", "/events", "/settings"]);
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
});
