/**
 * The live store: one WebSocket per tab, a reducer for every message the daemon sends, and the
 * reconnect/heartbeat rules from `docs/api.md` ("WebSocket /ws") as the Phase 4 ledger reads them.
 *
 * Reconnect matrix (keyed on what the browser can observe):
 * - a close before `open` ever fired = a failed handshake. The daemon answers HTTP 403 when the
 *   session is missing, but a daemon that is down looks identical from here, so: when the app
 *   knows a password is configured (`auth`), `GET /api/status` is probed — a 401 goes to /login,
 *   anything else is a reconnect; when no password is known, it is a reconnect.
 * - 1008 (fell 50 messages behind) and 1013 (32 sockets already) → reconnect after a longer
 *   backoff; the fresh socket's first message is a snapshot, which replaces what was missed.
 * - 1012 / 1001 / 1011 / 1006 / anything else → exponential backoff 1 s → 30 s.
 *
 * Heartbeat: `stale` is set when no `epoch` has arrived for 5 s while the socket is open (the
 * link is fine, the receiver is not producing); a socket that has carried nothing at all for
 * 30 s is presumed dead, closed, and reopened through the same backoff.
 */
import { create } from "zustand";
import { auth, goToLogin, wsUrl } from "./api";
import type {
  BaseMode,
  BaseModeEvent,
  Capabilities,
  ConsumerFailed,
  EventItem,
  Job,
  NtripClient,
  RawlogQueue,
  ReceiverState,
  ResetKind,
  SiteCheck,
  SystemStats,
  WsEpoch,
  WsMessage,
  WsUpdate,
} from "./types";

export type LiveStatus = "connecting" | "open" | "reconnecting";

export const STALE_AFTER_MS = 5000;
export const SILENT_CLOSE_MS = 30_000;
export const HEARTBEAT_MS = 1000;
export const BACKOFF_MIN_MS = 1000;
export const BACKOFF_MAX_MS = 30_000;
/** Floor for the retry after 1008/1013: the hub said "too fast" or "too many". */
export const BACKOFF_CROWDED_MS = 5000;
export const MAX_EVENTS = 200;
export const MAX_DAEMON_FAILURES = 20;

export interface BaseInfo {
  mode: BaseMode | null;
  site: string | null;
  /** Why the receiver refused the last mode change, or null. */
  reason: string | null;
  /** null until an RTCM 1005 has been compared with the active site. */
  verified: boolean | null;
  mismatch: SiteCheck | null;
}

export interface RawlogInfo {
  /** Path of the hour being written, from `rawlog.rotated`. */
  current: string | null;
  lastClosed: string | null;
  error: string | null;
  backpressure: boolean;
  queued: number | null;
}

export interface ConsumerFailure extends ConsumerFailed {
  at: number;
}

export interface ResetInfo {
  kind: ResetKind;
  at: number;
}

export interface LiveStore {
  /** Socket lifecycle. */
  status: LiveStatus;
  /** `status === "open"`. */
  connected: boolean;
  /** No epoch for `STALE_AFTER_MS` while open. Data freshness, not the link. */
  stale: boolean;
  /** Consecutive connects that did not stay up; 0 once one opens. */
  attempts: number;
  /** When the next connect is due while `reconnecting`. */
  nextRetryAt: number | null;
  role: string | null;
  topics: string[];
  state: ReceiverState | null;
  lastEpochAt: number | null;
  lastMessageAt: number | null;
  /** Receiver link as the daemon reports it; null until known. */
  receiverConnected: boolean | null;
  /** From `receiver.disconnected`; cleared on `receiver.connected`. */
  receiverReason: string | null;
  /** From `receiver.error`; the tape shows it until dismissed or the receiver reconnects. */
  receiverError: string | null;
  receiverCapabilities: Capabilities | null;
  receiverReset: ResetInfo | null;
  ntripClients: NtripClient[];
  /** Newest first, at most `MAX_EVENTS`. */
  events: EventItem[];
  system: SystemStats | null;
  base: BaseInfo;
  rawlog: RawlogInfo;
  /** Newest first, at most `MAX_DAEMON_FAILURES`. */
  daemonFailures: ConsumerFailure[];
  jobs: Record<string, Job>;
  applyMessage: (msg: WsMessage, now?: number) => void;
  connect: (token?: string | null) => void;
  disconnect: () => void;
  clearReceiverError: () => void;
}

