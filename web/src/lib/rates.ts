/**
 * Message rates for the Corrections page. The daemon reports cumulative `count` and `bytes` per
 * RTCM type; the rate is the delta over a sliding window of samples, one per epoch. Page-local
 * (a hook per page), like `useEpochRing`.
 */
import { useEffect, useRef, useState } from "react";
import type { RtcmMsgStats } from "./types";

export type Counts = Record<string, RtcmMsgStats>;

export interface Rate {
  hz: number;
  bytesPerS: number;
  /** The latest cumulative count and bytes the daemon reported for the type. */
  count: number;
  bytes: number;
}

export type Rates = Record<string, Rate>;

/** Window the rates are averaged over. */
export const WINDOW_S = 10;
/** A type missing from the daemon's map for this long reads 0 Hz; it stays listed with its last count. */
export const ABSENT_AFTER_MS = 30_000;

interface Sample {
  t: number;
  counts: Record<string, { count: number; bytes: number }>;
}

interface Known {
  count: number;
  bytes: number;
  /** When the type was last present in a pushed map. */
  seenAt: number;
  /** The rate computed the last time the type was present. */
  hz: number;
  bytesPerS: number;
}

const perSecond = (delta: number, dtS: number) => (dtS > 0 && delta > 0 ? delta / dtS : 0);

/**
 * Push the daemon's per-type map once per epoch with the sample time (ms); `rates()` averages
 * over the samples inside the window — always at least two, so a slow epoch cadence still gives
 * a number (the brief's contract). A type that vanishes from the map keeps its last rate and count
 * until `ABSENT_AFTER_MS`, then reads 0 Hz. A counter that went backwards (daemon restart) reads 0
 * until the window has moved past the reset.
 */
export class RateTracker {
  private samples: Sample[] = [];
  private known = new Map<string, Known>();
  constructor(private windowS = WINDOW_S) {}

  /** Samples currently retained. */
  get size(): number {
    return this.samples.length;
  }

  /** True once two samples exist, i.e. a rate is a measurement rather than a placeholder. */
  get ready(): boolean {
    return this.samples.length >= 2;
  }

  push(messages: Counts, tMs: number): void {
    const counts: Sample["counts"] = {};
    for (const [k, v] of Object.entries(messages)) {
      counts[k] = { count: v.count, bytes: v.bytes };
      const prev = this.known.get(k);
      this.known.set(k, { count: v.count, bytes: v.bytes, seenAt: tMs, hz: prev?.hz ?? 0, bytesPerS: prev?.bytesPerS ?? 0 });
    }
    this.samples.push({ t: tMs, counts });
    const cutoff = tMs - this.windowS * 1000;
    while (this.samples.length > 2 && this.samples[0].t < cutoff) this.samples.shift();
    this.refresh();
  }

  rates(): Rates {
    const out: Rates = {};
    const now = this.samples.length ? this.samples[this.samples.length - 1].t : 0;
    for (const [k, r] of this.known) {
      const gone = now - r.seenAt > ABSENT_AFTER_MS;
      out[k] = { hz: gone ? 0 : r.hz, bytesPerS: gone ? 0 : r.bytesPerS, count: r.count, bytes: r.bytes };
    }
    return out;
  }

  /** Recompute the rate of every type present in the newest sample from the oldest sample that has it. */
  private refresh(): void {
    if (this.samples.length < 2) return;
    const last = this.samples[this.samples.length - 1];
    for (const [k, v] of Object.entries(last.counts)) {
      const first = this.samples.find((s) => k in s.counts)!;
      const r = this.known.get(k)!;
      if (first === last) {
        r.hz = 0;
        r.bytesPerS = 0;
        continue;
      }
      const dt = (last.t - first.t) / 1000;
      r.hz = perSecond(v.count - first.counts[k].count, dt);
      r.bytesPerS = perSecond(v.bytes - first.counts[k].bytes, dt);
    }
  }
}

const readRates = (tr: RateTracker): Rates => (tr.ready ? tr.rates() : {});

/**
 * Hz and bytes/s per RTCM type, from one sample per distinct `messages` object (the live store
 * replaces `rtcm_out` on every epoch). Pass the same object between epochs and nothing is
 * sampled. Empty until two samples exist, so a page can show a dash instead of a false 0 Hz.
 */
export function useMessageRates(messages: Counts): Rates {
  const tracker = useRef<RateTracker | null>(null);
  tracker.current ??= new RateTracker(WINDOW_S);
  const [rates, setRates] = useState<Rates>(() => {
    tracker.current!.push(messages, Date.now());
    return readRates(tracker.current!);
  });
  const seeded = useRef(messages);
  useEffect(() => {
    if (seeded.current === messages) return; // the mount sample was taken in the state initialiser
    seeded.current = messages;
    tracker.current!.push(messages, Date.now());
    setRates(readRates(tracker.current!));
  }, [messages]);
  return rates;
}

export interface RingPoint {
  /** Sample ms. */
  t: number;
  v: number;
}

/**
 * The last `seconds` values of a live number, sampled once per second on a timer (so the ring
 * advances even while the value holds still), seeded at mount, oldest first. A `null` value is
 * "nothing to measure yet" (no snapshot): no point is taken, so the chart never starts at a false 0.
 */
export function useRing(value: number | null, seconds = 300): RingPoint[] {
  const latest = useRef(value);
  latest.current = value;
  const [ring, setRing] = useState<RingPoint[]>(() => (value == null ? [] : [{ t: Date.now(), v: value }]));
  useEffect(() => {
    const id = setInterval(() => {
      const v = latest.current;
      if (v == null) return;
      setRing((r) => {
        const next = [...r, { t: Date.now(), v }];
        return next.length > seconds ? next.slice(next.length - seconds) : next;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [seconds]);
  return ring;
}
