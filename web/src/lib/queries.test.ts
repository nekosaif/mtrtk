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
});
