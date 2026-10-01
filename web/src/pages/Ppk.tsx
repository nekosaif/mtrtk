import { useId, useMemo, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { FeatureCollection } from "geojson";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { JobFileLink, JobsPanel } from "@/components/JobsPanel";
import { Panel } from "@/components/Panel";
import { QualityStrip } from "@/components/QualityStrip";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { TrackMap } from "@/components/TrackMap";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { ApiError, describeError, get, getText, jobFileUrl, submitPpk, uploadPpkFile } from "@/lib/api";
import { fmtBytes, fmtDuration, fmtUtcDate, fromInput, toInput } from "@/lib/format";
import { useLive } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { usePpkDefaults, useSessions, useSites } from "@/lib/queries";
import type { Job, PpkBaseBody, PpkResult, PpkRoverBody, PpkSubmit, PpkUpload, Session, Site } from "@/lib/types";
import { cn } from "@/lib/utils";

const HOUR_MS = 3600_000;
/** `POST /api/ppk`'s own cap on a window (`MAX_WINDOW`), mirrored so the form says so first. */
export const PPK_MAX_DAYS = 7;
/** How many events the result table lists; `events.csv` has every one. */
export const EVENT_ROWS = 200;
/** |ECEF| of any point a base can sit at (the daemon refuses the rest as latitude/longitude). */
const ECEF_RADIUS_M = [6.2e6, 6.5e6] as const;

type RoverKind = PpkRoverBody["kind"];
type BaseKind = PpkBaseBody["kind"];
type CoordMode = "auto" | "site" | "manual";

// ------------------------------------------------------------------------------- helpers

/** Why a window cannot be processed yet, or null when it can. */
export function windowProblem(from: string, to: string): string | null {
  const a = fromInput(from);
  const b = fromInput(to);
  if (a == null || b == null) return "Enter both times.";
  if (a >= b) return "From must be before to.";
  if (b - a > PPK_MAX_DAYS * 24 * HOUR_MS) return `At most ${PPK_MAX_DAYS} days per run.`;
  return null;
}

/** Three ECEF metres, or null when the text is not one. */
export function parseXyz(parts: [string, string, string]): [number, number, number] | null {
  if (parts.some((p) => p.trim() === "")) return null;
  const xyz = parts.map(Number) as [number, number, number];
  if (!xyz.every(Number.isFinite)) return null;
  const r = Math.hypot(...xyz);
  return r >= ECEF_RADIUS_M[0] && r <= ECEF_RADIUS_M[1] ? xyz : null;
}

/** RTKLIB Q of every epoch, in order, from `track.geojson`'s same-quality runs. */
export function qualitiesOf(track: FeatureCollection | null | undefined): number[] {
  const qs: number[] = [];
  for (const f of track?.features ?? []) {
    if (f.geometry?.type !== "LineString") continue;
    const q = Number(f.properties?.q);
    const n = Number(f.properties?.epochs);
    if (Number.isFinite(q) && Number.isInteger(n) && n > 0) for (let i = 0; i < n; i++) qs.push(q);
  }
  return qs;
}

/** `events.csv` as rows of named fields; at most `limit` rows. Its values never hold a comma. */
export function parseCsv(text: string, limit = EVENT_ROWS): Record<string, string>[] {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "");
  if (lines.length === 0) return [];
  const head = lines[0].split(",");
  return lines.slice(1, limit + 1).map((line) => {
    const cells = line.split(",");
    return Object.fromEntries(head.map((h, i) => [h, cells[i] ?? ""]));
  });
}

/** A done PPK job's result, read defensively: `result` is untyped JSON. */
function ppkResultOf(job: Job): PpkResult | null {
  const r = job.result as Partial<PpkResult> | null;
  if (!r || typeof r !== "object" || !r.summary || typeof r.summary.epochs !== "number") return null;
  return {
    summary: r.summary,
    events: r.events ?? { total: 0, ok: 0, gap_too_large: 0, no_neighbours: 0 },
    warnings: Array.isArray(r.warnings) ? r.warnings.filter((w): w is string => typeof w === "string") : [],
    inputs: r.inputs && typeof r.inputs === "object" ? r.inputs : {},
    files: Array.isArray(r.files) ? r.files : [],
  };
}

