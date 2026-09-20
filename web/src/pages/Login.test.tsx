import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { auth } from "@/lib/api";
import Login from "./Login";

interface Answers {
  /** Status + body for POST /api/login; default 200 with a token. */
  login?: { status: number; body: unknown };
}

let calls: [string, RequestInit | undefined][] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    calls.push([p, init]);
    if (p.endsWith("/api/login")) {
      const r = a.login ?? { status: 200, body: { token: "abc" } };
      return json(r.body, r.status);
    }
    return json({ detail: `unexpected route ${p}` }, 404);
  }) as typeof fetch;
}

function renderPage(entry = "/login") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[entry]}>
        <Login />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const originalLocation = window.location;
let replace: ReturnType<typeof vi.fn>;

describe("Login page", () => {
  beforeEach(() => {
    auth.reset();
    mockFetch();
    replace = vi.fn();
    Object.defineProperty(window, "location", { value: { ...originalLocation, pathname: "/login", search: "", replace, assign: vi.fn() }, writable: true });
  });

  afterEach(() => {
    Object.defineProperty(window, "location", { value: originalLocation, writable: true });
  });

  it("is a page of its own, titled Sign in", async () => {
    renderPage();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Sign in");
    expect(document.title).toBe("Sign in · mtrtk");
  });

  it("signs in and returns to the page the operator came from", async () => {
    renderPage("/login?next=%2Flogs");
    await userEvent.type(screen.getByLabelText(/password/i), "pw");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));

    const post = calls.find(([u, i]) => u.endsWith("/api/login") && i?.method === "POST")!;
    expect(JSON.parse(post[1]!.body as string)).toEqual({ password: "pw" });
    expect(replace).toHaveBeenCalledWith("/logs");
  });

  it("goes home when nothing said where to return to", async () => {
    renderPage();
    await userEvent.type(screen.getByLabelText(/password/i), "pw");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(replace).toHaveBeenCalledWith("/");
  });

  it("refuses a next that points off this base station", async () => {
    renderPage("/login?next=https%3A%2F%2Felsewhere.example%2F");
    await userEvent.type(screen.getByLabelText(/password/i), "pw");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(replace).toHaveBeenCalledWith("/");
  });

  it("shows the daemon's refusal verbatim", async () => {
    mockFetch({ login: { status: 401, body: { detail: "wrong password" } } });
    renderPage();
    await userEvent.type(screen.getByLabelText(/password/i), "nope");
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("wrong password");
    expect(replace).not.toHaveBeenCalled();
  });

  it("says so when the base station has no password, and offers the way home", async () => {
    mockFetch({ login: { status: 200, body: { token: "" } } });
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText(/no password is configured/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /dashboard/i })).toHaveAttribute("href", "/");
    expect(replace).not.toHaveBeenCalled();
  });
});
