import { createElement, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { resetLiveForTests, useLive } from "./live";
import { bindLiveToQueries, useCasterClients } from "./queries";
import type { Job } from "./types";
import { sampleRover, sampleState } from "@/test/fixtures";

const job: Job = { id: "j1", kind: "export", status: "running", created_utc: "2026-09-18T11:30:00+00:00", updated_utc: "2026-09-18T11:30:10+00:00", progress: 0.2, message: null, params: {}, result: null, error: null };

describe("bindLiveToQueries", () => {
  beforeEach(() => resetLiveForTests());

  it("refreshes the job lists on a jobs.update but leaves finished jobs' file listings alone", () => {
    const qc = new QueryClient();
    qc.setQueryData(["jobs", "export", 50], []);
    qc.setQueryData(["jobs", "one", "j1"], job);
    qc.setQueryData(["jobs", "files", "j0"], []);
    const off = bindLiveToQueries(qc);
    try {
      useLive.setState({ jobs: { j1: job } });
      expect(qc.getQueryState(["jobs", "export", 50])?.isInvalidated).toBe(true);
      expect(qc.getQueryState(["jobs", "one", "j1"])?.isInvalidated).toBe(true);
      // one export publishes ~7 updates; every done row refetching its files on each is a GET storm
      expect(qc.getQueryState(["jobs", "files", "j0"])?.isInvalidated).toBe(false);
    } finally {
      off();
    }
  });

  it("drops a job deleted elsewhere from every cached listing at once, then refreshes them", () => {
    const qc = new QueryClient();
    const other: Job = { ...job, id: "j2", kind: "ppk" };
    qc.setQueryData(["jobs", "all", 50], [job, other], { updatedAt: 1_000 });
    qc.setQueryData(["jobs", "export", 50], [job], { updatedAt: 2_000 });
    qc.setQueryData(["jobs", "ppk", 50], [other], { updatedAt: 3_000 });
    qc.setQueryData(["jobs", "files", "j1"], [{ name: "a.obs", bytes: 1 }]);
    qc.setQueryData(["jobs", "files", "j2"], [{ name: "b.pos", bytes: 1 }]);
    const off = bindLiveToQueries(qc);
    try {
      useLive.getState().applyMessage({ type: "update", topic: "jobs", source: "jobs.deleted", data: { id: "j1", deleted: true } });
      expect(qc.getQueryData(["jobs", "all", 50])).toEqual([other]);
      expect(qc.getQueryData(["jobs", "export", 50])).toEqual([]);
      expect(qc.getQueryData(["jobs", "ppk", 50])).toEqual([other]);
      // When each listing was read is untouched: it is what tells a newer live job from a deleted one.
      expect(qc.getQueryState(["jobs", "all", 50])?.dataUpdatedAt).toBe(1_000);
      expect(qc.getQueryState(["jobs", "export", 50])?.dataUpdatedAt).toBe(2_000);
      expect(qc.getQueryState(["jobs", "all", 50])?.isInvalidated).toBe(true);
      expect(qc.getQueryState(["jobs", "files", "j1"])).toBeUndefined(); // its files are gone with it
      expect(qc.getQueryState(["jobs", "files", "j2"])?.isInvalidated).toBe(false);
    } finally {
      off();
    }
  });

  it("refreshes the survey points when the socket reports a stored point, and nothing else of the rover", () => {
    const qc = new QueryClient();
    qc.setQueryData(["rover", "points", "all"], []);
    qc.setQueryData(["rover", "points", 3], []);
    qc.setQueryData(["rover", "sessions"], []);
    const off = bindLiveToQueries(qc);
    try {
      useLive.setState({ lastSavedPointId: 9 });
      expect(qc.getQueryState(["rover", "points", "all"])?.isInvalidated).toBe(true);
      expect(qc.getQueryState(["rover", "points", 3])?.isInvalidated).toBe(true);
      expect(qc.getQueryState(["rover", "sessions"])?.isInvalidated).toBe(false);
    } finally {
      off();
    }
  });

  it("does not refresh the points when a fresh snapshot clears the last saved id", () => {
    const qc = new QueryClient();
    useLive.setState({ lastSavedPointId: 9 });
    qc.setQueryData(["rover", "points", "all"], []);
    const off = bindLiveToQueries(qc);
    try {
      useLive.setState({ lastSavedPointId: null });
      expect(qc.getQueryState(["rover", "points", "all"])?.isInvalidated).toBe(false);
    } finally {
      off();
    }
  });
});

describe("useCasterClients", () => {
  const clientsUpdate = (data: unknown) => ({ type: "update", topic: "ntrip", source: "ntrip.clients", data }) as never;
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  function renderClients(enabled = true) {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const off = bindLiveToQueries(qc);
    const wrapper = ({ children }: { children: ReactNode }) => createElement(QueryClientProvider, { client: qc }, children);
    const hook = renderHook(() => useCasterClients(enabled), { wrapper });
    return { ...hook, off, qc };
  }

  beforeEach(() => resetLiveForTests());

  it("drops the last rover the moment the socket says it left, whatever the REST list still holds", async () => {
    // The GET answered before the rover left; its refetch is in flight (or failing on a bad link).
    globalThis.fetch = vi.fn(async () => json([sampleRover()])) as typeof fetch;
    const { result, off } = renderClients();
    try {
      await waitFor(() => expect(result.current).toHaveLength(1)); // the socket has not spoken yet
      act(() => useLive.getState().applyMessage(clientsUpdate([sampleRover()])));
      expect(result.current).toHaveLength(1);
      act(() => useLive.getState().applyMessage(clientsUpdate([])));
      expect(result.current).toEqual([]);
    } finally {
      off();
    }
  });

  it("goes back to the REST list after a reconnect, until the new process lists its rovers", async () => {
    globalThis.fetch = vi.fn(async () => json([sampleRover({ id: 7 })])) as typeof fetch;
    act(() => useLive.getState().applyMessage(clientsUpdate([])));
    const { result, off, qc } = renderClients();
    try {
      await waitFor(() => expect(qc.getQueryData(["ntrip", "clients"])).toHaveLength(1));
      expect(result.current).toEqual([]); // the socket said none, and it outranks the poll
      act(() => useLive.getState().applyMessage({ type: "snapshot", role: "base", state: sampleState() } as never));
      await waitFor(() => expect(result.current.map((c) => c.id)).toEqual([7]));
    } finally {
      off();
    }
  });

  it("takes a quiet rover's fresher counters from the REST poll, never a rover the socket says left", async () => {
    // The socket lists a rover only on connect, leave or GGA: one that sent a single GGA at connect
    // keeps its connect-time bytes there while the 5 s poll has the caster's current figure.
    const early = sampleRover({ id: 1, bytes_sent: 1_000, last_gga_lat: 23.1, last_gga_utc: "2026-09-18T16:40:01+00:00" });
    const polled = sampleRover({ id: 1, bytes_sent: 250_000, last_gga_lat: 23.2, last_gga_utc: "2026-09-18T16:47:00+00:00" });
    globalThis.fetch = vi.fn(async () => json([polled, sampleRover({ id: 2 })])) as typeof fetch;
    act(() => useLive.getState().applyMessage(clientsUpdate([early])));
    const { result, off } = renderClients();
    try {
      await waitFor(() => expect(result.current[0].bytes_sent).toBe(250_000));
      expect(result.current.map((c) => c.id)).toEqual([1]);
      expect(result.current[0].last_gga_lat).toBe(23.2);
      expect(result.current[0].last_gga_utc).toBe("2026-09-18T16:47:00+00:00");
      // and a socket figure newer than the poll wins in its turn
      const later = sampleRover({ id: 1, bytes_sent: 300_000, last_gga_lat: 23.3, last_gga_utc: "2026-09-18T16:48:00+00:00" });
      act(() => useLive.getState().applyMessage(clientsUpdate([later])));
      expect(result.current[0]).toMatchObject({ bytes_sent: 300_000, last_gga_lat: 23.3 });
    } finally {
      off();
    }
  });

  it("on a rover role reads nothing and polls nothing", async () => {
    globalThis.fetch = vi.fn(async () => json([sampleRover()])) as typeof fetch;
    const { result, off } = renderClients(false);
    try {
      await act(async () => {});
      expect(result.current).toEqual([]);
      expect(globalThis.fetch).not.toHaveBeenCalled();
    } finally {
      off();
    }
  });
});
