import { act, renderHook } from "@testing-library/react";
import { resetLiveForTests, useLive } from "./live";
import { RING_SIZE, pushRing, sampleOf, useEpochRing } from "./epochRing";
import { sampleState } from "@/test/fixtures";

describe("epochRing", () => {
  beforeEach(() => resetLiveForTests());

  it("pushRing appends without mutating and caps at the size", () => {
    const a = [1, 2];
    const b = pushRing(a, 3, 3);
    expect(a).toEqual([1, 2]);
    expect(b).toEqual([1, 2, 3]);
    expect(pushRing(b, 4, 3)).toEqual([2, 3, 4]);
    expect(RING_SIZE).toBe(120);
  });

  it("sampleOf takes hAcc, satellites used and the mean C/N0 over the used satellites", () => {
    const s = sampleOf(sampleState(), 1000);
    expect(s.t).toBe(1000);
    expect(s.hAcc).toBe(0.012);
    expect(s.nsatUsed).toBe(6);
    expect(s.cnoMean).toBeCloseTo((45 + 38 + 40 + 30 + 42 + 36) / 6, 6);
    const none = { ...sampleState(), sats: [] };
    expect(sampleOf(none, 1).cnoMean).toBeNull();
  });

  it("useEpochRing samples the current state and every new epoch, not other state changes", () => {
    useLive.setState({ state: sampleState(), lastEpochAt: 1000 });
    const { result } = renderHook(() => useEpochRing(3));
    expect(result.current).toHaveLength(1);
    act(() => useLive.setState({ state: { ...sampleState(), epoch_count: 121, accuracy: { ...sampleState().accuracy, h_acc_m: 0.014 } }, lastEpochAt: 2000 }));
    expect(result.current).toHaveLength(2);
    expect(result.current[1].hAcc).toBe(0.014);
    // a hardware update touches `state` but is not an epoch
    act(() => useLive.setState({ state: { ...useLive.getState().state!, hardware: null } }));
    expect(result.current).toHaveLength(2);
    act(() => useLive.setState({ state: { ...useLive.getState().state!, epoch_count: 122 }, lastEpochAt: 3000 }));
    act(() => useLive.setState({ state: { ...useLive.getState().state!, epoch_count: 123 }, lastEpochAt: 4000 }));
    expect(result.current).toHaveLength(3);
    expect(result.current[0].t).toBe(2000);
  });

  it("useEpochRing stays empty without a state", () => {
    const { result } = renderHook(() => useEpochRing());
    expect(result.current).toEqual([]);
  });
});