// ------------------------------------------------------------- dependencies

export type AuthProbe = "unauthorized" | "authorized" | "unreachable";

interface LiveDeps {
  WebSocket: typeof WebSocket;
  now: () => number;
  navigateToLogin: () => void;
  passwordConfigured: () => boolean;
  probeAuth: () => Promise<AuthProbe>;
  log: (level: "debug" | "warn", message: string, ...rest: unknown[]) => void;
}

async function defaultProbe(): Promise<AuthProbe> {
  try {
    const r = await fetch("/api/status", { credentials: "same-origin" });
    return r.status === 401 ? "unauthorized" : "authorized";
  } catch {
    return "unreachable";
  }
}

function defaultLog(level: "debug" | "warn", message: string, ...rest: unknown[]): void {
  if (level === "warn") console.warn(message, ...rest);
  else if (import.meta.env.DEV) console.debug(message, ...rest);
}

const defaultDeps = (): LiveDeps => ({
  WebSocket: globalThis.WebSocket,
  now: () => Date.now(),
  navigateToLogin: goToLogin,
  passwordConfigured: () => auth.passwordConfigured(),
  probeAuth: defaultProbe,
  log: defaultLog,
});

let deps: LiveDeps = defaultDeps();

/** Test seam: swap the socket class, the clock, navigation or the auth probe. */
export function configureLive(overrides: Partial<LiveDeps>): void {
  deps = { ...deps, ...overrides };
}

// ------------------------------------------------------------------- reducer

const PVT_KEYS = ["position", "accuracy", "dops", "fix", "velocity", "time"] as const;
const SAT_KEYS = ["sats", "sat_summary"] as const;

const initialSlices = () => ({
  status: "connecting" as LiveStatus,
  connected: false,
  stale: true,
  attempts: 0,
  nextRetryAt: null as number | null,
  role: null as string | null,
  topics: [] as string[],
  state: null as ReceiverState | null,
  lastEpochAt: null as number | null,
  lastMessageAt: null as number | null,
  receiverConnected: null as boolean | null,
  receiverReason: null as string | null,
  receiverError: null as string | null,
  receiverCapabilities: null as Capabilities | null,
  receiverReset: null as ResetInfo | null,
  ntripClients: [] as NtripClient[],
  events: [] as EventItem[],
  system: null as SystemStats | null,
  base: { mode: null, site: null, reason: null, verified: null, mismatch: null } as BaseInfo,
  rawlog: { current: null, lastClosed: null, error: null, backpressure: false, queued: null } as RawlogInfo,
  daemonFailures: [] as ConsumerFailure[],
  jobs: {} as Record<string, Job>,
});

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** The keys of `section` that are in `keys`, and nothing else the server may have bundled. */
function pick<T extends object, K extends keyof T>(section: Partial<T>, keys: readonly K[]): Partial<Pick<T, K>> {
  const out: Partial<Pick<T, K>> = {};
  for (const key of keys) if (key in section) out[key] = section[key];
  return out;
}

function mergeEpoch(prev: ReceiverState, msg: WsEpoch): ReceiverState {
  const next: ReceiverState = { ...prev };
  if (msg.pvt) Object.assign(next, pick<ReceiverState, (typeof PVT_KEYS)[number]>(msg.pvt, PVT_KEYS));
  if (msg.sats) Object.assign(next, pick<ReceiverState, (typeof SAT_KEYS)[number]>(msg.sats, SAT_KEYS));
  if (msg.rtcm) next.rtcm_out = msg.rtcm;
  if (msg.svin) next.survey_in = msg.svin;
  next.epoch_count = prev.epoch_count + 1;
  return next;
}

