import { useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { EmptyState } from "@/components/EmptyState";
import { MapPanel } from "@/components/MapPanel";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Switch } from "@/components/ui/switch";
import { ROUTES, del, describeError, patch, post, route } from "@/lib/api";
import { fmtAcc, fmtDms, fmtUtcDate } from "@/lib/format";
import { useLive } from "@/lib/live";
import { pointsExportUrl, usePoints, useRover, useSessions } from "@/lib/queries";
import type { CollectStatus, Point, Session } from "@/lib/types";

const FORMATS = ["csv", "geojson", "kml", "gpx"] as const;
/** The collector's own bounds (`POST /api/rover/collect` is a 422 outside them). */
const EPOCHS_MIN = 1;
const EPOCHS_MAX = 3600;

const fixName = (carr: number) => (carr === 2 ? "RTK fixed" : carr === 1 ? "RTK float" : "3D");
const mm = (m: number) => (m * 1000).toFixed(0);

function SessionPanel({ current, enabled }: { current: Session | null; enabled: boolean }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  // The list and the overview (whose `session` decides what this panel shows) both changed.
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["rover", "sessions"] });
    void qc.invalidateQueries({ queryKey: ["rover"], exact: true });
  };
  const start = useMutation({
    mutationFn: () => post<Session>(route(ROUTES.startRoverSession), { name: name.trim() || null }),
    onSuccess: () => {
      setName("");
      refresh();
    },
  });
  const stop = useMutation({ mutationFn: () => post<Session | null>(route(ROUTES.stopRoverSession)), onSuccess: refresh });
  const error = start.error ?? stop.error;
  return (
    <Panel className="col-span-12 lg:col-span-4" title="Session">
      {current ? (
        <>
          <p>
            {current.name ?? `#${current.id}`} <span className="text-ink-2">started {fmtUtcDate(current.start_utc)}</span>
          </p>
          <Button className="mt-2" variant="outline" disabled={!enabled || stop.isPending} onClick={() => stop.mutate()}>
            Stop session
          </Button>
        </>
      ) : (
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            start.mutate();
          }}
        >
          <div className="flex flex-1 flex-col gap-1">
            <Label htmlFor="session-name">Session name</Label>
            <Input id="session-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="site-2026-09-19" />
          </div>
          <Button type="submit" className="self-end" disabled={!enabled || start.isPending}>
            Start
          </Button>
        </form>
      )}
      {error ? (
        <p role="alert" className="mt-2 text-status-critical-text">
          {describeError(error)}
        </p>
      ) : null}
      <p className="mt-3 text-[12px] leading-4 text-ink-2">Sessions group points and mark the raw-log window used for PPK.</p>
    </Panel>
  );
}

function CollectProgress({ collect }: { collect: CollectStatus }) {
  return (
    <div className="mt-4 rounded-md border border-line p-3">
      <div className="flex items-center justify-between gap-3">
        <span>{collect.name}</span>
        <span className="num text-ink-2">
          {collect.accepted} of {collect.target} epochs
          {collect.skipped ? ` · ${collect.skipped} skipped` : ""}
        </span>
      </div>
      <Progress value={Math.round((collect.accepted / Math.max(1, collect.target)) * 100)} className="mt-2" aria-label={`Collecting ${collect.name ?? "point"}`} />
      <div className="mt-2 grid grid-cols-3 gap-2">
        <Stat label="σ north" value={fmtAcc(collect.sd_n)} />
        <Stat label="σ east" value={fmtAcc(collect.sd_e)} />
        <Stat label="σ up" value={fmtAcc(collect.sd_u)} />
      </div>
      {collect.state === "aborted" ? (
        <p className="mt-2 text-status-critical-text">Stopped: {collect.reason ?? "cancelled"}</p>
      ) : collect.state === "done" ? (
        <p className="mt-2 text-status-good-text">Saved{collect.point_id != null ? ` as point ${collect.point_id}` : ""}.</p>
      ) : null}
    </div>
  );
}

