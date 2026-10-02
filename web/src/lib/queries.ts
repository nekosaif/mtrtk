/**
 * TanStack Query hooks, one per read route. Every URL comes from `ROUTES` so the contract test
 * covers it. Live data (the receiver state, NTRIP clients, events, jobs, base mode) also arrives
 * on the WebSocket; `bindLiveToQueries` invalidates the matching queries when it does, so a page
 * that reads a query stays fresh without polling hard.
 */
import { keepPreviousData, useQuery, type QueryClient } from "@tanstack/react-query";
import { ROUTES, fetchConfig, get, route } from "./api";
import { useLive } from "./live";
import type {
  BaseModeView,
  EventItem,
  HistoryMetrics,
  HistoryResponse,
  HourSlot,
  Job,
  JobFile,
  Level,
  LogsResponse,
  NtripClient,
  NtripHistoryRecord,
  NtripInfo,
  Point,
  PpkDefaults,
  Preset,
  ReceiverInfo,
  RoverOverview,
  Session,
  Site,
} from "./types";

export const useConfig = () => useQuery({ queryKey: ["config"], queryFn: fetchConfig });
export const useReceiver = () => useQuery({ queryKey: ["receiver"], queryFn: () => get<ReceiverInfo>(route(ROUTES.receiver)), refetchInterval: 10_000 });
export const useBaseMode = (enabled = true) => useQuery({ queryKey: ["base", "mode"], queryFn: () => get<BaseModeView>(route(ROUTES.baseMode)), refetchInterval: 5000, enabled });
export const useSites = () => useQuery({ queryKey: ["base", "sites"], queryFn: () => get<Site[]>(route(ROUTES.sites)) });
export const useNtrip = () => useQuery({ queryKey: ["ntrip"], queryFn: () => get<NtripInfo>(route(ROUTES.ntrip)), refetchInterval: 10_000 });
export const useNtripClients = (enabled = true) =>
  useQuery({ queryKey: ["ntrip", "clients"], queryFn: () => get<NtripClient[]>(route(ROUTES.ntripClients)), refetchInterval: 5000, enabled });
export const useNtripHistory = (limit = 50) =>
  useQuery({ queryKey: ["ntrip", "history", limit], queryFn: () => get<NtripHistoryRecord[]>(route(ROUTES.ntripHistory, {}, { limit })), refetchInterval: 30_000 });
export const useLogs = () => useQuery({ queryKey: ["logs"], queryFn: () => get<LogsResponse>(route(ROUTES.logs)), refetchInterval: 30_000 });
export const useAvailability = (from: string, to: string) =>
  useQuery({ queryKey: ["logs", "availability", from, to], queryFn: () => get<HourSlot[]>(route(ROUTES.logsAvailability, {}, { from, to })), enabled: Boolean(from && to) });
export const useEvents = (level?: Level, limit = 200) =>
  useQuery({ queryKey: ["events", level ?? "all", limit], queryFn: () => get<EventItem[]>(route(ROUTES.events, {}, { limit, level })) });
export const useHistoryMetrics = () => useQuery({ queryKey: ["history", "metrics"], queryFn: () => get<HistoryMetrics>(route(ROUTES.historyMetrics)), staleTime: Infinity });
/** `keepPrevious`: a window that slides keeps showing the last answer while the next one loads. */
export const useHistory = (metrics: string[], from: string, to: string, res: "auto" | "1s" | "1m" = "auto", opts: { keepPrevious?: boolean } = {}) =>
  useQuery({
    queryKey: ["history", metrics.join(","), from, to, res],
    queryFn: () => get<HistoryResponse>(route(ROUTES.history, {}, { metrics: metrics.join(","), from, to, res })),
    enabled: metrics.length > 0 && Boolean(from && to),
    placeholderData: opts.keepPrevious ? keepPreviousData : undefined,
  });
export const useJobs = (kind?: string, limit = 50) =>
  useQuery({ queryKey: ["jobs", kind ?? "all", limit], queryFn: () => get<Job[]>(route(ROUTES.jobs, {}, { kind, limit })), refetchInterval: 5000 });