export const useLive = create<LiveStore>((set, get) => ({
  ...initialSlices(),

  applyMessage: (msg, now = deps.now()) => {
    if (!isRecord(msg) || typeof msg.type !== "string") {
      deps.log("debug", "ws: message without a type", msg);
      return;
    }
    set({ lastMessageAt: now });
    if (msg.type === "snapshot") {
      const state = msg.state;
      set({
        state,
        role: msg.role,
        topics: msg.topics ?? [],
        receiverConnected: state.connected,
        lastEpochAt: state.epoch_count ? now : null,
        stale: !state.epoch_count,
      });
      return;
    }
    if (msg.type === "epoch") {
      const prev = get().state;
      if (!prev) {
        deps.log("debug", "ws: epoch before snapshot; ignored");
        return;
      }
      set({ state: mergeEpoch(prev, msg), lastEpochAt: now, stale: false });
      return;
    }
    if (msg.type === "update") {
      applyUpdate(msg, now, get, set);
      return;
    }
    deps.log("debug", "ws: unknown message type", (msg as { type: string }).type);
  },

  connect: (token) => connectSocket(token, get, set),

  disconnect: () => {
    manualClose = true;
    clearReconnect();
    stopHeartbeat();
    const ws = socket;
    socket = null;
    if (ws) closeQuietly(ws, 1000, "client closed");
    set({ status: "connecting", connected: false, nextRetryAt: null });
  },

  clearReceiverError: () => set({ receiverError: null }),
}));

type Get = () => LiveStore;
type Set = (partial: Partial<LiveStore>) => void;

function applyUpdate(msg: WsUpdate, now: number, get: Get, set: Set): void {
  const { source, data } = msg;
  const prev = get().state;
  const withState = (patch: Partial<ReceiverState>) => {
    if (prev) set({ state: { ...prev, ...patch } });
    else deps.log("debug", `ws: ${source} before snapshot; ignored`);
  };
  switch (source) {
    // ------------------------------------------------ receiver state sections
    case "state.hardware":
      withState({ hardware: data as ReceiverState["hardware"] });
      return;
    case "state.rf":
      withState({ rf: (Array.isArray(data) ? data : []) as ReceiverState["rf"] });
      return;
    case "state.spectrum":
      withState({ spectrum: (Array.isArray(data) ? data : []) as ReceiverState["spectrum"] });
      return;
    // ----------------------------------------------------------- side slices
    case "ntrip.clients":
      set({ ntripClients: Array.isArray(data) ? (data as NtripClient[]) : [] });
      return;
    case "events.new":
      if (!isRecord(data)) break;
      set({ events: [data as unknown as EventItem, ...get().events].slice(0, MAX_EVENTS) });
      return;
    case "system.stats":
      if (!isRecord(data)) break;
      set({ system: data as unknown as SystemStats });
      return;
    case "jobs.update": {
      if (!isRecord(data) || typeof data.id !== "string") break;
      const job = data as unknown as Job;
      set({ jobs: { ...get().jobs, [job.id]: job } });
      return;
    }
    // -------------------------------------------------------------- receiver
    case "receiver.connected":
      set({ receiverConnected: true, receiverError: null, receiverReason: null });
      if (prev) set({ state: { ...prev, connected: true, source: typeof data === "string" ? data : prev.source } });
      return;
    case "receiver.disconnected":
      set({ receiverConnected: false, receiverReason: typeof data === "string" ? data : null });
      if (prev) set({ state: { ...prev, connected: false } });
      return;
    case "receiver.error":
      set({ receiverError: typeof data === "string" ? data : JSON.stringify(data) });
      return;
    case "receiver.capabilities":
      if (!isRecord(data)) break;
      set({ receiverCapabilities: data as unknown as Capabilities });
      return;
    case "receiver.reset":
      if (!isRecord(data) || typeof data.kind !== "string") break;
      set({ receiverReset: { kind: data.kind as ResetKind, at: now } });
      return;
    // ------------------------------------------------------------------ base
    case "base.mode": {
      if (!isRecord(data)) break;
      const ev = data as unknown as BaseModeEvent;
      set({ base: { mode: ev.mode ?? null, site: ev.site ?? null, reason: ev.reason ?? null, verified: null, mismatch: null } });
      return;
    }
    case "base.site_verified": {
      const meta = isRecord(data) ? (data as unknown as SiteCheck) : null;
      const base = get().base;
      set({ base: { ...base, site: meta?.site ?? base.site, verified: true, mismatch: null } });
      return;
    }
    case "base.site_mismatch": {
      const meta = isRecord(data) ? (data as unknown as SiteCheck) : null;
      const base = get().base;
      set({ base: { ...base, site: meta?.site ?? base.site, verified: false, mismatch: meta } });
      return;
    }
    // ---------------------------------------------------------------- rawlog
    case "rawlog.rotated":
      set({ rawlog: { ...get().rawlog, current: typeof data === "string" ? data : null, error: null } });
      return;
    case "rawlog.closed": {
      const rawlog = get().rawlog;
      const path = typeof data === "string" ? data : null;
      set({ rawlog: { ...rawlog, lastClosed: path, current: rawlog.current === path ? null : rawlog.current } });
      return;
    }
    case "rawlog.pruned":
      deps.log("debug", "ws: raw log pruned", data);
      return;
    case "rawlog.error":
      set({ rawlog: { ...get().rawlog, error: typeof data === "string" ? data : JSON.stringify(data) } });
      return;
    case "rawlog.backpressure":
    case "rawlog.drained": {
      const q = isRecord(data) ? (data as unknown as RawlogQueue).queued : null;
      set({ rawlog: { ...get().rawlog, backpressure: source === "rawlog.backpressure", queued: typeof q === "number" ? q : null } });
      return;
    }
    // ---------------------------------------------------------------- daemon
    case "daemon.consumer_failed": {
      if (!isRecord(data)) break;
      const f = data as unknown as ConsumerFailed;
      const failure: ConsumerFailure = { name: String(f.name ?? "?"), error: String(f.error ?? ""), at: now };
      set({ daemonFailures: [failure, ...get().daemonFailures].slice(0, MAX_DAEMON_FAILURES) });
      return;
    }
    default:
      deps.log("debug", `ws: unhandled update ${msg.topic}/${source}`, data);
      return;
  }
  deps.log("debug", `ws: ${source} carried an unexpected payload`, data);
}

