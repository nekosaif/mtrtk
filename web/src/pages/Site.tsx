import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "@/app/PageHeader";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { MapPanel } from "@/components/MapPanel";
import { Panel } from "@/components/Panel";
import { SiteForm, type SiteInput } from "@/components/SiteForm";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { Gauge } from "@/components/charts/Gauge";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { activateSite, addSite, deleteSite, describeError, freezeSurvey, putBaseMode, restartSurvey } from "@/lib/api";
import { type CoordMode, DASH, fmtAcc, fmtDuration, fmtMeters, fmtPosition, fmtUtc, fmtUtcDate } from "@/lib/format";
import { ecefToLlh } from "@/lib/geo";
import { type BaseInfo, useLive, useStale } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useCoordMode } from "@/lib/prefs";
import { useAvailability, useBaseMode, useConfig, useSites } from "@/lib/queries";
import type { BaseMode, BaseModeView, ConfigResponse, ModeBody, Site as SiteT, SiteResult, SurveyIn } from "@/lib/types";
import { cn } from "@/lib/utils";
import { siteCheck } from "./Corrections";

// --------------------------------------------------------------------------------- constants

/** The settings' bounds for the survey-in parameters (`config.py`); the API refuses anything outside them. */
export const SVIN_DURATION = { min: 1, max: 86_400 } as const;
export const SVIN_ACC = { min: 0.01, max: 100 } as const;
/** The daemon calls a site verified when every ECEF axis of the 1005 agrees to this (`basemode.py`). */
export const SITE_TOLERANCE_M = 0.0005;

const PPP_SERVICES = [
  { name: "CSRS-PPP (NRCan)", url: "https://webapp.csrs-scrs.nrcan-rncan.gc.ca/geod/tools-outils/ppp.php", note: "free, worldwide, GPS + GLONASS; best with 24 h; result in ITRF2020 at the observation epoch" },
  { name: "AUSPOS (Geoscience Australia)", url: "https://gnss.ga.gov.au/auspos", note: "free, worldwide, GPS; 1 h minimum, 24 h recommended" },
  { name: "OPUS (NGS)", url: "https://geodesy.noaa.gov/OPUS/", note: "USA and territories only, GPS L1/L2; 15 min to 48 h" },
] as const;

const MODES: { value: BaseMode; label: string; about: string }[] = [
  { value: "survey-in", label: "Survey-in", about: "The receiver averages its own position until the time and accuracy gates are both met. About 1–2 m absolute." },
  { value: "fixed", label: "Fixed", about: "The receiver sits on a saved site and broadcasts its coordinates in RTCM 1005. Rovers inherit the site's absolute accuracy." },
  { value: "off", label: "Off", about: "No time mode: observations still flow, but without a base position there is no RTCM 1005 and rovers cannot fix." },
];

// ----------------------------------------------------------------------------------- helpers

const nowUtc = () => fmtUtc(new Date().toISOString());

/** A site's position in the operator's coordinate mode; ECEF mode shows the stored x/y/z, never a re-derivation. */
function sitePosition(s: Pick<SiteT, "lat" | "lon" | "height_m" | "x" | "y" | "z">, mode: CoordMode): [string, string] {
  return fmtPosition({ lat: s.lat, lon: s.lon, height_m: s.height_m, ecef_x_m: s.x, ecef_y_m: s.y, ecef_z_m: s.z, invalid_llh: false }, mode);
}

function TwoLines({ lines, className }: { lines: [string, string]; className?: string }) {
  if (lines[0] === DASH && lines[1] === DASH) return <span className={cn("num", className)}>{DASH}</span>;
  return (
    <span className={cn("num inline-flex flex-col leading-5", className)}>
      <span>{lines[0]}</span>
      <span>{lines[1]}</span>
    </span>
  );
}

/** What the header says about the base right now. */
function headerBadge(view: BaseModeView | undefined, svin: SurveyIn): { level: StatusLevel; label: string } | null {
  if (!view) return null;
  if (!view.available) return { level: "warning", label: "No position-mode manager" };
  if (view.mode === "fixed") return { level: view.verified ? "good" : "warning", label: `Fixed${view.site ? ` · ${view.site}` : ""}` };
  if (view.mode === "survey-in") return svin.valid ? { level: "good", label: "Survey-in complete" } : svin.active ? { level: "warning", label: "Survey-in running" } : { level: "warning", label: "Survey-in not started" };
  return { level: "serious", label: "Position mode off" };
}