type CollectDefaults = { epochs: number; fixed_only: boolean };
const FALLBACK_DEFAULTS: CollectDefaults = { epochs: 30, fixed_only: true };

function CollectPanel({ collect, canCollect, defaults }: { collect: CollectStatus | null; canCollect: boolean; defaults?: CollectDefaults }) {
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [note, setNote] = useState("");
  // null until edited: the field shows (and posts) POINT_EPOCHS / POINT_FIXED_ONLY until then.
  const [epochsEdit, setEpochs] = useState<string | null>(null);
  const [fixedOnlyEdit, setFixedOnly] = useState<boolean | null>(null);
  const epochs = epochsEdit ?? String((defaults ?? FALLBACK_DEFAULTS).epochs);
  const fixedOnly = fixedOnlyEdit ?? (defaults ?? FALLBACK_DEFAULTS).fixed_only;
  const collecting = collect?.state === "collecting";
  const n = Number(epochs);
  const epochsOk = Number.isInteger(n) && n >= EPOCHS_MIN && n <= EPOCHS_MAX;
  // The server's answer is the collection's state now; show it rather than the last one until the
  // first `points.progress` lands (a stale "Saved" banner, and a form that would POST into a 409).
  const showAnswer = (c: CollectStatus | null | undefined) => {
    if (typeof c?.state === "string") useLive.setState({ collect: c });
  };
  const start = useMutation({
    mutationFn: () => post<CollectStatus>(route(ROUTES.startCollect), { name: name.trim(), code: code.trim() || null, note: note.trim() || null, epochs: n, fixed_only: fixedOnly }),
    onSuccess: (c) => {
      // A progress update may already have overtaken the answer; it is the newer of the two.
      if (useLive.getState().collect?.state !== "collecting") showAnswer(c);
      toast.success(`Collecting ${name.trim()}`);
    },
  });
  const cancel = useMutation({ mutationFn: () => del<CollectStatus>(route(ROUTES.cancelCollect)), onSuccess: showAnswer });
  const error = start.error ?? cancel.error;
  return (
    <Panel className="col-span-12 lg:col-span-8" title="Collect a point">
      <form
        className="grid grid-cols-2 gap-3 md:grid-cols-4"
        onSubmit={(e) => {
          e.preventDefault();
          start.mutate();
        }}
      >
        <div className="flex flex-col gap-1">
          <Label htmlFor="pt-name">Point name</Label>
          <Input id="pt-name" value={name} onChange={(e) => setName(e.target.value)} disabled={collecting} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="pt-code">Code</Label>
          <Input id="pt-code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="BM, FENCE…" disabled={collecting} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="pt-note">Note</Label>
          <Input id="pt-note" value={note} onChange={(e) => setNote(e.target.value)} disabled={collecting} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="pt-epochs">Epochs</Label>
          <Input id="pt-epochs" inputMode="numeric" value={epochs} onChange={(e) => setEpochs(e.target.value)} disabled={collecting} aria-invalid={!epochsOk} aria-describedby="pt-epochs-hint" />
          <span id="pt-epochs-hint" className={epochsOk ? "sr-only" : "text-[12px] leading-4 text-status-critical-text"}>
            A whole number from {EPOCHS_MIN} to {EPOCHS_MAX}.
          </span>
        </div>
        <label className="col-span-2 flex items-center gap-2 text-[14px]">
          <Switch checked={fixedOnly} onCheckedChange={setFixedOnly} aria-label="RTK fixed epochs only" disabled={collecting} />
          RTK fixed epochs only
        </label>
        <div className="col-span-2 flex justify-end gap-2">
          {collecting ? (
            <Button type="button" variant="outline" disabled={cancel.isPending} onClick={() => cancel.mutate()}>
              Cancel
            </Button>
          ) : null}
          <Button type="submit" disabled={collecting || !name.trim() || !epochsOk || !canCollect || start.isPending}>
            Collect point
          </Button>
        </div>
      </form>
      {error ? (
        <p role="alert" className="mt-2 text-status-critical-text">
          {describeError(error)}
        </p>
      ) : null}
      {collect && collect.state !== "idle" ? <CollectProgress collect={collect} /> : null}
    </Panel>
  );
}

