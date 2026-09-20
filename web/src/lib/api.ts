/**
 * HTTP client for the daemon's API (`docs/api.md`).
 *
 * - `api<T>()` fetches JSON with the session cookie; a 401 routes to `/login` and is remembered
 *   (`auth`), so the live socket knows a password is configured.
 * - `ApiError` carries `status` and the server's `detail` untouched: a string for the hand-written
 *   409/504 refusals, a `ValidationIssue[]` for a 422. `describeError()` turns either into a line.
 * - `ROUTES` is the one table of paths the client knows; `contract.test.ts` checks every entry
 *   against the daemon's OpenAPI schema so a moved route fails the test suite, not a page.
 */
import type {
  ConfigChange,
  ConfigResponse,
  ConfigValues,
  FreezeBody,
  LoginResponse,
  ModeBody,
  OkResponse,
  PollResponse,
  ReapplyResponse,
  ResetKind,
  ResetResponse,
  SiteBody,
  SiteResult,
  ValidationIssue,
  BaseModeView,
  LogFile,
  JobFile,
} from "./types";

export type ApiDetail = string | ValidationIssue[];

export class ApiError extends Error {
  readonly status: number;
  readonly detail: ApiDetail;
  constructor(status: number, detail: ApiDetail) {
    super(`${status}: ${detailText(detail)}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
  /** The 422 issues, or an empty list for any other error. */
  get issues(): ValidationIssue[] {
    return Array.isArray(this.detail) ? this.detail : [];
  }
}

function detailText(detail: ApiDetail): string {
  if (typeof detail === "string") return detail;
  return detail
    .map((issue) => {
      // Drop the "body"/"query" prefix: the field name is what the operator recognises.
      const loc = issue.loc.filter((part, i) => !(i === 0 && (part === "body" || part === "query" || part === "path")));
      return loc.length ? `${loc.join(".")}: ${issue.msg}` : issue.msg;
    })
    .join("; ");
}

/** One line for a toast or an inline message, whatever was thrown. */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) return detailText(err.detail);
  if (err instanceof Error) return err.message;
  return String(err);
}

// --------------------------------------------------------------------- auth

/**
 * What the app knows about the password: a 401 has been seen, or `GET /api/config` returned
 * `web_password` masked. Both mean the daemon has one, which is what the live socket needs to
 * tell an HTTP-403 handshake from a daemon that is merely down.
 */
let unauthorizedSeen = false;
let configSaysPassword: boolean | null = null;

export const auth = {
  passwordConfigured(): boolean {
    return unauthorizedSeen || configSaysPassword === true;
  },
  unauthorizedSeen(): boolean {
    return unauthorizedSeen;
  },
  /** Called with `GET /api/config`'s `values`. */
  noteConfigValues(values: Partial<Pick<ConfigValues, "web_password">>): void {
    configSaysPassword = typeof values.web_password === "string" && values.web_password.length > 0;
  },
  reset(): void {
    unauthorizedSeen = false;
    configSaysPassword = null;
  },
};

export function loginPath(): string {
  const here = window.location.pathname + window.location.search;
  return here === "/" || here === "" ? "/login" : `/login?next=${encodeURIComponent(here)}`;
}

/** Route to the login page, remembering why. Never loops while already there. */
export function goToLogin(): void {
  unauthorizedSeen = true;
  if (window.location.pathname.startsWith("/login")) return;
  window.location.assign(loginPath());
}

// ------------------------------------------------------------------- fetch

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { credentials: "same-origin", ...init, headers });
  if (response.status === 401) {
    goToLogin();
    throw new ApiError(401, "authentication required");
  }
  if (!response.ok) throw await errorOf(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** The server's refusal as an `ApiError`: its `detail` when the body is JSON, else the status text. */
async function errorOf(response: Response): Promise<ApiError> {
  let detail: ApiDetail = response.statusText || `HTTP ${response.status}`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) detail = body.detail;
    else if (Array.isArray(body.detail)) detail = body.detail as ValidationIssue[];
    else if (body.detail != null) detail = JSON.stringify(body.detail);
  } catch {
    /* non-JSON error body: keep the status text */
  }
  return new ApiError(response.status, detail);
}

/**
 * Ask a download route whether it will answer before handing the URL to the browser: a 4xx
 * surfaces as an `ApiError` with the server's `detail` (the window cap, "no raw logs in that
 * window"), a 2xx resolves and its body is cancelled unread — the `<a download>` that follows
 * streams the real thing to disk without ever holding it in memory.
 */
export async function probeDownload(url: string): Promise<void> {
  const ctrl = new AbortController();
  const response = await fetch(url, { credentials: "same-origin", signal: ctrl.signal });
  if (response.status === 401) {
    goToLogin();
    throw new ApiError(401, "authentication required");
  }
  if (!response.ok) throw await errorOf(response);
  ctrl.abort();
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });

/** The one WebSocket URL. No `?topics=`: the single shared socket wants everything. */
export function wsUrl(token?: string | null): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${proto}://${window.location.host}/ws`);
  if (token) url.searchParams.set("token", token);
  return url.toString();
}

// ------------------------------------------------------------------ routes

export type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export interface RouteSpec {
  method: Method;
  /** OpenAPI path template, `{param}` for path parameters. */
  path: string;
  /** Name of the request body schema in the OpenAPI components, when the route takes one. */
  body?: string;
}

export const ROUTES = {
  health: { method: "GET", path: "/healthz" },
  login: { method: "POST", path: "/api/login", body: "LoginBody" },
  logout: { method: "POST", path: "/api/logout" },
  status: { method: "GET", path: "/api/status" },
  state: { method: "GET", path: "/api/state" },
  system: { method: "GET", path: "/api/system" },
  config: { method: "GET", path: "/api/config" },
  putConfig: { method: "PUT", path: "/api/config", body: "ConfigBody" },
  restart: { method: "POST", path: "/api/restart" },
  receiver: { method: "GET", path: "/api/receiver" },
  receiverReapply: { method: "POST", path: "/api/receiver/reapply" },
  receiverReset: { method: "POST", path: "/api/receiver/reset", body: "ResetBody" },
  receiverPoll: { method: "POST", path: "/api/receiver/poll", body: "PollBody" },
  baseMode: { method: "GET", path: "/api/base/mode" },
  putBaseMode: { method: "PUT", path: "/api/base/mode", body: "ModeBody" },
  survey: { method: "GET", path: "/api/base/survey" },
  surveyRestart: { method: "POST", path: "/api/base/survey/restart" },
  surveyFreeze: { method: "POST", path: "/api/base/survey/freeze", body: "FreezeBody" },
  sites: { method: "GET", path: "/api/base/sites" },
  addSite: { method: "POST", path: "/api/base/sites", body: "SiteBody" },
  deleteSite: { method: "DELETE", path: "/api/base/sites/{name}" },
  activateSite: { method: "POST", path: "/api/base/sites/{name}/activate" },
  ntrip: { method: "GET", path: "/api/ntrip" },
  ntripClients: { method: "GET", path: "/api/ntrip/clients" },
  ntripHistory: { method: "GET", path: "/api/ntrip/history" },
  logs: { method: "GET", path: "/api/logs" },
  logsAvailability: { method: "GET", path: "/api/logs/availability" },
  logsWindow: { method: "GET", path: "/api/logs/window" },
  logFile: { method: "GET", path: "/api/logs/{name}" },
  setKeep: { method: "PATCH", path: "/api/logs/{name}", body: "KeepBody" },
  deleteLog: { method: "DELETE", path: "/api/logs/{name}" },
  history: { method: "GET", path: "/api/history" },
  historyMetrics: { method: "GET", path: "/api/history/metrics" },
  events: { method: "GET", path: "/api/events" },
  ackEvent: { method: "POST", path: "/api/events/{event_id}/ack" },
  jobs: { method: "GET", path: "/api/jobs" },
  job: { method: "GET", path: "/api/jobs/{job_id}" },
  deleteJob: { method: "DELETE", path: "/api/jobs/{job_id}" },
  jobFiles: { method: "GET", path: "/api/jobs/{job_id}/files" },
  jobFile: { method: "GET", path: "/api/jobs/{job_id}/files/{name}" },
} as const satisfies Record<string, RouteSpec>;

export type QueryValue = string | number | boolean | null | undefined;

/** Fill a route's `{params}` (URL-encoded) and append the defined query values. */
export function route(spec: RouteSpec, params: Record<string, string | number> = {}, query: Record<string, QueryValue> = {}): string {
  const path = spec.path.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = params[name];
    if (value === undefined || value === null || value === "") throw new Error(`route ${spec.path}: missing path parameter "${name}"`);
    return encodeURIComponent(String(value));
  });
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) if (value !== undefined && value !== null) search.set(key, String(value));
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