// -------------------------------------------------------------------- socket

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
let backoffMs = BACKOFF_MIN_MS;
let currentToken: string | null | undefined;
let manualClose = false;
let openedAt: number | null = null;
/** An auth probe is in flight after a failed handshake; the next step is decided when it lands. */
let probing = false;

function clearReconnect(): void {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  reconnectTimer = null;
}

function stopHeartbeat(): void {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function closeQuietly(ws: WebSocket, code: number, reason: string): void {
  try {
    ws.close(code, reason);
  } catch (err) {
    deps.log("debug", "ws: close failed", err);
  }
}

function connectSocket(token: string | null | undefined, get: Get, set: Set): void {
  if (token !== undefined) currentToken = token;
  if (socket || reconnectTimer || probing) return; // one socket per tab; connect() is idempotent
  manualClose = false;
  backoffMs = BACKOFF_MIN_MS;
  openSocket(get, set);
}

function scheduleReconnect(set: Set, floorMs = 0): void {
  if (manualClose) return;
  clearReconnect();
  const delay = Math.max(backoffMs, floorMs);
  backoffMs = Math.min(delay * 2, BACKOFF_MAX_MS);
  set({ status: "reconnecting", connected: false, nextRetryAt: deps.now() + delay });
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    openSocket(useLive.getState, useLive.setState);
  }, delay);
}

function startHeartbeat(ws: WebSocket, set: Set): void {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (ws !== socket) {
      stopHeartbeat();
      return;
    }
    const now = deps.now();
    const s = useLive.getState();
    const stale = s.lastEpochAt === null || now - s.lastEpochAt > STALE_AFTER_MS;
    if (stale !== s.stale && s.status === "open") set({ stale });
    // Nothing at all since the socket was created (or since the last frame): presumed dead.
    const lastSign = Math.max(s.lastMessageAt ?? 0, openedAt ?? 0);
    if (lastSign && now - lastSign > SILENT_CLOSE_MS) {
      deps.log("warn", `ws: nothing received for ${Math.round((now - lastSign) / 1000)} s; reconnecting`);
      stopHeartbeat();
      socket = null;
      closeQuietly(ws, 4000, "silent");
      scheduleReconnect(set);
    }
  }, HEARTBEAT_MS);
}