function PointRow({ p }: { p: Point }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(p.name);
  const [code, setCode] = useState(p.code ?? "");
  const [note, setNote] = useState(p.note ?? "");
  const refresh = () => void qc.invalidateQueries({ queryKey: ["rover", "points"] });
  const save = useMutation({
    mutationFn: () => patch<Point>(route(ROUTES.patchPoint, { point_id: p.id }), { name: name.trim(), code: code.trim(), note: note.trim() }),
    onSuccess: () => {
      setEditing(false);
      refresh();
    },
  });
  const edit = () => {
    setName(p.name);
    setCode(p.code ?? "");
    setNote(p.note ?? "");
    save.reset();
    setEditing(true);
  };
  if (editing) {
    return (
      <tr className="border-b border-line/60 last:border-0">
        <td colSpan={7} className="py-2">
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              save.mutate();
            }}
          >
            <Input aria-label={`Name of point ${p.id}`} value={name} onChange={(e) => setName(e.target.value)} className="w-40" />
            <Input aria-label={`Code of point ${p.id}`} value={code} onChange={(e) => setCode(e.target.value)} className="w-28" placeholder="Code" />
            <Input aria-label={`Note of point ${p.id}`} value={note} onChange={(e) => setNote(e.target.value)} className="min-w-40 flex-1" placeholder="Note" />
            <Button type="submit" size="sm" disabled={!name.trim() || save.isPending}>
              Save
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            {save.isError ? (
              <p role="alert" className="w-full text-status-critical-text">
                {describeError(save.error)}
              </p>
            ) : null}
          </form>
        </td>
      </tr>
    );
  }
  return (
    <tr className="border-b border-line/60 last:border-0">
      <td className="py-1.5 pr-3" title={p.note ?? undefined}>
        {p.name}
      </td>
      <td className="py-1.5 pr-3 text-ink-2">{p.code ?? ""}</td>
      <td className="num py-1.5 pr-3 whitespace-nowrap">
        {fmtDms(p.lat, true)} {fmtDms(p.lon, false)} · {p.height_m.toFixed(3)} m
      </td>
      <td className="num py-1.5 pr-3 text-right whitespace-nowrap">
        {mm(p.sd_n)}/{mm(p.sd_e)}/{mm(p.sd_u)} mm
      </td>
      <td className="py-1.5 pr-3 whitespace-nowrap">
        {fixName(p.carr_soln)} · {p.n_epochs} ep
      </td>
      <td className="num py-1.5 pr-3 whitespace-nowrap">{fmtUtcDate(p.ts_utc)}</td>
      <td className="py-1.5 text-right whitespace-nowrap">
        <Button size="sm" variant="ghost" aria-label={`Edit ${p.name}`} onClick={edit}>
          Edit
        </Button>
        <ConfirmDialog
          trigger={
            <Button size="sm" variant="ghost" aria-label={`Delete ${p.name}`}>
              Delete
            </Button>
          }
          title={`Delete ${p.name}?`}
          body="The point is removed from the survey and from every later export."
          confirmLabel="Delete"
          destructive
          onConfirm={async () => {
            await del(route(ROUTES.deletePoint, { point_id: p.id }));
            refresh();
          }}
        />
      </td>
    </tr>
  );
}

/**
 * The rover's Survey page: the open session, collecting an averaged point (live progress and
 * σ from `points.progress`), the stored points with rename/delete, exports in four formats
 * (optionally one session's), and the points on the map.
 */
