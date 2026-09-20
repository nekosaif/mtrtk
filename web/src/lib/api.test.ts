import {
  ApiError,
  ROUTES,
  api,
  auth,
  describeError,
  receiverPoll,
  route,
  wsUrl,
} from "./api";

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("api", () => {
  const originalFetch = globalThis.fetch;
  const originalLocation = window.location;
  afterEach(() => {
    globalThis.fetch = originalFetch;
    Object.defineProperty(window, "location", { value: originalLocation, writable: true });
    auth.reset();
    vi.restoreAllMocks();
  });

  it("returns JSON and sends JSON bodies", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    globalThis.fetch = fetchMock as typeof fetch;
    await expect(api("/api/x", { method: "POST", body: JSON.stringify({ a: 1 }) })).resolves.toEqual({ ok: 1 });
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Content-Type")).toBe("application/json");
    expect(init.credentials).toBe("same-origin");
  });

  it("throws ApiError with the server detail", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "no site named 'x'" }), { status: 404 })) as typeof fetch;
    await expect(api("/api/x")).rejects.toMatchObject({ status: 404, detail: "no site named 'x'" });
  });

  it("redirects to /login on 401", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 })) as typeof fetch;
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { ...window.location, pathname: "/logs", search: "", assign }, writable: true });
    await expect(api("/api/x")).rejects.toBeInstanceOf(ApiError);
    expect(assign).toHaveBeenCalledWith("/login?next=%2Flogs");
  });

  it("remembers that a password is configured once a 401 has been seen", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 })) as typeof fetch;
    Object.defineProperty(window, "location", { value: { ...window.location, pathname: "/", search: "", assign: vi.fn() }, writable: true });
    expect(auth.passwordConfigured()).toBe(false);
    await api("/api/x").catch(() => undefined);
    expect(auth.passwordConfigured()).toBe(true);
  });

  it("does not redirect again while already on /login", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("", { status: 401 })) as typeof fetch;
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { ...window.location, pathname: "/login", search: "?next=%2F", assign }, writable: true });
    await api("/api/x").catch(() => undefined);
    expect(assign).not.toHaveBeenCalled();
  });

  it("learns that a password is configured from a masked GET /api/config", () => {
    auth.noteConfigValues({ web_password: "***" });
    expect(auth.passwordConfigured()).toBe(true);
    auth.reset();
    auth.noteConfigValues({ web_password: null });
    expect(auth.passwordConfigured()).toBe(false);
  });

  it("passes 422 validation details through untouched", async () => {
    const detail = [{ loc: ["body", "values", "svin_acc_limit_m"], msg: "Input should be greater than 0", type: "greater_than" }];
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ detail }, 422)) as typeof fetch;
    const err = await api("/api/config", { method: "PUT", body: "{}" }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(422);
    expect(apiErr.detail).toEqual(detail);
    expect(apiErr.issues).toEqual(detail);
    // only the request-part prefix is dropped; the field path is kept whole
    expect(describeError(apiErr)).toBe("values.svin_acc_limit_m: Input should be greater than 0");
    // a Settings validation error (PUT /api/config) has no "body" prefix at all
    expect(describeError(new ApiError(422, [{ loc: ["svin_acc_limit_m"], msg: "Input should be greater than 0", type: "greater_than" }]))).toBe(
      "svin_acc_limit_m: Input should be greater than 0",
    );
    expect(describeError(new ApiError(422, [{ loc: [], msg: "metrics is empty", type: "value_error" }, { loc: ["query", "limit"], msg: "too big", type: "x" }]))).toBe(
      "metrics is empty; limit: too big",
    );
  });

  it("keeps 409 and 504 details verbatim for the UI", async () => {
    const detail = "receiver is in passive mode: mtrtk only listens and writes no configuration";
    globalThis.fetch = vi.fn().mockResolvedValue(jsonResponse({ detail }, 409)) as typeof fetch;
    const err = (await api("/api/receiver/poll", { method: "POST", body: "{}" }).catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(409);
    expect(err.detail).toBe(detail);
    expect(describeError(err)).toBe(detail);
    expect(err.message).toBe(`409: ${detail}`);
  });

  it("falls back to the status text for a non-JSON error body", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" })) as typeof fetch;
    const err = (await api("/api/x").catch((e: unknown) => e)) as ApiError;
    expect(err.status).toBe(502);
    expect(err.detail).toBe("Bad Gateway");
  });

  it("returns undefined for a 204", async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(new Response(null, { status: 204 })) as typeof fetch;
    await expect(api("/api/x", { method: "DELETE" })).resolves.toBeUndefined();
  });

  it("describes a plain Error and a network failure too", () => {
    expect(describeError(new Error("Failed to fetch"))).toBe("Failed to fetch");
    expect(describeError("nope")).toBe("nope");
  });

  it("builds the websocket url with an optional token", () => {
    Object.defineProperty(window, "location", { value: { ...window.location, protocol: "http:", host: "base:8080" }, writable: true });
    expect(wsUrl()).toBe("ws://base:8080/ws");
    expect(wsUrl("abc")).toBe("ws://base:8080/ws?token=abc");
    // one shared socket wants every topic, so `?topics=` is never sent
    expect(wsUrl("abc")).not.toContain("topics");
  });

  it("uses wss behind https", () => {
    Object.defineProperty(window, "location", { value: { ...window.location, protocol: "https:", host: "base.example" }, writable: true });
    expect(wsUrl()).toBe("wss://base.example/ws");
  });

  it("fills path templates and encodes params and queries", () => {
    expect(route(ROUTES.sites)).toBe("/api/base/sites");
    expect(route(ROUTES.activateSite, { name: "roof top/1" })).toBe("/api/base/sites/roof%20top%2F1/activate");
    expect(route(ROUTES.history, undefined, { metrics: "h_acc_m,nsat_used", from: "2026-09-18T00:00:00+00:00", to: "2026-09-19T00:00:00+00:00", res: "auto" })).toBe(
      "/api/history?metrics=h_acc_m%2Cnsat_used&from=2026-09-18T00%3A00%3A00%2B00%3A00&to=2026-09-19T00%3A00%3A00%2B00%3A00&res=auto",
    );
    expect(route(ROUTES.events, undefined, { limit: 200, level: undefined })).toBe("/api/events?limit=200");
    expect(() => route(ROUTES.deleteSite, {})).toThrow(/name/);
  });

  it("posts the receiver poll body the API expects (msg_class + msg_id)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ identity: "MON-VER", swVersion: "EXT CORE 1.00" }, 200));
    globalThis.fetch = fetchMock as typeof fetch;
    await expect(receiverPoll("MON", "MON-VER")).resolves.toMatchObject({ identity: "MON-VER" });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/receiver/poll");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ msg_class: "MON", msg_id: "MON-VER" });
  });
});
