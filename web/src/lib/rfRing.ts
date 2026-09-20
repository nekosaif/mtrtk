/**
 * A page-local ring of the last RF health readings for the Receiver page's trend sparklines:
 * the sibling of `useEpochRing`. Samples on every MON-RF (`state.rf`) or MON-HW
 * (`state.hardware`) update, not on epochs, and lives in the component tree so it resets with
 * the page.
 */
import { useEffect, useState } from "react";
import { pushRing } from "./epochRing";
import { useLive } from "./live";
import type { ReceiverState } from "./types";

export const RF_RING_SIZE = 120;

export interface RfReading {
  jam: number;
  agc: number;
  noise: number;
}

export interface RfSample {
  /** Sample ms (wall clock at the update). */
  t: number;
  /** One entry per RF block, in the receiver's order (index-aligned with `state.rf`). */
  blocks: (RfReading & { id: number })[];
  /** The legacy MON-HW view, when the receiver sends it. */
  hw: RfReading | null;
}

export function rfSampleOf(state: ReceiverState, t: number): RfSample {
  return {
    t,
    blocks: state.rf.map((b) => ({ id: b.block_id, jam: b.jam_ind, agc: b.agc_cnt, noise: b.noise_per_ms })),
    hw: state.hardware ? { jam: state.hardware.jam_ind, agc: state.hardware.agc_cnt, noise: state.hardware.noise_per_ms } : null,
  };
}

/** The last `size` RF readings seen while mounted, oldest first; the current state seeds it. */
export function useRfRing(size = RF_RING_SIZE): RfSample[] {
  const [ring, setRing] = useState<RfSample[]>([]);
  useEffect(() => {
    const push = (state: ReceiverState) => setRing((r) => pushRing(r, rfSampleOf(state, Date.now()), size));
    const now = useLive.getState();
    setRing([]);
    if (now.state) push(now.state);
    return useLive.subscribe((next, prev) => {
      if (!next.state || next.state === prev.state) return;
      if (next.state.rf !== prev.state?.rf || next.state.hardware !== prev.state?.hardware) push(next.state);
    });
  }, [size]);
  return ring;
}