const fixedLevel = (pct: number): StatusLevel => (pct >= 95 ? "good" : pct >= 50 ? "warning" : "critical");
const pct = (v: number) => `${v.toFixed(1)} %`;
const mm = (m: number) => `${(m * 1000).toFixed(1)} mm`;
/** A solution time: RTKLIB writes GPST, which the daemon carries as if it were UTC. */
const fmtGpst = (iso: string | null) => fmtUtcDate(iso).replace(/ UTC$/, " GPST");

/** The server's refusal as sent: a 422's issues one per line, field and message; anything else verbatim. */
function ErrorDetail({ error }: { error: unknown }) {
  const issues = error instanceof ApiError ? error.issues : [];
  return (
    <Alert variant="destructive">
      <AlertDescription className="text-[14px] leading-5">
        {issues.length > 0 ? (
          <ul className="flex flex-col gap-1">
            {issues.map((i, n) => {
              const field = i.loc.filter((part, k) => !(k === 0 && (part === "body" || part === "query" || part === "path"))).join(".");
              return (
                <li key={n}>
                  {field ? <span className="num font-medium">{field}: </span> : null}
                  {i.msg}
                </li>
              );
            })}
          </ul>
        ) : (
          describeError(error)
        )}
      </AlertDescription>
    </Alert>
  );
}

