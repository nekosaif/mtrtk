/**
 * Per-browser preferences in `localStorage`, namespaced `mtrtk:`, JSON-encoded.
 *
 * Storage may be unavailable (private mode, blocked site data, quota): every access is wrapped
 * in try/catch and nothing here throws. A value that could not be written still lives in memory
 * for the rest of the page. `usePref` keeps every component on the same key in step within a
 * tab (module-level listeners) and across tabs (the `storage` event). Precedence when reading:
 * what this tab last set (until another tab overrides it), then what storage holds, then the
 * fallback.
 */
import { useCallback, useRef, useSyncExternalStore } from "react";
import { type CoordMode, isCoordMode } from "./format";

export const PREF_PREFIX = "mtrtk:";
/** The coordinate readout mode (`dd | dms | utm | ecef`), persisted per browser. */
export const COORD_MODE_PREF = "coordMode";

const MISSING = Symbol("missing");
type Parsed = { raw: string; value: unknown };

/** Last parse per key, so equal raw strings return the same reference (stable snapshots). */
const parsed = new Map<string, Parsed>();
/** Values set in this tab; they win over storage until another tab writes the key. */
const memory = new Map<string, unknown>();
const listeners = new Map<string, Set<() => void>>();
let storageBound = false;

function readRaw(full: string): string | null {
  try {
    return localStorage.getItem(full);
  } catch {
    return null;
  }
}

function parse(full: string, raw: string): unknown {
  const hit = parsed.get(full);
  if (hit && hit.raw === raw) return hit.value;
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    value = MISSING;
  }
  parsed.set(full, { raw, value });
  return value;
}

/** Read a preference; `fallback` when it is unset, unreadable or not valid JSON. */
export function getPref<T>(key: string, fallback: T): T {
  bindStorage();
  const full = PREF_PREFIX + key;
  if (memory.has(full)) return memory.get(full) as T;
  const raw = readRaw(full);
  if (raw != null) {
    const value = parse(full, raw);
    if (value !== MISSING) return value as T;
  }
  return fallback;
}

/** Write a preference (JSON); never throws. Components on the same key re-render. `undefined` removes it. */
export function setPref<T>(key: string, value: T): void {
  const full = PREF_PREFIX + key;
  const raw = JSON.stringify(value);
  if (raw === undefined) {
    memory.delete(full);
    parsed.delete(full);
  } else {
    memory.set(full, value);
    parsed.set(full, { raw, value });
  }
  try {
    if (raw === undefined) localStorage.removeItem(full);
    else localStorage.setItem(full, raw);
  } catch {
    // private mode, blocked storage or quota: the preference lives in `memory` for this page
  }
  notify(full);
}

function notify(full: string): void {
  listeners.get(full)?.forEach((cb) => cb());
}

function onStorage(e: StorageEvent): void {
  if (e.key == null) {
    // storage.clear() in another tab
    memory.clear();
    parsed.clear();
    listeners.forEach((set) => set.forEach((cb) => cb()));
    return;
  }
  if (!e.key.startsWith(PREF_PREFIX)) return;
  // the other tab's value now wins over anything this tab remembered
  memory.delete(e.key);
  if (e.newValue == null) parsed.delete(e.key);
  notify(e.key);
}

/** Listen for other tabs' writes from the start, so plain `getPref` callers see them too. */
function bindStorage(): void {
  if (!storageBound && typeof window !== "undefined") {
    window.addEventListener("storage", onStorage);
    storageBound = true;
  }
}
bindStorage();

function subscribePref(key: string, cb: () => void): () => void {
  const full = PREF_PREFIX + key;
  let set = listeners.get(full);
  if (!set) {
    set = new Set();
    listeners.set(full, set);
  }
  set.add(cb);
  bindStorage();
  return () => {
    set.delete(cb);
    if (set.size === 0) listeners.delete(full);
  };
}

/**
 * `const [mode, setMode] = usePref("coordMode", "dd")`. The fallback is taken from the first
 * render, so an inline object literal is safe; the setter identity is stable per key.
 */
export function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const fallbackRef = useRef(fallback);
  const subscribe = useCallback((cb: () => void) => subscribePref(key, cb), [key]);
  const read = useCallback(() => getPref<T>(key, fallbackRef.current), [key]);
  const value = useSyncExternalStore(subscribe, read, read);
  const set = useCallback((v: T) => setPref(key, v), [key]);
  return [value, set];
}

/** The default before the operator picks one: the plan's hero shows degrees, minutes, seconds. */
export const DEFAULT_COORD_MODE: CoordMode = "dms";

/** The persisted coordinate mode, validated (anything unexpected reads as the default). */
export function useCoordMode(): [CoordMode, (m: CoordMode) => void] {
  const [raw, set] = usePref<unknown>(COORD_MODE_PREF, DEFAULT_COORD_MODE);
  return [isCoordMode(raw) ? raw : DEFAULT_COORD_MODE, set];
}

/** Tests only: forget every cached value and hook listener (does not touch localStorage). */
export function resetPrefsForTests(): void {
  parsed.clear();
  memory.clear();
  listeners.clear();
}
