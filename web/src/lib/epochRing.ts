/**
 * A page-local ring of the last epochs for the dashboard sparklines. Kept in the component
 * tree (a hook), not in the live store: nothing else needs it, and it resets with the page.
 */
import { useEffect, useState } from "react";
import { useLive } from "./live";
import type { ReceiverState } from "./types";

export const RING_SIZE = 120;

export interface EpochSample {
  /** Epoch ms (the store's `lastEpochAt`). */
  t: number;
  hAcc: number | null;
  nsatUsed: number;
  /** Mean C/N0 over the satellites used in the fix (as the history sampler defines it). */
  cnoMean: number | null;
}

/** Append without mutating; the oldest entries fall off past `size`. */
export function pushRing<T>(ring: readonly T[], item: T, size: number): T[] {
  const next = [...ring, item];
  return next.length > size ? next.slice(next.length - size) : next;
}

export function sampleOf(state: ReceiverState, t: number): EpochSample {
  const used = state.sats.filter((s) => s.used && Number.isFinite(s.cno) && s.cno > 0);
  return {
    t,
    hAcc: state.accuracy.h_acc_m,
    nsatUsed: state.sat_summary.used,
    cnoMean: used.length ? used.reduce((sum, s) => sum + s.cno, 0) / used.length : null,
  };
}

/** The last `size` epochs seen while mounted, oldest first; the current state seeds it. */
export function useEpochRing(size = RING_SIZE): EpochSample[] {
  const [ring, setRing] = useState<EpochSample[]>([]);
  useEffect(() => {
    const push = (state: ReceiverState, t: number) => setRing((r) => pushRing(r, sampleOf(state, t), size));
    const now = useLive.getState();
    setRing([]);
    if (now.state) push(now.state, now.lastEpochAt ?? Date.now());
    return useLive.subscribe((next, prev) => {
      if (!next.state || next.state === prev.state) return;
      const isEpoch = next.state.epoch_count !== prev.state?.epoch_count || next.lastEpochAt !== prev.lastEpochAt;
      if (isEpoch) push(next.state, next.lastEpochAt ?? Date.now());
    });
  }, [size]);
  return ring;
}