export default function Survey() {
  const role = useLive((s) => s.role);
  const state = useLive((s) => s.state);
  const collect = useLive((s) => s.collect);
  const receiverConnected = useLive((s) => s.receiverConnected);
  const isRover = role === "rover";
  const sessions = useSessions(isRover);
  const rover = useRover(isRover);
  const [sessionFilter, setSessionFilter] = useState<number | undefined>(undefined);
  const points = usePoints(sessionFilter, isRover);
  // A list endpoint that answered with anything but a list is treated as empty, never trusted.
  const sessionList = useMemo(() => (Array.isArray(sessions.data) ? sessions.data : []), [sessions.data]);
  const pointList = useMemo(() => (Array.isArray(points.data) ? points.data : []), [points.data]);
  // Memoised: a new array per epoch would rebuild every marker's position for nothing.
  const mapPoints = useMemo(() => pointList.map((p) => ({ id: p.id, lat: p.lat, lon: p.lon, label: p.name })), [pointList]);

  if (!isRover) {
    return (
      <>
        <PageHeader title="Survey" />
        {role == null ? (
          <EmptyState title="Waiting for the receiver" />
        ) : (
          <EmptyState title="This daemon runs as a base station" body="The Survey page collects points on a rover. Set ROLE=rover in Settings to use it." />
        )}
      </>
    );
  }
  // The overview's `session` is the daemon's own answer; the list is the fallback until it lands.
  const overviewSession = rover.isSuccess && rover.data && "session" in rover.data ? rover.data.session : undefined;
  const current = overviewSession !== undefined ? overviewSession : (sessionList.find((s) => s.end_utc === null) ?? null);
  const collectDefaults = rover.isSuccess ? rover.data?.collect_defaults : undefined;
  return (
    <>
      <PageHeader title="Survey">
        {current ? <StatusBadge level="good" label={`Session ${current.name ?? current.id} open`} /> : <StatusBadge level="warning" label="No session open" />}
      </PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <SessionPanel current={current} enabled={sessions.isSuccess} />
        <CollectPanel collect={collect} canCollect={state != null && receiverConnected !== false} defaults={collectDefaults} />
        <Panel
          className="col-span-12 lg:col-span-7"
          title={`Points (${pointList.length})`}
          bodyClassName="overflow-x-auto p-2"
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <select
                aria-label="Session filter"
                value={sessionFilter ?? ""}
                onChange={(e) => setSessionFilter(e.target.value ? Number(e.target.value) : undefined)}
                className="rounded-md border border-line bg-panel-2 px-2 py-1 text-[12px]"
              >
                <option value="">All sessions</option>
                {sessionList.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name ?? `#${s.id}`}
                  </option>
                ))}
              </select>
              {FORMATS.map((f) => (
                <Button key={f} size="sm" variant="outline" asChild>
                  <a href={pointsExportUrl(f, sessionFilter)} download>
                    {f.toUpperCase()}
                  </a>
                </Button>
              ))}
            </div>
          }
        >
          {points.isError ? (
            <p role="alert" className="p-2 text-status-critical-text">
              Points unavailable: {describeError(points.error)}
            </p>
          ) : (
            <table aria-label="Points" className="w-full text-[14px]">
              <thead>
                <tr className="border-b border-line text-left text-ink-2">
                  <th className="py-1.5 pr-3 font-medium">Name</th>
                  <th className="py-1.5 pr-3 font-medium">Code</th>
                  <th className="py-1.5 pr-3 font-medium">Position</th>
                  <th className="py-1.5 pr-3 text-right font-medium">σ N/E/U</th>
                  <th className="py-1.5 pr-3 font-medium">Fix</th>
                  <th className="py-1.5 pr-3 font-medium">Time</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {pointList.map((p) => (
                  <PointRow key={p.id} p={p} />
                ))}
                {points.isSuccess && pointList.length === 0 ? (
                  <tr>
                    <td colSpan={7} className="py-6 text-center text-ink-3">
                      No points yet.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel className="col-span-12 lg:col-span-5" title="Map" bodyClassName="p-0">
          <MapPanel
            lat={state?.position.lat ?? null}
            lon={state?.position.lon ?? null}
            hAcc={state?.accuracy.h_acc_m ?? null}
            points={mapPoints}
            height={360}
          />
        </Panel>
      </div>
    </>
  );
}