/** A row of mutually exclusive options (a radio group drawn as buttons): thumb-sized at phone width. */
function Choice<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: { value: T; label: string; disabled?: boolean }[]; onChange: (v: T) => void }) {
  return (
    <div role="radiogroup" aria-label={label} className="flex flex-wrap gap-1">
      {options.map((o) => (
        <Button
          key={o.value}
          type="button"
          size="sm"
          role="radio"
          aria-checked={value === o.value}
          variant={value === o.value ? "default" : "outline"}
          disabled={o.disabled}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}

function Field({ label, htmlFor, children, hint }: { label: string; htmlFor?: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {hint ? <p className="text-[12px] leading-4 text-ink-2">{hint}</p> : null}
    </div>
  );
}

/**
 * Pick a file and it is uploaded at once (`POST /api/ppk/upload`), so a file that is neither
 * UBX nor RINEX is refused before the run is asked for. Shows what the daemon kept.
 */
function UploadField({ kind, label, value, onChange, hint }: { kind: "rover" | "base"; label: string; value: PpkUpload | null; onChange: (u: PpkUpload | null) => void; hint?: string }) {
  const id = useId();
  const [picked, setPicked] = useState<File | null>(null);
  const upload = useMutation({
    mutationFn: (file: File) => uploadPpkFile(kind, file),
    onSuccess: (u) => onChange(u),
  });
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <Input
        id={id}
        type="file"
        disabled={upload.isPending}
        onChange={(e) => {
          const file = e.target.files?.[0] ?? null;
          onChange(null);
          upload.reset();
          setPicked(file);
          if (file) upload.mutate(file);
        }}
      />
      {upload.isPending && picked ? (
        <p role="status" className="text-[12px] leading-4 text-ink-2">
          Uploading {picked.name} ({fmtBytes(picked.size)})…
        </p>
      ) : null}
      {value ? (
        <p className="num text-[12px] leading-4 text-ink-2">
          <span className="text-ink">{value.name}</span> · {fmtBytes(value.bytes)} · {value.detected === "ubx" ? "UBX" : `RINEX ${value.rinex === "nav" ? "navigation" : "observations"}`}
        </p>
      ) : null}
      {upload.isError ? <ErrorDetail error={upload.error} /> : null}
    </Field>
  );
}

// ---------------------------------------------------------------------------------- form

function sessionLabel(s: Session): string {
  return `${s.name ?? `#${s.id}`} · ${fmtUtcDate(s.start_utc)}${s.end_utc ? "" : " (open)"}`;
}

/**
 * The run's inputs: where the rover data is (a session or a window of this host's raw logs, or
 * an uploaded file), where the base data is (a remote mtrtk base over Tailscale, an uploaded
 * file, or this host's own logs), where the base sits, and the options. Every refusal from the
 * daemon is shown as it was sent.
 */
function PpkForm({ onQueued }: { onQueued: (job: Job) => void }) {
  const ids = useId();
  const qc = useQueryClient();
  const role = useLive((s) => s.role);
  const isRover = role === "rover";
  const defaults = usePpkDefaults();
  const sessions = useSessions(isRover);
  const sites = useSites();
  const sessionList = useMemo(() => (Array.isArray(sessions.data) ? sessions.data : []), [sessions.data]);
  const siteList = useMemo<Site[]>(() => (Array.isArray(sites.data) ? sites.data : []), [sites.data]);
  const activeSite = siteList.find((s) => s.active) ?? null;

  // Defaults follow the role until the operator picks: a rover pulls its base over the network,
  // a base processes against its own logs.
  const [roverPick, setRoverKind] = useState<RoverKind | null>(null);
  const roverKind: RoverKind = roverPick ?? (isRover ? "session" : "window");
  const [basePick, setBaseKind] = useState<BaseKind | null>(null);
  const baseKind: BaseKind = basePick ?? (isRover ? "remote" : "local");

  const [sessionPick, setSessionId] = useState<number | null>(null);
  const sessionId = sessionPick ?? sessionList[0]?.id ?? null;
  const [hourNow] = useState(() => Math.floor(Date.now() / HOUR_MS) * HOUR_MS);
  const [from, setFrom] = useState(() => toInput(hourNow - HOUR_MS));
  const [to, setTo] = useState(() => toInput(hourNow));
  const [roverUpload, setRoverUpload] = useState<PpkUpload | null>(null);

  const [urlTyped, setUrl] = useState<string | null>(null);
  const url = urlTyped ?? defaults.data?.ntrip_base_url ?? "";
  const [password, setPassword] = useState("");
  const [baseUpload, setBaseUpload] = useState<PpkUpload | null>(null);
  const [navUpload, setNavUpload] = useState<PpkUpload | null>(null);

  const [coords, setCoords] = useState<CoordMode>("auto");
  const [sitePick, setSite] = useState<string | null>(null);
  const siteName = sitePick ?? activeSite?.name ?? siteList[0]?.name ?? "";
  const [xyzText, setXyz] = useState<[string, string, string]>(["", "", ""]);

  const [events, setEvents] = useState(true);
  const [qzss, setQzss] = useState(false);
  const defaultElmask = defaults.data?.conf["pos1-elmask"] ?? "15";
  const [elmaskTyped, setElmask] = useState<string | null>(null);
  const elmask = elmaskTyped ?? defaultElmask;

  const autoLabel = baseKind === "remote" ? "Remote base's site" : baseKind === "local" ? "This host's site" : "RINEX header";
  const xyz = parseXyz(xyzText);
  const elmaskN = Number(elmask);
  const elmaskOk = elmask.trim() !== "" && Number.isFinite(elmaskN) && elmaskN >= 0 && elmaskN < 90;

  const problem = ((): string | null => {
    if (roverKind === "session" && sessionId == null) return isRover ? "No rover session yet: start one on the Survey page, or pick a window." : "Sessions are a rover's; pick a window or upload the rover file.";
    if (roverKind === "window") {
      const p = windowProblem(from, to);
      if (p) return p;
    }
    if (roverKind === "upload" && !roverUpload) return "Upload the rover file (raw UBX or RINEX observations).";
    if (baseKind === "remote" && !/^https?:\/\/\S+$/.test(url.trim())) return "Enter the base's web address, e.g. http://100.100.50.10:8080.";
    if (baseKind === "upload" && !baseUpload) return "Upload the base file (raw UBX or RINEX observations).";
    if (coords === "site" && !siteName) return "This host has no sites: choose another way to give the base position.";
    if (coords === "manual" && !xyz) return "Enter the base's ECEF X, Y, Z in metres (not latitude/longitude).";
    if (coords === "auto" && baseKind === "local" && !activeSite) return "This host has no active site: pick a site or enter the base's ECEF XYZ.";
    if (coords === "auto" && baseKind === "upload" && baseUpload?.detected === "ubx") return "An uploaded UBX base carries no surveyed position: pick a site or enter ECEF XYZ.";
    if (!elmaskOk) return "Elevation mask is degrees, 0 to 89.";
    return null;
  })();

  const submit = useMutation({
    mutationFn: () => {
      const rover: PpkRoverBody =
        roverKind === "session"
          ? { kind: "session", session_id: sessionId }
          : roverKind === "window"
            ? { kind: "window", start: `${from}:00Z`, end: `${to}:00Z` }
            : { kind: "upload", upload_id: roverUpload!.upload_id };
      const base: PpkBaseBody =
        baseKind === "remote"
          ? { kind: "remote", url: url.trim(), ...(password ? { password } : {}) }
          : baseKind === "upload"
            ? { kind: "upload", upload_id: baseUpload!.upload_id, ...(navUpload ? { nav_upload_id: navUpload.upload_id } : {}) }
            : { kind: "local" };
      const body: PpkSubmit = { rover, base, events, include_qzss: qzss };
      if (coords === "site") body.base_site = siteName;
      if (coords === "manual") body.base_xyz = xyz;
      if (elmask.trim() !== defaultElmask) body.conf_overrides = { "pos1-elmask": elmask.trim() };
      return submitPpk(body);
    },
    onSuccess: (job) => {
      toast.success("PPK queued", { description: `Job ${job.id}` });
      void qc.invalidateQueries({ queryKey: ["jobs"] });
      onQueued(job);
    },
  });

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!problem) submit.mutate();
      }}
    >
      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="mb-1 font-medium">Rover</legend>
        <Choice
          label="Rover source"
          value={roverKind}
          onChange={setRoverKind}
          options={[
            { value: "session", label: "Session", disabled: !isRover },
            { value: "window", label: "Window" },
            { value: "upload", label: "Upload" },
          ]}
        />
        {roverKind === "session" ? (
          <Field label="Session" htmlFor={`${ids}-session`} hint="Its time range over this host's raw logs.">
            <select id={`${ids}-session`} value={sessionId ?? ""} onChange={(e) => setSessionId(Number(e.target.value))} className="h-9 min-w-0 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink">
              {sessionList.length === 0 ? <option value="">No sessions</option> : null}
              {sessionList.map((s) => (
                <option key={s.id} value={s.id}>
                  {sessionLabel(s)}
                </option>
              ))}
            </select>
          </Field>
        ) : roverKind === "window" ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="From (UTC)" htmlFor={`${ids}-from`}>
              <Input id={`${ids}-from`} type="datetime-local" className="num" value={from} onChange={(e) => setFrom(e.target.value)} />
            </Field>
            <Field label="To (UTC)" htmlFor={`${ids}-to`}>
              <Input id={`${ids}-to`} type="datetime-local" className="num" value={to} onChange={(e) => setTo(e.target.value)} />
            </Field>
          </div>
        ) : (
          <UploadField kind="rover" label="Rover file" value={roverUpload} onChange={setRoverUpload} hint={`Raw UBX (F9P, or an INS's GNSS port) or RINEX observations; up to ${fmtBytes(defaults.data?.max_upload_bytes ?? 2 * 1024 ** 3)}.`} />
        )}
      </fieldset>

      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="mb-1 font-medium">Base</legend>
        <Choice
          label="Base source"
          value={baseKind}
          onChange={setBaseKind}
          options={[
            { value: "remote", label: "Remote base" },
            { value: "upload", label: "Upload" },
            { value: "local", label: "Local logs" },
          ]}
        />
        {baseKind === "remote" ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="Base web address" htmlFor={`${ids}-url`} hint={defaults.data?.ntrip_base_url ? "Guessed from this rover's caster address." : "The base's mtrtk web UI; its raw logs are fetched from there."}>
              <Input id={`${ids}-url`} inputMode="url" autoComplete="off" spellCheck={false} className="num" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://100.100.50.10:8080" />
            </Field>
            <Field label="Base web password" htmlFor={`${ids}-pw`} hint="Only if the base has one; used for this run and not stored.">
              <Input id={`${ids}-pw`} type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
          </div>
        ) : baseKind === "upload" ? (
          <div className="grid gap-2 sm:grid-cols-2">
            <UploadField kind="base" label="Base file" value={baseUpload} onChange={setBaseUpload} hint="Raw UBX or RINEX observations." />
            {baseUpload?.detected === "rinex" ? <UploadField kind="base" label="Navigation file (optional)" value={navUpload} onChange={setNavUpload} hint="RINEX navigation, when the rover brought none." /> : null}
          </div>
        ) : (
          <p className="text-ink-2">This host's own raw logs, from an hour before the window (for the ephemerides).</p>
        )}
      </fieldset>

      <fieldset className="flex min-w-0 flex-col gap-2">
        <legend className="mb-1 font-medium">Base position</legend>
        <Choice
          label="Base position"
          value={coords}
          onChange={setCoords}
          options={[
            { value: "auto", label: autoLabel },
            { value: "site", label: "Site" },
            { value: "manual", label: "Manual XYZ" },
          ]}
        />
        {coords === "auto" ? (
          <p className="text-[12px] leading-4 text-ink-2">
            {baseKind === "remote"
              ? "The remote base's active site."
              : baseKind === "local"
                ? activeSite
                  ? `This host's active site, ${activeSite.name}.`
                  : "This host has no active site."
                : "APPROX POSITION XYZ of the base RINEX: often only an approximation."}
          </p>
        ) : coords === "site" ? (
          <Field label="Site" htmlFor={`${ids}-site`}>
            <select id={`${ids}-site`} value={siteName} onChange={(e) => setSite(e.target.value)} className="h-9 min-w-0 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink">
              {siteList.length === 0 ? <option value="">No sites</option> : null}
              {siteList.map((s) => (
                <option key={s.name} value={s.name}>
                  {s.name}
                  {s.active ? " (active)" : ""}
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {(["X", "Y", "Z"] as const).map((axis, i) => (
              <Field key={axis} label={`${axis} (m)`} htmlFor={`${ids}-${axis}`}>
                <Input
                  id={`${ids}-${axis}`}
                  inputMode="decimal"
                  className="num"
                  value={xyzText[i]}
                  onChange={(e) => setXyz((prev) => prev.map((v, k) => (k === i ? e.target.value : v)) as [string, string, string])}
                />
              </Field>
            ))}
          </div>
        )}
      </fieldset>

      <fieldset className="grid min-w-0 items-end gap-3 sm:grid-cols-3">
        <legend className="mb-1 font-medium">Options</legend>
        <div className="flex h-9 items-center gap-2">
          <Switch id={`${ids}-events`} checked={events} onCheckedChange={setEvents} />
          <Label htmlFor={`${ids}-events`}>Camera events</Label>
        </div>
        <div className="flex h-9 items-center gap-2">
          <Switch id={`${ids}-qzss`} checked={qzss} onCheckedChange={setQzss} />
          <Label htmlFor={`${ids}-qzss`}>QZSS</Label>
        </div>
        <Field label="Elevation mask (°)" htmlFor={`${ids}-elmask`}>
          <Input id={`${ids}-elmask`} inputMode="decimal" className="num" value={elmask} onChange={(e) => setElmask(e.target.value)} aria-invalid={!elmaskOk} />
        </Field>
      </fieldset>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 flex-1 text-[12px] leading-4 text-ink-2">{problem ?? "Times are UTC; the solution is in GPST."}</p>
        <Button type="submit" disabled={submit.isPending || problem != null}>
          {submit.isPending ? "Starting…" : "Run PPK"}
        </Button>
      </div>
      {submit.isError ? <ErrorDetail error={submit.error} /> : null}
    </form>
  );
}

// -------------------------------------------------------------------------------- result

function EventsTable({ rows, total }: { rows: Record<string, string>[]; total: number }) {
  return (
    <div className="overflow-x-auto">
      <table aria-label="Camera events" className="w-full text-[14px]">
        <thead>
          <tr className="border-b border-line text-left text-ink-2">
            <th className="py-1.5 pr-3 font-medium">#</th>
            <th className="py-1.5 pr-3 font-medium">Time (UTC)</th>
            <th className="py-1.5 pr-3 font-medium">Latitude</th>
            <th className="py-1.5 pr-3 font-medium">Longitude</th>
            <th className="py-1.5 pr-3 text-right font-medium">Height (m)</th>
            <th className="py-1.5 pr-3 font-medium">Q</th>
            <th className="py-1.5 font-medium">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.n} className="border-b border-line/60 last:border-0">
              <td className="num py-1.5 pr-3">{r.count}</td>
              <td className="num py-1.5 pr-3 whitespace-nowrap">{r.time_utc}</td>
              <td className="num py-1.5 pr-3">{r.lat}</td>
              <td className="num py-1.5 pr-3">{r.lon}</td>
              <td className="num py-1.5 pr-3 text-right">{r.height_m}</td>
              <td className="num py-1.5 pr-3">{r.q}</td>
              <td className={cn("py-1.5", r.status === "ok" ? "text-ink" : "text-status-warning-text")}>{r.status.replace(/_/g, " ")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {total > rows.length ? (
        <p className="mt-2 text-[12px] leading-4 text-ink-2">
          The first {rows.length} of {total}; events.csv has every one.
        </p>
      ) : null}
    </div>
  );
}

/** One done PPK job: the track on a map, its quality strip, the statistics, warnings, events and downloads. */
function PpkResultView({ job }: { job: Job }) {
  const result = ppkResultOf(job);
  const names = new Set((result?.files ?? []).map((f) => f.name));
  const track = useQuery({
    queryKey: ["ppk", "track", job.id],
    queryFn: () => get<FeatureCollection>(jobFileUrl(job.id, "track.geojson")),
    enabled: names.has("track.geojson"),
    staleTime: Infinity,
  });
  const eventsGeo = useQuery({
    queryKey: ["ppk", "events-geo", job.id],
    queryFn: () => get<FeatureCollection>(jobFileUrl(job.id, "events.geojson")),
    enabled: names.has("events.geojson"),
    staleTime: Infinity,
  });
  const eventsCsv = useQuery({
    queryKey: ["ppk", "events-csv", job.id],
    queryFn: async () => parseCsv(await getText(jobFileUrl(job.id, "events.csv"))),
    enabled: names.has("events.csv"),
    staleTime: Infinity,
  });
  const qs = useMemo(() => qualitiesOf(track.data), [track.data]);

  if (!result) return <EmptyState title="This job has no PPK result to show" body="Its files, if any, are listed with the job." />;
  const s = result.summary;
  const ev = result.events;
  const source = typeof result.inputs.base_xyz_source === "string" ? result.inputs.base_xyz_source : null;
  const soltype = typeof result.inputs.soltype === "string" ? result.inputs.soltype : null;
  return (
    <div className="grid grid-cols-12 gap-4">
      <div className="col-span-12 flex min-w-0 flex-col gap-3 lg:col-span-8">
        {track.isError ? (
          <ErrorDetail error={track.error} />
        ) : track.data ? (
          <>
            <TrackMap key={job.id} track={track.data} events={eventsGeo.data ?? null} />
            <QualityStrip qs={qs} />
          </>
        ) : (
          <p className="text-ink-2">{names.has("track.geojson") ? "Loading the track…" : "This run wrote no track."}</p>
        )}
      </div>
      <div className="col-span-12 flex min-w-0 flex-col gap-3 lg:col-span-4">
        <div>
          <Stat label="Epochs" value={String(s.epochs)} hint={`every ${s.interval_s} s`} />
          <Stat label="Duration" value={fmtDuration(s.duration_s)} />
          <Stat label="Fixed" value={pct(s.fixed_pct)} level={fixedLevel(s.fixed_pct)} />
          <Stat label="Float" value={pct(s.float_pct)} />
          <Stat label="Single" value={pct(s.single_pct)} />
          <Stat label="Mean σ fixed (N/E/U)" value={s.mean_sd_fixed ? `${mm(s.mean_sd_fixed.n)} / ${mm(s.mean_sd_fixed.e)} / ${mm(s.mean_sd_fixed.u)}` : "—"} />
          <Stat label="First epoch" value={fmtGpst(s.first_time)} />
          <Stat label="Last epoch" value={fmtGpst(s.last_time)} />
          <Stat label="Gaps" value={String(s.gaps.length)} level={s.gaps.length ? "warning" : undefined} />
          {source ? <Stat label="Base position" value={source} /> : null}
          {soltype ? <Stat label="Solution" value={soltype} /> : null}
          {ev.total ? <Stat label="Camera events placed" value={`${ev.ok} of ${ev.total}`} level={ev.ok === ev.total ? "good" : "warning"} /> : null}
        </div>
        {result.warnings.length > 0 ? (
          <ul aria-label="Warnings" className="flex list-disc flex-col gap-1 pl-4">
            {result.warnings.map((w) => (
              <li key={w} className="text-[12px] leading-4 text-status-warning-text">
                {w}
              </li>
            ))}
          </ul>
        ) : null}
        <div>
          <h3 className="mb-1 font-medium">Downloads</h3>
          <ul className="flex flex-col gap-1 text-[14px]">
            {result.files.map((f) => (
              <li key={f.name} className="flex min-w-0 items-baseline justify-between gap-2">
                <JobFileLink url={jobFileUrl(job.id, f.name)} name={f.name} label={`Download ${f.name}`} />
                <span className="num shrink-0 text-[12px] leading-4 text-ink-2">{fmtBytes(f.bytes)}</span>
              </li>
            ))}
            <li className="flex min-w-0 items-baseline justify-between gap-2">
              <JobFileLink url={jobFileUrl(job.id, "summary.json")} name="summary.json" label="Download summary.json" />
            </li>
          </ul>
        </div>
      </div>
      {names.has("events.csv") ? (
        <div className="col-span-12 min-w-0">
          <h3 className="mb-1 font-medium">Camera events</h3>
          {eventsCsv.isError ? <ErrorDetail error={eventsCsv.error} /> : eventsCsv.data ? <EventsTable rows={eventsCsv.data} total={ev.total} /> : <p className="text-ink-2">Loading events…</p>}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------------- page

/**
 * Post-processed kinematic: the form that queues a run, the PPK jobs, and the selected done
 * job's result. On a host without RTKLIB the form still shows, under a banner saying why a run
 * would fail.
 */
export default function Ppk() {
  const defaults = usePpkDefaults();
  const [selected, setSelected] = useState<Job | null>(null);
  const missing = defaults.data ? [!defaults.data.rnx2rtkp && "rnx2rtkp", !defaults.data.convbin && "convbin"].filter(Boolean) : [];
  return (
    <>
      <PageHeader title="PPK">
        {defaults.data ? (
          missing.length ? (
            <StatusBadge level="critical" label="RTKLIB missing" />
          ) : (
            <StatusBadge level={defaults.data.demo5 ? "good" : "warning"} label={defaults.data.demo5 ? "RTKLIB demo5" : "RTKLIB 2.4.3"} />
          )
        ) : null}
      </PageHeader>
      {missing.length ? (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription className="text-[14px] leading-5">RTKLIB not installed on this host ({missing.join(", ")} not found) — use the Docker image.</AlertDescription>
        </Alert>
      ) : null}
      {defaults.isError ? (
        <div className="mb-4">
          <ErrorDetail error={defaults.error} />
        </div>
      ) : null}
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-6" title="New PPK run">
          <PpkForm onQueued={() => setSelected(null)} />
        </Panel>
        <JobsPanel kind="ppk" title="PPK jobs" className="col-span-12 lg:col-span-6" onSelect={setSelected} selectedId={selected?.id ?? null} />
        {selected ? (
          <Panel className="col-span-12" title={`Result · job ${selected.id}`} actions={<Button type="button" size="sm" variant="ghost" onClick={() => setSelected(null)}>Close</Button>}>
            <PpkResultView key={selected.id} job={selected} />
          </Panel>
        ) : null}
      </div>
    </>
  );
}
