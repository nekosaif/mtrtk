import { act, renderHook } from "@testing-library/react";
import { resetLiveForTests, useLive } from "./live";
import { rfSampleOf, useRfRing } from "./rfRing";
import { sampleState } from "@/test/fixtures";

describe("rfRing", () => {
  beforeEach(() => resetLiveForTests());

  it("samples jamming, AGC and noise per RF block, in the receiver's order, and from MON-HW", () => {
    const s = rfSampleOf(sampleState(), 1000);
    expect(s.t).toBe(1000);
    expect(s.blocks).toEqual([
      { id: 0, jam: 10, agc: 4000, noise: 80 },
      { id: 1, jam: 5, agc: 5000, noise: 70 },
    ]);
    expect(s.hw).toEqual({ jam: 12, agc: 3000, noise: 90 });
    const bare = sampleState();
    bare.hardware = null;
    expect(rfSampleOf(bare, 1).hw).toBeNull();
  });

  it("seeds from the current state, appends when rf or hardware changes, ignores plain epochs", () => {
    useLive.setState({ state: sampleState(), lastEpochAt: 1000 });
    const { result } = renderHook(() => useRfRing(3));
    expect(result.current).toHaveLength(1);
    // an epoch alone (same rf/hardware references) is not an RF sample
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, epoch_count: prev.epoch_count + 1 }, lastEpochAt: 2000 });
    });
    expect(result.current).toHaveLength(1);
    // a MON-RF update is
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, rf: prev.rf.map((b) => ({ ...b, jam_ind: b.jam_ind + 1 })) } });
    });
    expect(result.current).toHaveLength(2);
    expect(result.current[1].blocks[0].jam).toBe(11);
    // and so is a MON-HW update
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, hardware: { ...prev.hardware!, jam_ind: 40 } } });
    });
    expect(result.current).toHaveLength(3);
    expect(result.current[2].hw?.jam).toBe(40);
    // the ring holds `size`
    act(() => {
      const prev = useLive.getState().state!;
      useLive.setState({ state: { ...prev, rf: prev.rf.map((b) => ({ ...b, jam_ind: 99 })) } });
    });
    expect(result.current).toHaveLength(3);
    expect(result.current[0].blocks[0].jam).toBe(11);
  });
});
