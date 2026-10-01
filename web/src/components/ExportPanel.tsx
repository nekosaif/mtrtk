import { useEffect, useId, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { toast } from "sonner";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { describeError, submitExport } from "@/lib/api";
import { fromInput, toInput } from "@/lib/format";
import { usePresets } from "@/lib/queries";
import type { ExportRequest, Preset } from "@/lib/types";

const HOUR_MS = 3600_000;
/** `POST /api/export`'s own cap (`MAX_WINDOW`), mirrored so the button says so first. */
export const EXPORT_MAX_DAYS = 7;
/** Where the export jobs panel sits on the page; the panel's `id`. */
export const EXPORT_JOBS_ANCHOR = "export-jobs";
/** The export panel's own `id`: a deep link (`/logs?export=…`) scrolls it into view. */
export const EXPORT_PANEL_ANCHOR = "export-rinex";

/** Why the window cannot be exported yet, or null when it can. */
function windowProblem(from: string, to: string): string | null {
  const a = fromInput(from);
  const b = fromInput(to);
  if (a == null || b == null) return "Enter both times.";
  if (a >= b) return "From must be before to.";
  if (b - a > EXPORT_MAX_DAYS * 24 * HOUR_MS) return `At most ${EXPORT_MAX_DAYS} days per export.`;
  return null;
}

/** The fixed options of a preset, worded: "RINEX 3.04, 30 s interval, Hatanaka + gzip". */
function fixedOptions(p: Preset): string {
  const parts = [`RINEX ${p.version}`, p.interval_s ? `${p.interval_s} s interval` : "native interval"];
  if (p.hatanaka) parts.push(p.gzip ? "Hatanaka + gzip" : "Hatanaka");
  else if (p.gzip) parts.push("gzip");
  if (p.exclude_systems.length) parts.push("GPS only");
  return `${parts.join(", ")}.`;
}

/**
 * Start a RINEX export job: a preset (with what its service asks of the data), a UTC window,
 * and - for the adjustable `generic` preset only - interval and compression. The fixed presets
 * take no overrides (the daemon refuses them). The job's progress and files appear in the
 * export jobs panel; a refusal (one export at a time, no raw logs in the window) shows verbatim.
 *
 * `window` follows a window picked elsewhere on the page (an hour on the availability strip);
 * otherwise the window is the last `initialHours` whole hours.
 */
export function ExportPanel({
  initialPreset = "csrs-ppp",
  initialHours = 24,
  window,
  onSubmitted,
  focusTarget = false,
}: {
  /** Put the focus on the target select once the presets are in (the page was deep-linked here). */
  focusTarget?: boolean;
  initialPreset?: string;
  initialHours?: number;
  window?: [string, string];
  onSubmitted?: (jobId: string) => void;
}) {
  const ids = useId();
  const presets = usePresets();
  const list = Array.isArray(presets.data) ? presets.data : [];
  const [presetId, setPresetId] = useState(initialPreset);
  const [hourNow] = useState(() => Math.floor(Date.now() / HOUR_MS) * HOUR_MS);
  const [from, setFrom] = useState(() => window?.[0] ?? toInput(hourNow - initialHours * HOUR_MS));
  const [to, setTo] = useState(() => window?.[1] ?? toInput(hourNow));
  const [seenWindow, setSeenWindow] = useState(window);
  if (window !== seenWindow) {
    // A new window from the page wins over what was typed (set during render, not in an effect).
    setSeenWindow(window);
    if (window) {
      setFrom(window[0]);
      setTo(window[1]);
    }
  }
  const [interval, setIntervalText] = useState("");
  const [hatanaka, setHatanaka] = useState(false);
  const [gzip, setGzip] = useState(false);

  const chosen = list.find((p) => p.id === presetId) ?? list[0];
  const selectRef = useRef<HTMLSelectElement>(null);
  const focused = useRef(false);
  const ready = chosen != null;
  useEffect(() => {
    if (!focusTarget || !ready || focused.current) return;
    focused.current = true;
    selectRef.current?.focus({ preventScroll: true });
  }, [focusTarget, ready]);
  const problem = windowProblem(from, to);
  const intervalS = interval.trim() === "" ? null : Number(interval);
  // Only the adjustable preset sends (and shows) the interval, so only it can be held by one.
  const intervalBad = Boolean(chosen?.adjustable) && intervalS != null && !(Number.isFinite(intervalS) && intervalS > 0);

  const submit = useMutation({
    mutationFn: () => {
      const body: ExportRequest = { start: `${from}:00Z`, end: `${to}:00Z`, preset: chosen!.id };
      if (chosen!.adjustable) Object.assign(body, { interval_s: intervalS, hatanaka, gzip });
      return submitExport(body);
    },
    onSuccess: (job) => {
      toast.success("Export queued", { description: `Job ${job.id}` });
      onSubmitted?.(job.id);
    },
  });

  if (presets.isError) {
    return (
      <Alert variant="destructive">
        <AlertDescription className="text-[14px] leading-5">The export presets could not be read: {describeError(presets.error)}</AlertDescription>
      </Alert>
    );
  }
  if (!chosen) return <p className="text-ink-2">{presets.isPending ? "Loading presets…" : "The daemon reports no export presets."}</p>;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${ids}-preset`}>Target</Label>
        <select ref={selectRef} id={`${ids}-preset`} value={chosen.id} onChange={(e) => setPresetId(e.target.value)} className="h-9 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink">
          {list.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <p className="text-[12px] leading-4 text-ink-2">{chosen.description}</p>
        {chosen.constraints.length ? (
          <ul className="list-disc pl-4 text-[12px] leading-4 text-ink-2">
            {chosen.constraints.map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        ) : null}
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-from`}>From (UTC)</Label>
          <Input id={`${ids}-from`} type="datetime-local" className="num" value={from} onChange={(e) => setFrom(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${ids}-to`}>To (UTC)</Label>
          <Input id={`${ids}-to`} type="datetime-local" className="num" value={to} onChange={(e) => setTo(e.target.value)} />
        </div>
      </div>
      {problem ? <p className="text-[12px] leading-4 text-ink-2">{problem}</p> : null}

      {chosen.adjustable ? (
        <div className="grid items-end gap-3 sm:grid-cols-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor={`${ids}-interval`}>Interval (s)</Label>
            <Input id={`${ids}-interval`} inputMode="decimal" placeholder="native" className="num" value={interval} onChange={(e) => setIntervalText(e.target.value)} />
          </div>
          <div className="flex h-9 items-center gap-2">
            <Switch id={`${ids}-hatanaka`} checked={hatanaka} onCheckedChange={setHatanaka} />
            <Label htmlFor={`${ids}-hatanaka`}>Hatanaka</Label>
          </div>
          <div className="flex h-9 items-center gap-2">
            <Switch id={`${ids}-gzip`} checked={gzip} onCheckedChange={setGzip} />
            <Label htmlFor={`${ids}-gzip`}>gzip</Label>
          </div>
          {intervalBad ? <p className="text-[12px] leading-4 text-status-warning-text sm:col-span-3">Interval is a positive number of seconds; leave it empty for the native rate.</p> : null}
        </div>
      ) : (
        <p className="text-[12px] leading-4 text-ink-2">{fixedOptions(chosen)}</p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        {chosen.service_url ? (
          <a href={chosen.service_url} target="_blank" rel="noreferrer" className="text-ink hover:underline">
            Open {chosen.name}
          </a>
        ) : (
          <span />
        )}
        <Button type="button" onClick={() => submit.mutate()} disabled={submit.isPending || problem != null || intervalBad}>
          {submit.isPending ? "Starting…" : "Start export"}
        </Button>
      </div>

      {submit.isError ? (
        <Alert variant="destructive">
          <AlertDescription className="text-[14px] leading-5">{describeError(submit.error)}</AlertDescription>
        </Alert>
      ) : null}
      {submit.isSuccess ? (
        <p role="status" className="text-[14px] leading-5 text-ink-2">
          Job <span className="num text-ink">{submit.data.id}</span> is queued; its progress and files appear under{" "}
          <a href={`#${EXPORT_JOBS_ANCHOR}`} className="text-ink hover:underline">
            Export jobs
          </a>
          .
        </p>
      ) : null}
    </div>
  );
}
