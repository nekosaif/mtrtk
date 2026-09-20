import { create } from "zustand";

/**
 * Placeholder live store. Task 2 replaces this file with the WebSocket client;
 * the field names below are the ones that store keeps, so `Tape` and any other
 * reader written against this shape keep compiling.
 */
export type LiveStatus = "connecting" | "open" | "reconnecting";

export interface LiveStore {
  /** WebSocket lifecycle. */
  status: LiveStatus;
  /** `status === "open"` — the socket is up, whatever the receiver is doing. */
  connected: boolean;
  /** `ReceiverState` snapshot once Task 2 lands; null until then. */
  state: null;
  /** `Date.now()` of the last epoch; null until the first one. */
  lastEpochAt: number | null;
  /** Receiver link as reported by the daemon; null until known. */
  receiverConnected: boolean | null;
}

export const useLive = create<LiveStore>(() => ({
  status: "connecting",
  connected: false,
  state: null,
  lastEpochAt: null,
  receiverConnected: null,
}));
