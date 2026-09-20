import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { COORD_MODE_PREF, PREF_PREFIX, getPref, resetPrefsForTests, setPref, useCoordMode, usePref } from "./prefs";

function otherTabWrites(key: string, value: unknown | null) {
  const full = PREF_PREFIX + key;
  if (value === null) localStorage.removeItem(full);
  else localStorage.setItem(full, JSON.stringify(value));
  window.dispatchEvent(new StorageEvent("storage", { key: full, newValue: value === null ? null : JSON.stringify(value), storageArea: localStorage }));
}

describe("prefs", () => {
  beforeEach(() => {
    localStorage.clear();
    resetPrefsForTests();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("getPref/setPref round-trip through a namespaced localStorage key", () => {
    expect(getPref("coordMode", "dd")).toBe("dd");
    setPref("coordMode", "utm");
    expect(localStorage.getItem("mtrtk:coordMode")).toBe('"utm"');
    expect(getPref("coordMode", "dd")).toBe("utm");
    setPref("panel", { open: true, n: 2 });
    expect(getPref("panel", { open: false, n: 0 })).toEqual({ open: true, n: 2 });
    expect(PREF_PREFIX).toBe("mtrtk:");
  });

  it("falls back on corrupt or foreign values", () => {
    localStorage.setItem("mtrtk:coordMode", "{not json");
    expect(getPref("coordMode", "dd")).toBe("dd");
    localStorage.setItem("coordMode", '"utm"'); // un-namespaced: not ours
    expect(getPref("coordMode", "dd")).toBe("dd");
  });

  it("never throws when storage is blocked; the value lives on for this page", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("QuotaExceededError", "QuotaExceededError");
    });
    expect(getPref("coordMode", "dd")).toBe("dd");
    expect(() => setPref("coordMode", "dms")).not.toThrow();
    expect(getPref("coordMode", "dd")).toBe("dms");
    expect(getPref("other", 7)).toBe(7);
  });

  it("a write that fails on an existing key still wins in this tab until another tab speaks", () => {
    setPref("coordMode", "utm"); // stored fine
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("QuotaExceededError", "QuotaExceededError");
    });
    setPref("coordMode", "dms"); // quota: storage keeps "utm"
    expect(localStorage.getItem("mtrtk:coordMode")).toBe('"utm"');
    expect(getPref("coordMode", "dd")).toBe("dms");
    vi.restoreAllMocks();
    // the other tab's value takes over, hook or no hook mounted
    otherTabWrites("coordMode", "ecef");
    expect(getPref("coordMode", "dd")).toBe("ecef");
  });

  it("setPref(key, undefined) removes the preference", () => {
    setPref("coordMode", "utm");
    setPref("coordMode", undefined);
    expect(localStorage.getItem("mtrtk:coordMode")).toBeNull();
    expect(getPref("coordMode", "dd")).toBe("dd");
  });

  it("usePref reads, writes, and keeps two hooks on the same key in step within a tab", () => {
    const a = renderHook(() => usePref<string>("coordMode", "dd"));
    const b = renderHook(() => usePref<string>("coordMode", "dd"));
    expect(a.result.current[0]).toBe("dd");
    act(() => a.result.current[1]("utm"));
    expect(a.result.current[0]).toBe("utm");
    expect(b.result.current[0]).toBe("utm");
    expect(localStorage.getItem("mtrtk:coordMode")).toBe('"utm"');
    // the setter identity is stable
    const setter = a.result.current[1];
    a.rerender();
    expect(a.result.current[1]).toBe(setter);
  });

  it("usePref follows another tab through the storage event", () => {
    const { result } = renderHook(() => usePref<string>("coordMode", "dd"));
    expect(result.current[0]).toBe("dd");
    act(() => otherTabWrites("coordMode", "ecef"));
    expect(result.current[0]).toBe("ecef");
    act(() => otherTabWrites("coordMode", null)); // the other tab reset it
    expect(result.current[0]).toBe("dd");
    // an unrelated key does not disturb us
    act(() => otherTabWrites("theme", "dark"));
    expect(result.current[0]).toBe("dd");
  });

  it("usePref survives a whole-storage clear from another tab", () => {
    setPref("coordMode", "utm");
    const { result } = renderHook(() => usePref<string>("coordMode", "dd"));
    expect(result.current[0]).toBe("utm");
    act(() => {
      localStorage.clear();
      window.dispatchEvent(new StorageEvent("storage", { key: null, newValue: null, storageArea: localStorage }));
    });
    expect(result.current[0]).toBe("dd");
  });

  it("usePref unsubscribes on unmount", () => {
    const { result, unmount } = renderHook(() => usePref<string>("coordMode", "dd"));
    unmount();
    expect(() => otherTabWrites("coordMode", "utm")).not.toThrow();
    expect(result.current[0]).toBe("dd"); // no update after unmount, no error either
  });

  it("useCoordMode validates what it finds and persists what it is given", () => {
    localStorage.setItem(PREF_PREFIX + COORD_MODE_PREF, '"bogus"');
    const { result } = renderHook(() => useCoordMode());
    expect(result.current[0]).toBe("dd");
    act(() => result.current[1]("utm"));
    expect(result.current[0]).toBe("utm");
    expect(localStorage.getItem("mtrtk:coordMode")).toBe('"utm"');
    expect(getPref(COORD_MODE_PREF, "dd")).toBe("utm");
  });
});
