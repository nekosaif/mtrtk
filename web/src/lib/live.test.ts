import {
  BACKOFF_CROWDED_MS,
  MAX_DAEMON_FAILURES,
  MAX_EVENTS,
  SILENT_CLOSE_MS,
  STALE_AFTER_MS,
  configureLive,
  isStale,
  resetLiveForTests,
  useLive,
} from "./live";
import type { ReceiverState, WsMessage } from "./types";

function baseState(): ReceiverState {
  return {
    connected: true, source: "file", epoch_count: 3, raw_epochs: 3, last_epoch_mono: null,
    position: { lat: 23.8, lon: 90.2, height_m: -36, hmsl_m: 13, ecef_x_m: null, ecef_y_m: null, ecef_z_m: null, invalid_llh: false },
    accuracy: { h_acc_m: 1.0, v_acc_m: 1.5, p_acc_m: null, t_acc_ns: null, s_acc_mps: null, head_acc_deg: null },
    dops: { g: null, p: 1.2, t: null, v: null, h: null, n: null, e: null },
    fix: { fix_type: 3, fix_type_name: "3D", gnss_fix_ok: true, diff_soln: false, carr_soln: 0, carr_soln_name: "None", num_sv: 20, last_correction_age: 0, psm_state: 0, spoof_det_state: 0, ttff_ms: null, uptime_ms: null },
    velocity: { vel_n_mps: 0, vel_e_mps: 0, vel_d_mps: 0, ground_speed_mps: 0, heading_motion_deg: 0 },
    time: { utc: "2026-09-18T16:47:34+00:00", itow_ms: 1, gps_week: null, gps_tow_s: null, leap_s: null, valid_date: true, valid_time: true, fully_resolved: true, valid_utc: false, utc_standard: null, t_acc_ns: null, clk_bias_ns: null, clk_drift_nsps: null, f_acc_psps: null, leap_source: null, time_to_leap_event_s: null, leap_change: null },
    sats: [], sat_summary: { tracked: 0, used: 0, per_gnss: {} }, hardware: null, rf: [], spectrum: [], ports: [],
    survey_in: { active: false, valid: false, dur_s: 0, obs: 0, mean_x_m: null, mean_y_m: null, mean_z_m: null, mean_acc_m: null },
    rtcm_out: { messages: {}, total_count: 0, total_bytes: 0, bytes_per_s: 0 },
    firmware: { sw_version: "", hw_version: "", fw_version: "", protver: "", module: "", extensions: [] },
  };
}

// Real message shapes, copied from docs/api.md ("WebSocket /ws").
const SNAPSHOT: WsMessage = { type: "snapshot", role: "base", topics: ["pvt", "sats"], state: baseState() };
const UPDATE_CONNECTED: WsMessage = { type: "update", topic: "receiver", source: "receiver.connected", data: "serial:/dev/ttyACM0" };
function epoch(over: Partial<ReceiverState> = {}): WsMessage {
  const s = { ...baseState(), ...over };
  return {
    type: "epoch",
    t: 1789861804.99,
    pvt: { position: s.position, accuracy: s.accuracy, dops: s.dops, fix: s.fix, velocity: s.velocity, time: s.time },
    sats: { sats: s.sats, sat_summary: s.sat_summary },
    rtcm: s.rtcm_out,
    svin: s.survey_in,
  };
}
const update = (topic: string, source: string, data: unknown): WsMessage => ({ type: "update", topic, source, data }) as WsMessage;

