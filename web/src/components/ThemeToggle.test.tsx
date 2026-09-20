import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { Rail } from "@/app/Rail";
import { auth } from "@/lib/api";
import { resetPrefsForTests } from "@/lib/prefs";
import { THEME_PREF, ThemeToggle, applyTheme, initTheme, resolveTheme } from "./ThemeToggle";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** jsdom's matchMedia is stubbed in setup.ts; this one answers a chosen system preference. */
function stubMatchMedia(prefersLight: boolean) {
  const listeners: ((e: MediaQueryListEvent) => void)[] = [];
  const mql = {
    matches: prefersLight,
    media: "(prefers-color-scheme: light)",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn((_: string, cb: (e: MediaQueryListEvent) => void) => listeners.push(cb)),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  };
  Object.defineProperty(window, "matchMedia", { writable: true, value: vi.fn().mockReturnValue(mql) });
  return {
    mql,
    change(next: boolean) {
      mql.matches = next;
      listeners.forEach((cb) => cb({ matches: next } as MediaQueryListEvent));
    },
  };
}

function renderToggle() {
  return render(<ThemeToggle />);
}

describe("ThemeToggle", () => {
  beforeEach(() => {
    resetPrefsForTests();
    localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    stubMatchMedia(false);
  });

  it("writes the theme onto the document element", () => {
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    applyTheme("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("resolves 'system' through prefers-color-scheme", () => {
    stubMatchMedia(true);
    expect(resolveTheme("system")).toBe("light");
    stubMatchMedia(false);
    expect(resolveTheme("system")).toBe("dark");
    expect(resolveTheme("light")).toBe("light");
  });

  it("cycles dark → light → system and persists the choice", async () => {
    renderToggle();
    expect(document.documentElement.dataset.theme).toBe("dark");

    await userEvent.click(screen.getByRole("button", { name: /light theme/i }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("mtrtk:theme")).toBe('"light"');

    await userEvent.click(screen.getByRole("button", { name: /system theme/i }));
    expect(localStorage.getItem("mtrtk:theme")).toBe('"system"');
    // No system preference for light in this stub, so "system" lands on dark.
    expect(document.documentElement.dataset.theme).toBe("dark");

    await userEvent.click(screen.getByRole("button", { name: /dark theme/i }));
    expect(localStorage.getItem("mtrtk:theme")).toBe('"dark"');
  });

  it("applies the saved theme before React mounts and keeps following the system", () => {
    localStorage.setItem(`mtrtk:${THEME_PREF}`, '"system"');
    const media = stubMatchMedia(false);
    initTheme();
    expect(document.documentElement.dataset.theme).toBe("dark");
    media.change(true);
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("survives storage it may not read", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => initTheme()).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("dark");
    getItem.mockRestore();
  });
});

describe("Rail foot", () => {
  const originalLocation = window.location;
  let replace: ReturnType<typeof vi.fn>;

  function renderRail() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <Rail />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  beforeEach(() => {
    resetPrefsForTests();
    localStorage.clear();
    auth.reset();
    document.documentElement.removeAttribute("data-theme");
    stubMatchMedia(false);
    replace = vi.fn();
    Object.defineProperty(window, "location", { value: { ...originalLocation, pathname: "/", search: "", replace, assign: vi.fn() }, writable: true });
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (p.endsWith("/api/config")) return json({ values: { web_password: "***" }, pending: {}, env_file: ".env", secret_keys: [], live_keys: [], read_only_keys: [], url_secret_keys: [] });
      return json({ ok: true });
    }) as typeof fetch;
  });

  afterEach(() => {
    Object.defineProperty(window, "location", { value: originalLocation, writable: true });
  });

  it("carries the theme toggle under the navigation links", () => {
    renderRail();
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(within(nav).getByRole("button", { name: /light theme/i })).toBeInTheDocument();
  });

  it("signs out through the daemon and lands on the login page", async () => {
    renderRail();
    const signOut = await screen.findByRole("button", { name: /sign out/i });
    await userEvent.click(signOut);
    const posts = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit | undefined][] } }).mock.calls;
    expect(posts.some(([u, i]) => String(u).endsWith("/api/logout") && i?.method === "POST")).toBe(true);
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