// ------------------------------------------------------ typed commands

export const login = (password: string) => post<LoginResponse>(route(ROUTES.login), { password });
export const logout = () => post<OkResponse>(route(ROUTES.logout));
/** Send only the keys the operator changed (R1); the answer says what moved and whether a restart is needed. */
export const putConfig = (values: Partial<ConfigValues>) => put<ConfigChange>(route(ROUTES.putConfig), { values });
export const restartDaemon = () => post<OkResponse>(route(ROUTES.restart));
export const fetchConfig = async (): Promise<ConfigResponse> => {
  const cfg = await get<ConfigResponse>(route(ROUTES.config));
  auth.noteConfigValues(cfg.values);
  return cfg;
};

export const receiverReapply = () => post<ReapplyResponse>(route(ROUTES.receiverReapply));
export const receiverReset = (kind: ResetKind) => post<ResetResponse>(route(ROUTES.receiverReset), { kind });
export const receiverPoll = (msg_class: string, msg_id: string) => post<PollResponse>(route(ROUTES.receiverPoll), { msg_class, msg_id });

export const putBaseMode = (body: ModeBody) => put<BaseModeView>(route(ROUTES.putBaseMode), body);
export const restartSurvey = () => post<BaseModeView>(route(ROUTES.surveyRestart));
export const freezeSurvey = (body: FreezeBody) => post<SiteResult>(route(ROUTES.surveyFreeze), body);
export const addSite = (body: SiteBody) => post<SiteResult>(route(ROUTES.addSite), body);
export const activateSite = (name: string) => post<SiteResult>(route(ROUTES.activateSite, { name }));
export const deleteSite = (name: string) => del<OkResponse>(route(ROUTES.deleteSite, { name }));

export const setKeep = (name: string, keep: boolean) => patch<LogFile>(route(ROUTES.setKeep, { name }), { keep });
export const deleteLog = (name: string, force = false) => del<OkResponse>(route(ROUTES.deleteLog, { name }, { force: force ? 1 : undefined }));
/** Download URLs (not fetched as JSON): hand them to an `<a href download>`. */
export const logFileUrl = (name: string) => route(ROUTES.logFile, { name });
export const logWindowUrl = (from: string, to: string) => route(ROUTES.logsWindow, {}, { from, to });

export const ackEvent = (id: number) => post<OkResponse>(route(ROUTES.ackEvent, { event_id: id }));

export const deleteJob = (id: string) => del<OkResponse>(route(ROUTES.deleteJob, { job_id: id }));
export const fetchJobFiles = (id: string) => get<JobFile[]>(route(ROUTES.jobFiles, { job_id: id }));
export const jobFileUrl = (id: string, name: string) => route(ROUTES.jobFile, { job_id: id, name });