function openSocket(get: Get, set: Set): void {
  if (socket) return; // never two sockets, whatever path led here
  let ws: WebSocket;
  try {
    ws = new deps.WebSocket(wsUrl(currentToken));
  } catch (err) {
    // A malformed URL or a browser that refuses the scheme: not something a retry fixes fast.
    deps.log("warn", "ws: could not create the socket", err);
    scheduleReconnect(set, BACKOFF_MAX_MS);
    return;
  }
  socket = ws;
  openedAt = deps.now();
  let opened = false;
  set({ status: get().status === "reconnecting" ? "reconnecting" : "connecting", connected: false });
  startHeartbeat(ws, set);

  ws.onopen = () => {
    if (ws !== socket) return;
    opened = true;
    backoffMs = BACKOFF_MIN_MS;
    set({ status: "open", connected: true, attempts: 0, nextRetryAt: null });
  };
  ws.onmessage = (ev) => {
    if (ws !== socket) return;
    try {
      get().applyMessage(JSON.parse(ev.data as string) as WsMessage);
    } catch (err) {
      deps.log("warn", "ws: bad message", err);
    }
  };
  ws.onerror = () => {
    // The browser always follows an error with a close event; that is where the decision is made.
    deps.log("debug", "ws: error event");
  };
  ws.onclose = (ev) => {
    if (ws !== socket) return; // a socket we already replaced or closed on purpose
    socket = null;
    stopHeartbeat();
    set({ attempts: get().attempts + 1, connected: false });
    if (manualClose) return;
    if (!opened) {
      onHandshakeFailed(set);
      return;
    }
    if (ev.code === 1008 || ev.code === 1013) {
      deps.log("warn", `ws: closed ${ev.code} (${ev.code === 1008 ? "fell behind" : "too many clients"}); reconnecting with a longer backoff`);
      scheduleReconnect(set, BACKOFF_CROWDED_MS);
      return;
    }
    deps.log("debug", `ws: closed ${ev.code}; reconnecting`);
    scheduleReconnect(set);
  };
}

function onHandshakeFailed(set: Set): void {
  if (!deps.passwordConfigured()) {
    scheduleReconnect(set);
    return;
  }
  // Indistinguishable on the wire: an HTTP 403 (log in again) and a daemon that is down.
  set({ status: "reconnecting", connected: false });
  probing = true;
  void deps.probeAuth().then((verdict) => {
    probing = false;
    if (manualClose) return;
    if (verdict === "unauthorized") {
      deps.navigateToLogin();
      // Keep a slow retry going: a login on /login (or in another tab) sets the cookie this
      // socket needs, and the next attempt then succeeds without a reload.
      scheduleReconnect(set, BACKOFF_MAX_MS);
      return;
    }
    scheduleReconnect(set);
  });
}

// ------------------------------------------------------------------ helpers

/** True when no epoch has arrived for STALE_AFTER_MS. Pure; the store's `stale` is this, ticked. */
export function isStale(lastEpochAt: number | null, now = Date.now()): boolean {
  return lastEpochAt === null || now - lastEpochAt > STALE_AFTER_MS;
}

/** True when the readings should be greyed: no fresh epoch, or no socket to bring one. */
export function useStale(): boolean {
  return useLive((s) => s.stale || s.status !== "open");
}

/** Tests only: drop the socket, every timer and every slice; restore the default dependencies. */
export function resetLiveForTests(): void {
  manualClose = true;
  clearReconnect();
  stopHeartbeat();
  const ws = socket;
  socket = null;
  if (ws) closeQuietly(ws, 1000, "reset");
  backoffMs = BACKOFF_MIN_MS;
  currentToken = undefined;
  openedAt = null;
  probing = false;
  manualClose = false;
  deps = defaultDeps();
  useLive.setState(initialSlices());
}