/** The fixed export presets; they never change while the daemon runs. */
export const usePresets = () => useQuery({ queryKey: ["export", "presets"], queryFn: () => get<Preset[]>(route(ROUTES.exportPresets)), staleTime: Infinity });
/** Asked for only once a job is done, after which its files never change (a delete drops the row). */
export const useJobFiles = (id: string | null) =>
  useQuery({ queryKey: ["jobs", "files", id], queryFn: () => get<JobFile[]>(route(ROUTES.jobFiles, { job_id: id! })), enabled: Boolean(id), staleTime: Infinity });

/** `GET /api/rover`: 409 on a base, so pass `enabled = false` there. */
export const useRover = (enabled = true) => useQuery({ queryKey: ["rover"], queryFn: () => get<RoverOverview>(route(ROUTES.rover)), refetchInterval: 5000, enabled });
/** Newest first. Polled like `useRover`: another browser or an API client can open or close one. */
export const useSessions = (enabled = true) => useQuery({ queryKey: ["rover", "sessions"], queryFn: () => get<Session[]>(route(ROUTES.roverSessions)), refetchInterval: 5000, enabled });
/** Newest first; one session's points when `sessionId` is given. */
export const usePoints = (sessionId?: number, enabled = true) =>
  useQuery({ queryKey: ["rover", "points", sessionId ?? "all"], queryFn: () => get<Point[]>(route(ROUTES.points, {}, { session_id: sessionId })), enabled });
/** Download URL for the points export (an `<a href download>`, not fetched as JSON). */
export const pointsExportUrl = (fmt: "csv" | "geojson" | "kml" | "gpx", sessionId?: number) => route(ROUTES.pointsExport, {}, { fmt, session_id: sessionId });

/** What this host's PPK can run, and the option file a job starts from. RTKLIB does not come and go while the page is open. */
export const usePpkDefaults = () => useQuery({ queryKey: ["ppk", "defaults"], queryFn: () => get<PpkDefaults>(route(ROUTES.ppkDefaults)), staleTime: 60_000 });

/**
 * Invalidate the queries whose truth just changed on the socket. Returns the unsubscribe.
 * Called once from `App`; pages need do nothing.
 */
export function bindLiveToQueries(qc: QueryClient): () => void {
  return useLive.subscribe((s, prev) => {
    // Not ["jobs","files",id]: a finished job's files never change, and every rendered done row
    // refetching its listing on each progress update of another job is a GET storm on a Pi.
    const deleted = s.lastDeletedJobId !== prev.lastDeletedJobId ? s.lastDeletedJobId : null;
    if (deleted != null) dropDeletedJob(qc, deleted);
    if (s.jobs !== prev.jobs || deleted != null) void qc.invalidateQueries({ queryKey: ["jobs"], predicate: (q) => q.queryKey[1] !== "files" });
    if (s.base !== prev.base) void qc.invalidateQueries({ queryKey: ["base"] });
    if (s.ntripClients !== prev.ntripClients) void qc.invalidateQueries({ queryKey: ["ntrip"] });
    if (s.events !== prev.events) void qc.invalidateQueries({ queryKey: ["events"] });
    if (s.receiverConnected !== prev.receiverConnected || s.receiverCapabilities !== prev.receiverCapabilities) {
      void qc.invalidateQueries({ queryKey: ["receiver"] });
      void qc.invalidateQueries({ queryKey: ["status"] });
    }
    if (s.rawlog !== prev.rawlog) void qc.invalidateQueries({ queryKey: ["logs"] });
    // A stored point lands in the list (and may have opened nothing new: sessions are explicit).
    if (s.lastSavedPointId !== prev.lastSavedPointId && s.lastSavedPointId != null) void qc.invalidateQueries({ queryKey: ["rover", "points"] });
  });
}

/**
 * Take a job the socket said was deleted out of every cached listing now, rather than one refetch
 * later, and forget its files. Each listing keeps the time it was read: `mergeJobs` compares a live
 * job against it to tell one newer than the listing from one deleted elsewhere.
 */
function dropDeletedJob(qc: QueryClient, id: string): void {
  qc.removeQueries({ queryKey: ["jobs", "files", id], exact: true });
  for (const q of qc.getQueryCache().findAll({ queryKey: ["jobs"] })) {
    const data: unknown = q.state.data;
    if (q.queryKey[1] === "files" || !Array.isArray(data)) continue;
    const kept = (data as Partial<Job>[]).filter((j) => j?.id !== id);
    if (kept.length !== data.length) qc.setQueryData(q.queryKey, kept, { updatedAt: q.state.dataUpdatedAt });
  }
}
