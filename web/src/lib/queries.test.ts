import { QueryClient } from "@tanstack/react-query";
import { resetLiveForTests, useLive } from "./live";
import { bindLiveToQueries } from "./queries";
import type { Job } from "./types";

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