describe("live store reducer", () => {
  beforeEach(() => resetLiveForTests());

  it("applies snapshot then merges epochs", () => {
    const apply = useLive.getState().applyMessage;
    apply(SNAPSHOT, 1000);
    expect(useLive.getState().state?.fix.fix_type).toBe(3);
    expect(useLive.getState().role).toBe("base");
    expect(useLive.getState().receiverConnected).toBe(true);
    apply({ type: "epoch", t: 1, pvt: { ...baseState(), accuracy: { ...baseState().accuracy, h_acc_m: 0.02 } } as never, rtcm: { messages: {}, total_count: 5, total_bytes: 50, bytes_per_s: 10 } } as WsMessage, 2000);
    const s = useLive.getState();
    expect(s.state?.accuracy.h_acc_m).toBe(0.02);
    expect(s.state?.rtcm_out.total_count).toBe(5);
    expect(s.state?.epoch_count).toBe(4);
    expect(s.lastEpochAt).toBe(2000);
    expect(s.stale).toBe(false);
  });

  it("merges only the sections an epoch carries and leaves the rest of the state alone", () => {
    const apply = useLive.getState().applyMessage;
    apply(SNAPSHOT, 1000);
    apply(update("rf", "state.hardware", { jam_ind: 7 }));
    apply({ type: "epoch", t: null, svin: { active: true, valid: false, dur_s: 12, obs: 12, mean_x_m: null, mean_y_m: null, mean_z_m: null, mean_acc_m: 3.2 } }, 3000);
    const s = useLive.getState();
    expect(s.state?.survey_in.dur_s).toBe(12);
    expect(s.state?.accuracy.h_acc_m).toBe(1.0);
    expect((s.state?.hardware as { jam_ind: number }).jam_ind).toBe(7);
    expect(s.state?.epoch_count).toBe(4);
  });

  it("ignores an epoch that arrives before any snapshot", () => {
    useLive.getState().applyMessage(epoch(), 5);
    expect(useLive.getState().state).toBeNull();
    expect(useLive.getState().lastEpochAt).toBeNull();
  });

  it("routes updates to the right slice", () => {
    const apply = useLive.getState().applyMessage;
    apply(SNAPSHOT);
    apply({ type: "update", topic: "rf", source: "state.hardware", data: { jam_ind: 7 } });
    apply({ type: "update", topic: "events", source: "events.new", data: { id: 1, kind: "jamming", level: "warning", message: "x", ts_utc: "t", meta: {}, acked: false } });
    apply({ type: "update", topic: "receiver", source: "receiver.disconnected", data: "unplugged" });
    apply({ type: "update", topic: "jobs", source: "jobs.update", data: { id: "abc", kind: "export", status: "running", progress: 0.5 } });
    apply({ type: "update", topic: "base", source: "base.site_verified", data: { site: "roof" } });
    const s = useLive.getState();
    expect((s.state?.hardware as { jam_ind: number }).jam_ind).toBe(7);
    expect(s.events[0].kind).toBe("jamming");
    expect(s.receiverConnected).toBe(false);
    expect(s.jobs.abc.progress).toBe(0.5);
    expect(s.base.verified).toBe(true);
  });

  it(`caps the event ring buffer at ${MAX_EVENTS}, newest first`, () => {
    const apply = useLive.getState().applyMessage;
    for (let i = 0; i < MAX_EVENTS + 10; i++) apply({ type: "update", topic: "events", source: "events.new", data: { id: i, kind: "k", level: "info", message: "", ts_utc: "", meta: {}, acked: false } });
    expect(MAX_EVENTS).toBe(200);
    expect(useLive.getState().events).toHaveLength(MAX_EVENTS);
    expect(useLive.getState().events[0].id).toBe(MAX_EVENTS + 9);
  });

  it("handles every receiver.* source", () => {
    const apply = useLive.getState().applyMessage;
    apply(SNAPSHOT, 1000);
    apply(update("receiver", "receiver.error", "link failure: device reports readiness to read but returned no data"), 1100);
    expect(useLive.getState().receiverError).toMatch(/link failure/);
    apply(update("receiver", "receiver.disconnected", "link failure: …"), 1200);
    expect(useLive.getState().receiverConnected).toBe(false);
    expect(useLive.getState().receiverReason).toBe("link failure: …");
    expect(useLive.getState().state?.connected).toBe(false);
    apply(UPDATE_CONNECTED, 1300);
    const s = useLive.getState();
    expect(s.receiverConnected).toBe(true);
    expect(s.receiverError).toBeNull();
    expect(s.receiverReason).toBeNull();
    expect(s.state?.connected).toBe(true);
    expect(s.state?.source).toBe("serial:/dev/ttyACM0");
    apply(update("receiver", "receiver.capabilities", { protver: "27.12", fw_version: "HPG 1.13", module: "ZED-F9P", supported: ["NAV-PVT"], unsupported: ["NAV-SIG"] }), 1400);
    expect(useLive.getState().receiverCapabilities?.fw_version).toBe("HPG 1.13");
    apply(update("receiver", "receiver.reset", { kind: "cold" }), 1500);
    expect(useLive.getState().receiverReset).toEqual({ kind: "cold", at: 1500 });
    useLive.getState().clearReceiverError();
    expect(useLive.getState().receiverError).toBeNull();
  });

  it("handles every base.* source", () => {
    const apply = useLive.getState().applyMessage;
    apply(update("base", "base.mode", { mode: "fixed", site: "roof", reason: null }));
    expect(useLive.getState().base).toMatchObject({ mode: "fixed", site: "roof", reason: null, verified: null, mismatch: null });
    apply(update("base", "base.site_mismatch", { site: "roof", dx: 1.2, dy: 0, dz: 0 }));
    expect(useLive.getState().base.verified).toBe(false);
    expect(useLive.getState().base.mismatch).toMatchObject({ site: "roof", dx: 1.2 });
    apply(update("base", "base.site_verified", { site: "roof", dx: 0.001, dy: 0, dz: 0 }));
    expect(useLive.getState().base.verified).toBe(true);
    expect(useLive.getState().base.mismatch).toBeNull();
    apply(update("base", "base.site_mismatch", { site: "roof", reason: "no fixType 5 within 60s after applying the fixed position" }));
    expect(useLive.getState().base.mismatch?.reason).toMatch(/fixType 5/);
    apply(update("base", "base.mode", { mode: "survey-in", site: null, reason: "receiver refused TMODE" }));
    expect(useLive.getState().base).toMatchObject({ mode: "survey-in", site: null, reason: "receiver refused TMODE", verified: null });
  });

  it("handles every rawlog.* source", () => {
    const apply = useLive.getState().applyMessage;
    apply(update("rawlog", "rawlog.rotated", "/data/ubx/2026/09/18/MTRK_20260918_16.ubx"));
    expect(useLive.getState().rawlog.current).toMatch(/_16\.ubx$/);
    apply(update("rawlog", "rawlog.backpressure", { queued: 900 }));
    expect(useLive.getState().rawlog).toMatchObject({ backpressure: true, queued: 900 });
    apply(update("rawlog", "rawlog.drained", { queued: 10 }));
    expect(useLive.getState().rawlog).toMatchObject({ backpressure: false, queued: 10 });
    apply(update("rawlog", "rawlog.error", "write: OSError(28, 'No space left on device')"));
    expect(useLive.getState().rawlog.error).toMatch(/No space/);
    apply(update("rawlog", "rawlog.closed", "/data/ubx/2026/09/18/MTRK_20260918_16.ubx"));
    expect(useLive.getState().rawlog.current).toBeNull();
    expect(useLive.getState().rawlog.lastClosed).toMatch(/_16\.ubx$/);
    expect(() => apply(update("rawlog", "rawlog.pruned", "/data/ubx/2026/09/10/MTRK_20260910_01.ubx"))).not.toThrow();
  });

  it("keeps the daemon's consumer failures, newest first", () => {
    const apply = useLive.getState().applyMessage;
    for (let i = 0; i < MAX_DAEMON_FAILURES + 3; i++) apply(update("daemon", "daemon.consumer_failed", { name: "caster", error: `RuntimeError: boom ${i}` }), 1000 + i);
    const f = useLive.getState().daemonFailures;
    expect(f).toHaveLength(MAX_DAEMON_FAILURES);
    expect(f[0]).toEqual({ name: "caster", error: `RuntimeError: boom ${MAX_DAEMON_FAILURES + 2}`, at: 1000 + MAX_DAEMON_FAILURES + 2 });
  });

  it("routes ntrip, system, span and rf sources", () => {
    const apply = useLive.getState().applyMessage;
    apply(SNAPSHOT);
    apply(update("ntrip", "ntrip.clients", [{ id: 1, ip: "100.64.0.2", port: 5000, mountpoint: "MTRK", user_agent: "NTRIP x", username: "rover", version: 2, connected_utc: "t", bytes_sent: 1, dropped_frames: 0, last_gga_lat: null, last_gga_lon: null, last_gga_utc: null }]));
    expect(useLive.getState().ntripClients).toHaveLength(1);
    apply(update("system", "system.stats", { cpu_pct: 3, mem_pct: 40, disk_free_gb: 10, disk_used_pct: 50, uptime_s: 5, temp_c: 41, load1: 0.2, ts_utc: "t" }));
    expect(useLive.getState().system?.cpu_pct).toBe(3);
    apply(update("span", "state.spectrum", [{ block_id: 0, span_hz: 1, res_hz: 1, center_hz: 1, pga_db: 1, bins: [1, 2] }]));
    expect(useLive.getState().state?.spectrum[0].bins).toEqual([1, 2]);
    apply(update("rf", "state.rf", [{ block_id: 0, jam_ind: 3 }]));
    expect((useLive.getState().state?.rf[0] as { jam_ind: number }).jam_ind).toBe(3);
  });

  it("logs unknown sources at debug and never throws", () => {
    const debug = vi.fn();
    configureLive({ log: debug });
    const apply = useLive.getState().applyMessage;
    expect(() => apply(update("receiver", "receiver.something_new", { x: 1 }))).not.toThrow();
    expect(() => apply(update("daemon", "daemon.other", null))).not.toThrow();
    expect(() => apply(update("mystery", "mystery.topic", "?"))).not.toThrow();
    expect(() => apply({ type: "nonsense" } as unknown as WsMessage)).not.toThrow();
    expect(() => apply(update("jobs", "jobs.update", "not a job"))).not.toThrow();
    expect(debug).toHaveBeenCalled();
  });

  it("isStale is pure", () => {
    expect(isStale(null, 10_000)).toBe(true);
    expect(isStale(4000, 9000)).toBe(false);
    expect(isStale(4000, 9001)).toBe(true);
    expect(STALE_AFTER_MS).toBe(5000);
  });
});