// ------------------------------------------------------------------------------- site result

/**
 * What the last activate or freeze did. `applied` is the daemon's word for "the receiver is on
 * it now" — a row can be active without that (no manager, or the receiver refused the fixed
 * position), which is a warning, never a success. `persist` is the follow-up
 * `PUT /api/base/mode {"mode":"fixed","site"}` that makes the setting outlive a restart.
 */
interface SiteOutcome {
  kind: "activate" | "freeze";
  name: string;
  applied: boolean;
  activated: boolean;
  at: string;
  persist: { ok: true } | { ok: false; detail: string } | null;
}

function SiteOutcomeLine({ outcome, view, live }: { outcome: SiteOutcome; view: BaseModeView | undefined; live: BaseInfo }) {
  const time = <span className="num">{outcome.at} UTC</span>;
  if (!outcome.activated) {
    return (
      <p role="status" aria-label="Site result" className="text-ink-2">
        Saved <span className="text-ink">{outcome.name}</span> from the survey-in at {time}. It is not active: activate it from the table when you want the base on it.
      </p>
    );
  }
  if (!outcome.applied) {
    const why = view && !view.available
      ? "no base-mode manager is running (replay or rover role), so the row waits for a live base to pick it up at its next start."
      : `the receiver did not take the fixed position${live.reason ? ` (${live.reason})` : ""}; the base stays where it was.`;
    return (
      <div role="status" aria-label="Site result" className="flex flex-col gap-1 text-ink-2">
        <StatusBadge level="warning" label={`${outcome.name} is active but not applied`} className="self-start" />
        <p>
          The row is the active site as of {time}, but the receiver is not on it: {why}
        </p>
      </div>
    );
  }
  return (
    <div role="status" aria-label="Site result" className="flex flex-col gap-1 text-ink-2">
      <StatusBadge level="good" label={`${outcome.name} is the active site`} className="self-start" />
      <p>
        The receiver is on it in fixed mode as of {time}
        {outcome.persist?.ok ? (
          <>
            {" "}· mode and site saved to .env, so they survive a restart.
          </>
        ) : outcome.persist ? (
          <>
            , but the mode was <span className="text-ink">not saved to .env</span> — after a restart the base goes back to what .env says: {outcome.persist.detail}
          </>
        ) : null}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------------- position mode panel

function inRange(v: number, lo: number, hi: number) {
  return Number.isFinite(v) && v >= lo && v <= hi;
}

function PositionModePanel({
  view,
  loading,
  sites,
  config,
  live,
}: {
  view: BaseModeView | undefined;
  loading: boolean;
  sites: SiteT[];
  config: ConfigResponse | undefined;
  live: BaseInfo;
}) {
  const qc = useQueryClient();
  const [picked, setPicked] = useState<BaseMode | null>(null);
  const [dur, setDur] = useState<string | null>(null);
  const [acc, setAcc] = useState<string | null>(null);
  const [siteName, setSiteName] = useState<string | null>(null);
  const [done, setDone] = useState<{ asked: BaseMode; view: BaseModeView; at: string } | null>(null);
  const apply = useMutation({
    mutationFn: (body: ModeBody) => putBaseMode(body),
    onSuccess: (v, body) => {
      setDone({ asked: body.mode, view: v, at: nowUtc() });
      void qc.invalidateQueries({ queryKey: ["base"] });
      void qc.invalidateQueries({ queryKey: ["config"] });
    },
  });

  const available = view?.available ?? false;
  const mode: BaseMode = picked ?? view?.mode ?? "survey-in";
  const active = sites.find((s) => s.active);
  const durStr = dur ?? String(view?.svin.min_duration_s ?? 300);
  const accStr = acc ?? String(view?.svin.acc_limit_m ?? 2);
  const durNum = Number(durStr);
  const accNum = Number(accStr);
  const durOk = durStr.trim() !== "" && Number.isInteger(durNum) && inRange(durNum, SVIN_DURATION.min, SVIN_DURATION.max);
  const accOk = accStr.trim() !== "" && inRange(accNum, SVIN_ACC.min, SVIN_ACC.max);
  const site = siteName ?? view?.site ?? active?.name ?? "";
  const siteOk = site !== "" && sites.some((s) => s.name === site);
  const canApply = available && !apply.isPending && (mode === "survey-in" ? durOk && accOk : mode === "fixed" ? siteOk : true);

  const body = (): ModeBody => {
    if (mode === "survey-in") return { mode, svin_min_duration_s: durNum, svin_acc_limit_m: accNum };
    if (mode === "fixed") return { mode, site };
    return { mode };
  };

  const pending = config?.pending;
  const pendingMode = pending?.base_mode;
  const pendingSite = pending && "active_site" in pending ? pending.active_site : undefined;
  const hasPending = pendingMode !== undefined || pendingSite !== undefined;

  return (
    <Panel className="col-span-12 lg:col-span-4" title="Position mode">
      <div className="flex flex-col gap-3">
        {loading && !view ? <p className="text-ink-2">Loading the base mode…</p> : null}
        {view && !available ? (
          <p className="text-ink-2">No position-mode manager on this source (replay or rover role): nothing here can be written to a receiver. Sites can still be saved below; a live base picks the active one up at its next start.</p>
        ) : null}
        <fieldset disabled={!available || apply.isPending} className="flex flex-col gap-3 disabled:opacity-60">
          <legend className="sr-only">Base position mode</legend>
          <div className="flex flex-col gap-2" role="radiogroup" aria-label="Mode">
            {MODES.map((m) => (
              <label key={m.value} className="flex items-start gap-2 text-[14px] leading-5">
                <input type="radio" name="base-mode" value={m.value} checked={mode === m.value} onChange={() => setPicked(m.value)} className="mt-1 accent-brass" />
                <span className="flex flex-col">
                  <span className={cn(mode === m.value ? "text-ink" : "text-ink-2")}>{m.label}</span>
                  <span className="text-[12px] leading-4 text-ink-2">{m.about}</span>
                </span>
              </label>
            ))}
          </div>
          {mode === "survey-in" ? (
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <Label htmlFor="svin-min" className="text-[12px] leading-4 text-ink-2">
                  Minimum duration (s)
                </Label>
                <Input id="svin-min" type="number" inputMode="numeric" min={SVIN_DURATION.min} max={SVIN_DURATION.max} step={1} value={durStr} onChange={(e) => setDur(e.target.value)} className="num" />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="svin-acc" className="text-[12px] leading-4 text-ink-2">
                  Accuracy limit (m)
                </Label>
                <Input id="svin-acc" type="number" inputMode="decimal" min={SVIN_ACC.min} max={SVIN_ACC.max} step={0.01} value={accStr} onChange={(e) => setAcc(e.target.value)} className="num" />
              </div>
              {!durOk ? <p className="col-span-2 text-[12px] leading-4 text-status-warning">Duration is a whole number of seconds between {SVIN_DURATION.min} and {SVIN_DURATION.max}.</p> : null}
              {!accOk ? <p className="col-span-2 text-[12px] leading-4 text-status-warning">Accuracy limit is between {SVIN_ACC.min} and {SVIN_ACC.max} m.</p> : null}
              <p className="col-span-2 text-[12px] leading-4 text-ink-2">Applying the same values to a running survey changes nothing on this firmware; use Restart survey-in to start it over.</p>
            </div>
          ) : null}
          {mode === "fixed" ? (
            <div className="flex flex-col gap-1">
              <Label htmlFor="fixed-site" className="text-[12px] leading-4 text-ink-2">
                Site
              </Label>
              <select id="fixed-site" value={siteOk ? site : ""} onChange={(e) => setSiteName(e.target.value)} className="h-9 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink">
                <option value="">Choose a site…</option>
                {sites.map((s) => (
                  <option key={s.name} value={s.name}>
                    {s.name} · {s.source}
                    {s.active ? " (active)" : ""}
                  </option>
                ))}
              </select>
              {sites.length === 0 ? (
                <p className="text-[12px] leading-4 text-status-warning">There are no sites yet: freeze a survey-in or add coordinates below, then come back.</p>
              ) : !siteOk ? (
                <p className="text-[12px] leading-4 text-status-warning">Fixed mode needs a site to sit on: pick one above.</p>
              ) : null}
            </div>
          ) : null}
          <div className="flex items-center gap-3">
            <Button type="button" disabled={!canApply} onClick={() => apply.mutate(body())}>
              Apply mode
            </Button>
            {view && picked && picked !== view.mode ? <span className="text-[12px] leading-4 text-ink-2">Now: {view.mode}</span> : null}
          </div>
        </fieldset>
        {apply.isError ? (
          <Alert variant="destructive">
            <AlertDescription className="text-[14px] leading-5">{describeError(apply.error)}</AlertDescription>
          </Alert>
        ) : null}
        {done ? (
          <p role="status" aria-label="Mode result" className="text-ink-2">
            {done.view.mode === done.asked ? (
              <>
                Position mode set to <span className="text-ink">{done.view.mode}</span>
                {done.view.mode === "fixed" && done.view.site ? ` on ${done.view.site}` : ""} at <span className="num">{done.at} UTC</span>.
              </>
            ) : (
              <>
                Asked for {done.asked} at <span className="num">{done.at} UTC</span>, but the receiver is in <span className="text-ink">{done.view.mode}</span>
                {live.reason ? `: ${live.reason}` : "."}
              </>
            )}
          </p>
        ) : null}
        {hasPending ? (
          <p className="text-[12px] leading-4 text-ink-2">
            After a restart or recreate the base will follow what .env says
            {pendingMode !== undefined ? (
              <>
                : mode <span className="num text-ink">{pendingMode}</span>
              </>
            ) : null}
            {pendingSite !== undefined ? (
              <>
                {pendingMode !== undefined ? ", " : ": "}site <span className="num text-ink">{pendingSite ?? "none"}</span>
              </>
            ) : null}
            . The running base is doing something else right now; Apply mode writes .env too and clears this.
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

// ----------------------------------------------------------------------------- survey-in panel

function SurveyPanel({
  svin,
  view,
  coordMode,
  onOutcome,
}: {
  svin: SurveyIn;
  view: BaseModeView | undefined;
  coordMode: CoordMode;
  onOutcome: (o: SiteOutcome) => void;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [activate, setActivate] = useState(true);
  const [restarted, setRestarted] = useState<string | null>(null);
  const restart = useMutation({
    mutationFn: () => restartSurvey(),
    onSuccess: () => {
      setRestarted(nowUtc());
      void qc.invalidateQueries({ queryKey: ["base"] });
    },
  });
  const freeze = useMutation({ mutationFn: (b: { name: string; activate: boolean }) => freezeSurvey(b) });

  const available = view?.available ?? false;
  const minDur = view?.svin.min_duration_s ?? null;
  const accLimit = view?.svin.acc_limit_m ?? null;
  const gatePct = svin.mean_acc_m != null && accLimit != null && svin.mean_acc_m > 0 ? Math.min(100, (accLimit / svin.mean_acc_m) * 100) : 0;
  const mean = svin.mean_x_m != null && svin.mean_y_m != null && svin.mean_z_m != null ? ecefToLlh(svin.mean_x_m, svin.mean_y_m, svin.mean_z_m) : null;
  const meanLines: [string, string] = mean ? sitePosition({ lat: mean[0], lon: mean[1], height_m: mean[2], x: svin.mean_x_m!, y: svin.mean_y_m!, z: svin.mean_z_m! }, coordMode) : [DASH, DASH];
  const stateBadge = svin.valid ? { level: "good" as const, label: "Survey-in complete" } : svin.active ? { level: "warning" as const, label: "Survey-in running" } : null;
  const canRestart = available && view?.mode === "survey-in";
  const canFreeze = available && svin.valid;
  const restartWhy = !view ? null : !available ? "Restart and freeze need a base-mode manager; this source has none." : view.mode !== "survey-in" ? `The base is in ${view.mode} mode: switch to survey-in before restarting it.` : null;

  const doFreeze = async () => {
    const trimmed = name.trim();
    const result: SiteResult = await freeze.mutateAsync({ name: trimmed, activate });
    setName("");
    onOutcome(await finishSiteResult("freeze", result, activate));
  };

  return (
    <Panel className="col-span-12 lg:col-span-4" title="Survey-in">
      <div className="flex flex-col gap-3">
        {stateBadge ? <StatusBadge level={stateBadge.level} label={stateBadge.label} className="self-start" /> : <p className="text-ink-2">No survey-in is running.</p>}
        <Gauge label="Elapsed" value={svin.dur_s} max={minDur ?? Math.max(svin.dur_s, 1)} level={svin.valid ? "good" : undefined} format={(v) => `${fmtDuration(v)} of ${minDur != null ? fmtDuration(minDur) : DASH}`} />
        <Gauge label="Accuracy gate (σ)" value={Math.round(gatePct * 10) / 10} max={100} level={svin.valid ? "good" : undefined} format={() => `${fmtAcc(svin.mean_acc_m)} · limit ${fmtAcc(accLimit)}`} />
        <div>
          <Stat label="Observations" value={String(svin.obs)} />
          <div className="flex items-baseline justify-between gap-3 py-1.5">
            <span className="text-ink-2">Mean position</span>
            <TwoLines lines={meanLines} className="text-right" />
          </div>
        </div>
        <p className="text-[12px] leading-4 text-ink-2">
          The survey ends when both bars are full. The gate is the survey's own mean accuracy (σ, NAV-SVIN meanAcc), not the fix's hAcc: indoors σ settles around 10 m and never meets the limit, however long it runs — put the antenna under open sky.
        </p>
        {restartWhy ? <p className="text-[12px] leading-4 text-ink-2">{restartWhy}</p> : null}
        <div className="flex flex-wrap gap-2">
          <ConfirmDialog
            trigger={
              <Button type="button" variant="outline" disabled={!canRestart}>
                Restart survey-in
              </Button>
            }
            title="Restart the survey-in?"
            body="Sends TMODE off, then survey-in again with the same parameters, so the receiver starts averaging from zero. Nothing is deleted and observations keep flowing; RTCM 1005 pauses until the new survey is valid. This is its own step because re-sending the same parameters does not restart a survey on this firmware."
            confirmLabel="Restart"
            onConfirm={() => restart.mutateAsync()}
          />
          <ConfirmDialog
            trigger={
              <Button type="button" disabled={!canFreeze}>
                Freeze as site
              </Button>
            }
            title="Freeze the survey-in as a site"
            body="Saves the survey's mean position as a site (frame WGS84 as the receiver reports it, source survey-in). With Activate now, the base switches to fixed mode on it and the setting is saved to .env."
            confirmLabel="Freeze"
            confirmDisabled={!name.trim()}
            onConfirm={doFreeze}
          >
            <div className="flex flex-col gap-2 text-[14px] leading-5">
              <div className="flex flex-col gap-1">
                <Label htmlFor="freeze-name">Site name</Label>
                <Input id="freeze-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="roof-2026" autoComplete="off" spellCheck={false} />
              </div>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={activate} onChange={(e) => setActivate(e.target.checked)} className="accent-brass" />
                Activate now (switch the base to fixed mode on it)
              </label>
            </div>
          </ConfirmDialog>
        </div>
        {restarted ? (
          <p role="status" aria-label="Restart result" className="text-ink-2">
            Survey-in restarted at <span className="num">{restarted} UTC</span>; the counters start over on the next epoch.
          </p>
        ) : null}
      </div>
    </Panel>
  );
}

/**
 * Turn a `{site, applied}` answer into an outcome. When the receiver took the site, follow with
 * `PUT /api/base/mode {"mode":"fixed","site"}` so BASE_MODE/ACTIVE_SITE persist (ruling 1); when
 * it did not, there is nothing true to persist — the running settings mirror the mode the base
 * ended up in — so the warning stands alone.
 */
async function finishSiteResult(kind: SiteOutcome["kind"], result: SiteResult, activated: boolean): Promise<SiteOutcome> {
  const base: SiteOutcome = { kind, name: result.site.name, applied: result.applied, activated, at: nowUtc(), persist: null };
  if (!activated || !result.applied) return base;
  try {
    await putBaseMode({ mode: "fixed", site: result.site.name });
    return { ...base, persist: { ok: true } };
  } catch (err) {
    return { ...base, persist: { ok: false, detail: describeError(err) } };
  }
}

// -------------------------------------------------------------------------- verification panel

function VerificationPanel({ view, live, active, coordMode }: { view: BaseModeView | undefined; live: BaseInfo; active: SiteT | undefined; coordMode: CoordMode }) {
  const check = siteCheck(live, view);
  const last = view?.last_1005 ?? null;
  const delta = last && active ? { dx: last.x - active.x, dy: last.y - active.y, dz: last.z - active.z } : null;
  const worst = delta ? Math.max(Math.abs(delta.dx), Math.abs(delta.dy), Math.abs(delta.dz)) : null;
  const mm = (m: number) => `${(m * 1000).toFixed(1)} mm`;
  const fixed = (live.mode ?? view?.mode) === "fixed";

  let sentence: ReactNode;
  if (view && !view.available) sentence = "Verification needs a base-mode manager; this source has none.";
  else if (!fixed) sentence = "Verification runs while a fixed site is active: the RTCM 1005 the rovers receive is compared with the site's coordinates.";
  else if (!last) sentence = "Waiting for RTCM 1005 — the receiver broadcasts it once it holds a valid position.";
  else if (!active) sentence = "A 1005 is flowing but no site is marked active in the table, so there is nothing to compare it with.";
  else if (worst != null && worst <= SITE_TOLERANCE_M) sentence = `RTCM 1005 matches the active site: every axis within ${mm(SITE_TOLERANCE_M)}.`;
  else sentence = `RTCM 1005 differs from the active site by up to ${fmtMeters(worst, 3)}: the receiver is not on ${active.name}.`;

  return (
    <Panel className="col-span-12 lg:col-span-4" title="Verification">
      <div className="flex flex-col gap-3">
        {check ? <StatusBadge level={check.level} label={check.label} className="self-start" /> : null}
        <p className="text-ink-2">{sentence}</p>
        {delta && active ? (
          <div>
            <Stat label="ΔX" value={mm(delta.dx)} hint="1005 minus site" />
            <Stat label="ΔY" value={mm(delta.dy)} />
            <Stat label="ΔZ" value={mm(delta.dz)} />
          </div>
        ) : null}
        {last ? (
          <div>
            <p className="mb-1 text-[12px] leading-4 text-ink-2">Last RTCM 1005 (station {last.station_id})</p>
            <Stat label="X" value={fmtMeters(last.x, 4)} />
            <Stat label="Y" value={fmtMeters(last.y, 4)} />
            <Stat label="Z" value={fmtMeters(last.z, 4)} />
          </div>
        ) : null}
        {active ? (
          <div className="flex items-baseline justify-between gap-3 border-t border-line pt-2">
            <span className="text-ink-2">Active site {active.name}</span>
            <TwoLines lines={sitePosition(active, coordMode)} className="text-right" />
          </div>
        ) : null}
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------------- sites table

function siteColumns(coordMode: CoordMode, actions: (s: SiteT) => ReactNode): Column<SiteT>[] {
  return [
    {
      key: "name",
      header: "Name",
      cell: (s) => (
        <span className="inline-flex items-center gap-2">
          <span>{s.name}</span>
          {s.active ? <StatusBadge level="good" label="active" className="text-[12px]" /> : null}
        </span>
      ),
      sortValue: (s) => s.name,
    },
    { key: "pos", header: "Position", cell: (s) => <TwoLines lines={sitePosition(s, coordMode)} />, sortValue: (s) => s.lat },
    { key: "h", header: "Height (ellipsoid)", cell: (s) => fmtMeters(s.height_m, 3), sortValue: (s) => s.height_m, align: "right" },
    {
      key: "sigma",
      header: "σ per axis",
      cell: (s) => (s.sigma_x != null && s.sigma_y != null && s.sigma_z != null ? (s.sigma_x === s.sigma_y && s.sigma_y === s.sigma_z ? fmtAcc(s.sigma_x) : `${fmtAcc(s.sigma_x)} / ${fmtAcc(s.sigma_y)} / ${fmtAcc(s.sigma_z)}`) : DASH),
      sortValue: (s) => s.sigma_x,
      align: "right",
    },
    { key: "frame", header: "Frame", cell: (s) => `${s.frame}${s.epoch ? ` @ ${s.epoch}` : ""}`, sortValue: (s) => s.frame },
    { key: "source", header: "Source", cell: (s) => <span className="text-ink-2">{s.source}</span>, sortValue: (s) => s.source },
    { key: "created", header: "Created", cell: (s) => <span className="num">{fmtUtcDate(s.created_utc)}</span>, sortValue: (s) => s.created_utc },
    { key: "actions", header: "", cell: actions },
  ];
}

// --------------------------------------------------------------------------------------- page

/**
 * Where the base stands: the position mode and its survey-in parameters, the survey's
 * progress against its two gates, the saved sites (activate, delete, add), the RTCM 1005
 * check against the active site, and the steps to a centimetre site from a PPP service.
 */
export default function Site() {
  const state = useLive((s) => s.state);
  const live = useLive((s) => s.base);
  const stale = useStale();
  const [coordMode] = useCoordMode();
  const qc = useQueryClient();
  const sites = useSites();
  const mode = useBaseMode();
  const config = useConfig();
  const [window24] = useState(() => {
    const now = new Date();
    return [new Date(now.getTime() - 24 * 3600_000).toISOString(), now.toISOString()] as const;
  });
  const availability = useAvailability(window24[0], window24[1]);
  const [addOpen, setAddOpen] = useState(false);
  const [pppOpen, setPppOpen] = useState(false);
  const [outcome, setOutcome] = useState<SiteOutcome | null>(null);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["base"] });
    void qc.invalidateQueries({ queryKey: ["config"] });
  };
  const activate = useMutation({ mutationFn: (name: string) => activateSite(name) });
  const remove = useMutation({ mutationFn: (name: string) => deleteSite(name), onSuccess: invalidate });

  const finish = (o: SiteOutcome) => {
    setOutcome(o);
    invalidate();
  };
  const doActivate = async (name: string) => finish(await finishSiteResult("activate", await activate.mutateAsync(name), true));
  const doAdd = async (s: SiteInput) => {
    await addSite(s);
    setAddOpen(false);
    setPppOpen(false);
    invalidate();
  };

  if (!state) {
    return (
      <>
        <PageHeader title="Site" />
        <EmptyState title="Waiting for the receiver" body="The position mode, the survey-in and the site check appear as soon as the daemon reports a snapshot." />
      </>
    );
  }

  const view = mode.data;
  const rows = Array.isArray(sites.data) ? sites.data : [];
  const active = rows.find((s) => s.active);
  const badge = headerBadge(view, state.survey_in);
  const hours = Array.isArray(availability.data) ? availability.data.filter((h) => h.available).length : null;

  const rowActions = (s: SiteT) => (
    <div className="flex justify-end gap-1">
      {!s.active ? (
        <ConfirmDialog
          trigger={
            <Button type="button" size="sm" variant="outline">
              Activate
            </Button>
          }
          title={`Activate ${s.name}?`}
          body={`A running base switches to fixed mode on ${s.name} within 10 s and broadcasts its coordinates in RTCM 1005; rover positions shift by the offset between the sites. The mode and site are then saved to .env so they survive a restart.`}
          confirmLabel="Activate"
          onConfirm={() => doActivate(s.name)}
        />
      ) : null}
      <ConfirmDialog
        trigger={
          <Button type="button" size="sm" variant="ghost" disabled={s.active} title={s.active ? "The active site cannot be deleted: activate another first" : undefined}>
            Delete
          </Button>
        }
        title={`Delete site ${s.name}?`}
        body="The site's coordinates are lost and can only come back by entering them again. Nothing on the receiver changes."
        confirmLabel="Delete"
        destructive
        requireText={s.name}
        onConfirm={() => remove.mutateAsync(s.name)}
      />
    </div>
  );

  return (
    <>
      <PageHeader title="Site">{badge ? <StatusBadge level={badge.level} label={badge.label} /> : null}</PageHeader>
      <div data-testid="site-grid" data-stale={stale} className={cn("grid grid-cols-12 gap-4", stale && "[&_.num]:text-ink-3")}>
        <PositionModePanel view={view} loading={mode.isPending} sites={rows} config={config.data} live={live} />
        <SurveyPanel svin={state.survey_in} view={view} coordMode={coordMode} onOutcome={finish} />
        <VerificationPanel view={view} live={live} active={active} coordMode={coordMode} />

        <Panel
          className="col-span-12"
          title="Sites"
          bodyClassName="p-2"
          actions={
            <Dialog open={addOpen} onOpenChange={setAddOpen}>
              <DialogTrigger asChild>
                <Button type="button" size="sm">
                  Add site
                </Button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-2xl">
                <DialogHeader>
                  <DialogTitle className="text-[16px] leading-6 font-medium">Add a site</DialogTitle>
                  <DialogDescription className="text-[14px] leading-5 text-ink-2">Coordinates as ECEF metres or as latitude, longitude and ellipsoidal height. Adding does not activate.</DialogDescription>
                </DialogHeader>
                <SiteForm onSubmit={doAdd} />
              </DialogContent>
            </Dialog>
          }
        >
          {sites.isPending ? (
            <p className="p-2 text-ink-3">Loading sites…</p>
          ) : sites.isError ? (
            <Alert variant="destructive" className="m-2">
              <AlertDescription className="text-[14px] leading-5">The sites could not be read: {describeError(sites.error)}</AlertDescription>
            </Alert>
          ) : (
            <DataTable
              aria-label="Sites"
              columns={siteColumns(coordMode, rowActions)}
              rows={rows}
              rowKey={(s) => s.name}
              dense
              empty={<EmptyState title="No sites yet" body="Freeze a completed survey-in, or add the coordinates from a PPP report, then activate the site to put the base on it." />}
            />
          )}
          {outcome ? (
            <div className="px-2 pt-3">
              <SiteOutcomeLine outcome={outcome} view={view} live={live} />
            </div>
          ) : null}
          {remove.isError ? (
            <div className="px-2 pt-3">
              <Alert variant="destructive">
                <AlertDescription className="text-[14px] leading-5">{describeError(remove.error)}</AlertDescription>
              </Alert>
            </div>
          ) : null}
        </Panel>

        <Panel className="col-span-12 md:col-span-5 lg:col-span-4" title={active ? `Map · ${active.name}` : "Map"} bodyClassName="flex p-0">
          {active && active.lat != null && active.lon != null ? (
            <MapPanel lat={active.lat} lon={active.lon} hAcc={null} />
          ) : (
            <div className="p-4">
              <EmptyState title="No active site" body="The map shows the active site once one is marked in the table." />
            </div>
          )}
        </Panel>

        <Panel className="col-span-12 md:col-span-7 lg:col-span-8" title="Centimetre site from PPP">
          <ol className="grid gap-4 xl:grid-cols-3">
            <li className="flex flex-col gap-2 rounded-md border border-line p-3">
              <p className="font-medium">1. Collect 24 hours of raw data and export it</p>
              <p className="text-ink-2">
                {hours == null ? (availability.isError ? "Raw-log availability could not be read." : "Checking the raw logs…") : <><span className="num text-ink">{hours} of 24 hours</span> of raw data available in the last 24 h.</>}
              </p>
              <p className="text-[12px] leading-4 text-ink-2">Download the window from Logs; convert it to RINEX with RTKLIB convbin until the RINEX export arrives in Phase 5. Note the antenna height above the mark.</p>
              <Link to="/logs" className="self-start text-ink-2 hover:text-ink hover:underline">
                Open logs
              </Link>
            </li>
            <li className="flex flex-col gap-2 rounded-md border border-line p-3">
              <p className="font-medium">2. Submit the observation file to a PPP service</p>
              <ul className="flex flex-col gap-1.5">
                {PPP_SERVICES.map((s) => (
                  <li key={s.name}>
                    <a href={s.url} target="_blank" rel="noreferrer" className="text-ink hover:underline">
                      {s.name}
                    </a>
                    <span className="block text-[12px] leading-4 text-ink-2">{s.note}</span>
                  </li>
                ))}
              </ul>
              <p className="text-[12px] leading-4 text-ink-2">Static mode; the report comes back by e-mail with ECEF X/Y/Z, per-axis sigma, frame and epoch.</p>
            </li>
            <li className="flex flex-col gap-2 rounded-md border border-line p-3">
              <p className="font-medium">3. Enter the result and activate the site</p>
              <p className="text-ink-2">Type the ECEF coordinates and sigma from the report; frame and source are pre-filled for CSRS-PPP. Activate the site from the table when the antenna is on the mark.</p>
              <div className="flex flex-wrap gap-2">
                <Dialog open={pppOpen} onOpenChange={setPppOpen}>
                  <DialogTrigger asChild>
                    <Button type="button" size="sm">
                      Enter PPP result
                    </Button>
                  </DialogTrigger>
                  <DialogContent className="sm:max-w-2xl">
                    <DialogHeader>
                      <DialogTitle className="text-[16px] leading-6 font-medium">Site from a PPP result</DialogTitle>
                      <DialogDescription className="text-[14px] leading-5 text-ink-2">The report's ECEF coordinates in metres; sigma is the per-axis value it quotes. Change the source for AUSPOS or OPUS.</DialogDescription>
                    </DialogHeader>
                    <SiteForm initial={{ source: "csrs-ppp", frame: "ITRF2020" }} onSubmit={doAdd} />
                  </DialogContent>
                </Dialog>
                <Button type="button" size="sm" variant="outline" disabled title="Reading the PPP report file arrives with Phase 5" className="h-auto min-w-0 max-w-full whitespace-normal text-left">
                  Import report file · coming in Phase 5
                </Button>
              </div>
            </li>
          </ol>
        </Panel>
      </div>
    </>
  );
}
