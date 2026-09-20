import { act, renderHook } from "@testing-library/react";
import { ABSENT_AFTER_MS, RateTracker, useMessageRates, useRing } from "./rates";
import type { RtcmMsgStats } from "./types";

const m = (count: number, bytes: number): RtcmMsgStats => ({ count, bytes, last_seen_mono: 1 });

describe("RateTracker", () => {
  // ---- the brief's acceptance tests --------------------------------------------------------

  it("computes Hz and bytes/s from count deltas", () => {
    const tr = new RateTracker(10);
    tr.push({ "1077": { count: 100, bytes: 40_000, last_seen_mono: 1 } }, 1000);
    tr.push({ "1077": { count: 105, bytes: 42_000, last_seen_mono: 1 } }, 6000);
    expect(tr.rates()["1077"].hz).toBeCloseTo(1.0, 5);
    expect(tr.rates()["1077"].bytesPerS).toBeCloseTo(400, 5);
  });

  it("drops samples older than the window and handles new types", () => {
    const tr = new RateTracker(10);
    tr.push({ "1005": { count: 1, bytes: 25, last_seen_mono: 1 } }, 0);
    tr.push({ "1005": { count: 2, bytes: 50, last_seen_mono: 1 } }, 5000);
    tr.push({ "1005": { count: 4, bytes: 100, last_seen_mono: 1 }, "1230": { count: 1, bytes: 10, last_seen_mono: 1 } }, 20000);
    expect(tr.rates()["1005"].hz).toBeCloseTo(2 / 15, 5);
    expect(tr.rates()["1230"].hz).toBe(0);
  });

  // ---- rulings ----------------------------------------------------------------------------

  it("uses a sliding window: only the last 10 s of samples count", () => {
    const tr = new RateTracker(10);
    // 1 Hz for 10 s, then 3 Hz for 10 s: the window must forget the slow stretch.
    for (let t = 0; t <= 10_000; t += 1000) {
      const count = t / 1000;
      tr.push({ "1077": m(count, count * 400) }, t);
    }
    for (let t = 11_000; t <= 20_000; t += 1000) {
      const count = 10 + (3 * (t - 10_000)) / 1000;
      tr.push({ "1077": m(count, count * 400) }, t);
    }
    expect(tr.rates()["1077"].hz).toBeCloseTo(3, 5);
    expect(tr.rates()["1077"].bytesPerS).toBeCloseTo(1200, 5);
    expect(tr.size).toBeLessThanOrEqual(12);
  });

  it("reports the latest count and bytes with every rate", () => {
    const tr = new RateTracker(10);
    tr.push({ "1005": m(1, 25) }, 0);
    tr.push({ "1005": m(3, 75) }, 2000);
    expect(tr.rates()["1005"]).toMatchObject({ count: 3, bytes: 75, hz: 1, bytesPerS: 25 });
  });

  it("keeps a type that vanished from the map at its last rate and count, then drops it to zero after 30 s", () => {
    const tr = new RateTracker(10);
    tr.push({ "1005": m(1, 25), "1230": m(1, 10) }, 0);
    tr.push({ "1005": m(2, 50), "1230": m(2, 20) }, 1000);
    expect(tr.rates()["1230"].hz).toBeCloseTo(1, 5);
    // the daemon stops listing 1230
    for (let t = 2000; t <= 29_000; t += 1000) tr.push({ "1005": m(t / 1000 + 1, 0) }, t);
    let r = tr.rates()["1230"];
    expect(r).toBeDefined();
    expect(r.count).toBe(2);
    expect(r.hz).toBeCloseTo(1, 5); // frozen at the last known rate while it is merely missing
    tr.push({ "1005": m(31, 0) }, 1000 + ABSENT_AFTER_MS + 1);
    r = tr.rates()["1230"];
    expect(r.hz).toBe(0);
    expect(r.bytesPerS).toBe(0);
    expect(r.count).toBe(2); // still listed with its last count
  });

  it("never reports a negative rate when the daemon's counters reset", () => {
    const tr = new RateTracker(10);
    tr.push({ "1077": m(500, 200_000) }, 0);
    tr.push({ "1077": m(3, 1200) }, 1000);
    expect(tr.rates()["1077"].hz).toBe(0);
    expect(tr.rates()["1077"].bytesPerS).toBe(0);
  });
});

describe("useMessageRates", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("samples once per messages object and returns the rates over the window", () => {
    vi.setSystemTime(new Date("2026-09-18T16:47:00Z"));
    const first = { "1077": m(100, 40_000) };
    const { result, rerender } = renderHook(({ messages }: { messages: Record<string, RtcmMsgStats> }) => useMessageRates(messages), { initialProps: { messages: first } });
    expect(result.current).toEqual({}); // one sample: no rate yet
    // the same object again is not a new sample
    rerender({ messages: first });
    expect(result.current).toEqual({});
    act(() => vi.advanceTimersByTime(5000));
    rerender({ messages: { "1077": m(105, 42_000) } });
    expect(result.current["1077"].hz).toBeCloseTo(1, 5);
    expect(result.current["1077"].bytesPerS).toBeCloseTo(400, 5);
  });
});

describe("useRing", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("samples the latest value once per second and caps the ring at `seconds` points", () => {
    vi.setSystemTime(new Date("2026-09-18T16:47:00Z"));
    let value = 1000;
    const { result, rerender } = renderHook(() => useRing(value, 3));
    expect(result.current.map((p) => p.v)).toEqual([1000]); // seeded at mount
    value = 1100;
    rerender();
    expect(result.current).toHaveLength(1); // a new value alone does not add a point
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current.map((p) => p.v)).toEqual([1000, 1100]);
    value = 1200;
    rerender();
    act(() => vi.advanceTimersByTime(3000));
    expect(result.current).toHaveLength(3);
    expect(result.current.map((p) => p.v)).toEqual([1200, 1200, 1200]);
    expect(result.current[2].t - result.current[0].t).toBe(2000);
  });

  it("stops sampling when unmounted", () => {
    const { result, unmount } = renderHook(() => useRing(5, 10));
    unmount();
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current).toHaveLength(1);
  });
});