// ---------------------------------------------------------------- the socket

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = 0;
  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  closedWith: { code?: number; reason?: string } | null = null;
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  close(code?: number, reason?: string): void {
    this.readyState = 3;
    this.closedWith = { code, reason };
  }
  // --- server side
  serverOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  serverSend(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) } as MessageEvent);
  }
  serverClose(code: number, wasClean = true): void {
    this.readyState = 3;
    this.onclose?.({ code, reason: "", wasClean } as CloseEvent);
  }
  failHandshake(): void {
    // What a browser does on an HTTP 403 (or a refused connection): error, then an unclean 1006.
    this.onerror?.(new Event("error"));
    this.serverClose(1006, false);
  }
}
const sockets = () => FakeWebSocket.instances;
const last = () => FakeWebSocket.instances[FakeWebSocket.instances.length - 1];

describe("live socket", () => {
  let navigateToLogin: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-20T00:00:00Z"));
    FakeWebSocket.instances = [];
    resetLiveForTests();
    navigateToLogin = vi.fn();
    configureLive({
      WebSocket: FakeWebSocket as unknown as typeof WebSocket,
      navigateToLogin,
      passwordConfigured: () => false,
      probeAuth: async () => "unreachable",
      log: () => undefined,
    });
  });
  afterEach(() => {
    useLive.getState().disconnect();
    vi.useRealTimers();
  });

  it("opens exactly one socket per tab, without ?topics=, and connect() is idempotent", () => {
    useLive.getState().connect();
    useLive.getState().connect();
    expect(sockets()).toHaveLength(1);
    expect(last().url).toMatch(/\/ws$/);
    expect(last().url).not.toContain("topics");
    expect(useLive.getState().status).toBe("connecting");
    last().serverOpen();
    expect(useLive.getState().status).toBe("open");
    expect(useLive.getState().connected).toBe(true);
    useLive.getState().connect();
    expect(sockets()).toHaveLength(1);
  });

  it("adds ?token= only when one was pasted", () => {
    useLive.getState().connect("abc");
    expect(last().url).toMatch(/\/ws\?token=abc$/);
  });

  it("feeds messages through applyMessage and survives a malformed frame", () => {
    useLive.getState().connect();
    last().serverOpen();
    last().serverSend(SNAPSHOT);
    expect(useLive.getState().state?.epoch_count).toBe(3);
    expect(() => last().onmessage?.({ data: "{not json" } as MessageEvent)).not.toThrow();
    last().serverSend(epoch());
    expect(useLive.getState().state?.epoch_count).toBe(4);
  });

  it("reconnects with exponential backoff 1 s → 30 s after an abnormal close, and resets on open", async () => {
    useLive.getState().connect();
    last().serverOpen();
    const delays: number[] = [];
    let previous = Date.now();
    for (const expected of [1000, 2000, 4000, 8000, 16_000, 30_000, 30_000]) {
      last().serverClose(1006, false);
      expect(useLive.getState().status).toBe("reconnecting");
      const before = sockets().length;
      await vi.advanceTimersByTimeAsync(expected - 1);
      expect(sockets()).toHaveLength(before);
      await vi.advanceTimersByTimeAsync(1);
      expect(sockets()).toHaveLength(before + 1);
      delays.push(Date.now() - previous);
      previous = Date.now();
    }
    expect(delays).toEqual([1000, 2000, 4000, 8000, 16_000, 30_000, 30_000]);
    expect(useLive.getState().attempts).toBe(7);
    last().serverOpen();
    expect(useLive.getState().attempts).toBe(0);
    last().serverClose(1012);
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets()).toHaveLength(9); // the ladder restarted at 1 s
  });

  it("treats 1012, 1001 and 1011 as plain backoff reconnects", async () => {
    useLive.getState().connect();
    for (const code of [1012, 1001, 1011]) {
      last().serverOpen();
      last().serverClose(code);
      expect(useLive.getState().status).toBe("reconnecting");
      expect(navigateToLogin).not.toHaveBeenCalled();
      await vi.runOnlyPendingTimersAsync();
    }
    expect(sockets()).toHaveLength(4);
  });

  it("uses a longer backoff after 1008 (slow client) and 1013 (too many clients), never logs out", async () => {
    useLive.getState().connect();
    last().serverOpen();
    last().serverClose(1008);
    expect(navigateToLogin).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(BACKOFF_CROWDED_MS - 1);
    expect(sockets()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets()).toHaveLength(2);
    // the new socket takes a fresh snapshot, which replaces whatever the slow one missed
    last().serverOpen();
    last().serverSend({ ...SNAPSHOT, state: { ...baseState(), epoch_count: 99 } });
    expect(useLive.getState().state?.epoch_count).toBe(99);
    // that open reset the ladder, so 1013 now waits the crowded floor again, not twice it
    last().serverClose(1013);
    await vi.advanceTimersByTimeAsync(BACKOFF_CROWDED_MS - 1);
    expect(sockets()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets()).toHaveLength(3);
    // and when that attempt fails without opening, the ladder continues from the floor: 10 s
    last().serverClose(1006, false);
    await vi.advanceTimersByTimeAsync(BACKOFF_CROWDED_MS * 2 - 1);
    expect(sockets()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(sockets()).toHaveLength(4);
    expect(navigateToLogin).not.toHaveBeenCalled();
  });

  it("a failed handshake with no known password is a reconnect, not a logout", async () => {
    useLive.getState().connect();
    last().failHandshake();
    expect(navigateToLogin).not.toHaveBeenCalled();
    expect(useLive.getState().status).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets()).toHaveLength(2);
  });

  it("a failed handshake with a password configured goes to /login once the API confirms the 401", async () => {
    configureLive({ passwordConfigured: () => true, probeAuth: async () => "unauthorized" });
    useLive.getState().connect();
    last().failHandshake();
    await vi.advanceTimersByTimeAsync(0);
    expect(navigateToLogin).toHaveBeenCalledTimes(1);
    // it keeps a slow retry going so a login in another tab, or on /login itself, is picked up
    expect(useLive.getState().status).toBe("reconnecting");
  });

  it("a failed handshake with a password configured but the API still answering is a reconnect", async () => {
    configureLive({ passwordConfigured: () => true, probeAuth: async () => "authorized" });
    useLive.getState().connect();
    last().failHandshake();
    await vi.advanceTimersByTimeAsync(0);
    expect(navigateToLogin).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets()).toHaveLength(2);
  });

  it("a failed handshake with a password configured while the daemon is down is a reconnect", async () => {
    configureLive({ passwordConfigured: () => true, probeAuth: async () => "unreachable" });
    useLive.getState().connect();
    last().failHandshake();
    await vi.advanceTimersByTimeAsync(0);
    expect(navigateToLogin).not.toHaveBeenCalled();
    expect(useLive.getState().status).toBe("reconnecting");
  });

  it("marks the data stale after 5 s without an epoch and closes a silent socket after 30 s", async () => {
    useLive.getState().connect();
    last().serverOpen();
    last().serverSend(SNAPSHOT);
    last().serverSend(epoch());
    expect(useLive.getState().stale).toBe(false);
    await vi.advanceTimersByTimeAsync(STALE_AFTER_MS + 1000);
    expect(useLive.getState().stale).toBe(true);
    expect(useLive.getState().status).toBe("open"); // stale is data freshness, not the link
    last().serverSend(update("system", "system.stats", { cpu_pct: 1, mem_pct: 1, disk_free_gb: 1, disk_used_pct: 1, uptime_s: 1, temp_c: null, load1: null, ts_utc: "t" }));
    last().serverSend(epoch());
    expect(useLive.getState().stale).toBe(false);
    // nothing at all for 30 s: the socket is presumed dead and closed, then reopened
    await vi.advanceTimersByTimeAsync(SILENT_CLOSE_MS + 1000);
    const dead = last();
    expect(dead.closedWith).not.toBeNull();
    expect(useLive.getState().status).toBe("reconnecting");
    dead.serverClose(1000); // the browser's close event for our own close()
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets()).toHaveLength(2);
  });

  it("a socket that never opens is given up on after the silent window too", async () => {
    useLive.getState().connect();
    await vi.advanceTimersByTimeAsync(SILENT_CLOSE_MS + 1000);
    expect(last().closedWith).not.toBeNull();
  });

  it("disconnect() closes the socket, clears every timer and does not reconnect", async () => {
    useLive.getState().connect();
    last().serverOpen();
    last().serverClose(1006, false);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    useLive.getState().disconnect();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(sockets()).toHaveLength(1);
    useLive.getState().connect();
    last().serverOpen();
    useLive.getState().disconnect();
    expect(last().closedWith?.code).toBe(1000);
    expect(useLive.getState().connected).toBe(false);
    last().serverClose(1000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(sockets()).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores events from a socket it has already replaced", async () => {
    useLive.getState().connect();
    const first = last();
    first.serverOpen();
    useLive.getState().disconnect();
    useLive.getState().connect();
    const second = last();
    second.serverOpen();
    first.serverClose(1006, false); // late close from the old socket
    first.serverSend(epoch()); // and a late frame
    expect(useLive.getState().status).toBe("open");
    expect(useLive.getState().state).toBeNull();
    await vi.advanceTimersByTimeAsync(SILENT_CLOSE_MS / 2); // short of the silent window
    expect(sockets()).toHaveLength(2);
    expect(useLive.getState().status).toBe("open");
  });

  it("does not open a second socket while an auth probe is still deciding", async () => {
    let resolve: (v: "authorized") => void = () => undefined;
    configureLive({ passwordConfigured: () => true, probeAuth: () => new Promise((r) => (resolve = r)) });
    useLive.getState().connect();
    last().failHandshake();
    useLive.getState().connect(); // e.g. a StrictMode re-run, or a page calling connect() again
    expect(sockets()).toHaveLength(1);
    resolve("authorized");
    await vi.advanceTimersByTimeAsync(0);
    expect(sockets()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(sockets()).toHaveLength(2);
  });
});
