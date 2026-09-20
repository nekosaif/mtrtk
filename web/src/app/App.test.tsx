import { renderHook } from "@testing-library/react";
import { auth, goToLogin } from "@/lib/api";
import { configureLive, resetLiveForTests, useLive } from "@/lib/live";
import { useLiveConnection } from "./App";

// App pulls in the whole route table; the map needs a stub because jsdom has no WebGL.
vi.mock("maplibre-gl", () => import("@/test/maplibreMock"));

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }
  close(): void {
    this.readyState = 3;
  }
}

const originalLocation = window.location;
function at(pathname: string, search = "") {
  Object.defineProperty(window, "location", {
    value: { ...originalLocation, origin: originalLocation.origin, pathname, search, assign: vi.fn(), replace: vi.fn() },
    writable: true,
  });
}

/**
 * The socket belongs to a signed-in session. Opened on /login it can only be refused, and the
 * store's own reconnect ladder then knocks on the daemon every 30 s for as long as the tab is
 * parked there — the one page where there is nothing live to show.
 */
describe("the app's live connection", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    resetLiveForTests();
    auth.reset();
    at("/");
    configureLive({ WebSocket: FakeWebSocket as unknown as typeof WebSocket, log: () => undefined });
  });
  afterEach(() => {
    useLive.getState().disconnect();
    Object.defineProperty(window, "location", { value: originalLocation, writable: true });
  });

  it("opens the one socket on an ordinary page, passing a pasted ?token= through", () => {
    at("/logs", "?token=abc");
    renderHook(() => useLiveConnection());
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toContain("token=abc");
  });

  it("opens no socket at all while the tab is parked on /login", () => {
    at("/login", "?next=%2Flogs");
    renderHook(() => useLiveConnection());
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(useLive.getState().status).toBe("connecting");
    expect(useLive.getState().nextRetryAt).toBeNull();
  });

  it("opens no socket once the client knows this session is not signed in", () => {
    at("/login");
    goToLogin(); // the 401 path: it only records the fact while already on /login
    at("/logs");
    renderHook(() => useLiveConnection());
    expect(auth.unauthorizedSeen()).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
