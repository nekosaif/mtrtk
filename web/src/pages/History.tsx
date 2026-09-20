import { useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { PageHeader } from "@/app/PageHeader";
import { Panel } from "@/components/Panel";
import { EmptyState } from "@/components/EmptyState";
import { TimeSeries, type TsPoint } from "@/components/charts/TimeSeries";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { describeError } from "@/lib/api";
import { fmtAcc } from "@/lib/format";
import { usePref } from "@/lib/prefs";
import { useHistory, useHistoryMetrics } from "@/lib/queries";
import { cn } from "@/lib/utils";

export interface HistoryMetric {
  /** The 1 s sample column; the daemon maps it to its rollup at `res=1m` and echoes this name back. */
  key: string;
  label: string;
  unit: string;
  group: string;
  format: (v: number) => string;
  color?: string;
}

/**
 * Labels, units and formats for the metrics worth a chart. Only the entries the daemon lists in
 * `GET /api/history/metrics` are offered; a name missing there (no thermal sensor, an older
 * schema) is simply not shown.
 */
export const HISTORY_METRICS: HistoryMetric[] = [
  { key: "h_acc_m", label: "Horizontal accuracy", unit: "m", group: "Position", format: (v) => fmtAcc(v) },
  { key: "v_acc_m", label: "Vertical accuracy", unit: "m", group: "Position", format: (v) => fmtAcc(v) },
  { key: "pdop", label: "PDOP", unit: "", group: "Position", format: (v) => v.toFixed(2) },
  { key: "nsat_used", label: "Satellites used", unit: "", group: "Satellites", format: (v) => v.toFixed(0), color: "var(--sys-gps)" },
  { key: "nsat_tracked", label: "Satellites tracked", unit: "", group: "Satellites", format: (v) => v.toFixed(0), color: "var(--sys-gps)" },
  { key: "cno_mean", label: "Mean C/N0", unit: "dB-Hz", group: "Satellites", format: (v) => v.toFixed(1), color: "var(--sys-galileo)" },
  { key: "jam_ind", label: "Jamming indicator", unit: "", group: "RF", format: (v) => v.toFixed(0), color: "var(--status-serious)" },
  { key: "agc_cnt", label: "AGC", unit: "", group: "RF", format: (v) => v.toFixed(0) },
  { key: "noise_per_ms", label: "Noise per ms", unit: "", group: "RF", format: (v) => v.toFixed(0) },
  { key: "rtcm_bytes_per_s", label: "RTCM output", unit: "B/s", group: "Corrections", format: (v) => v.toFixed(0), color: "var(--sys-glonass)" },
  { key: "ntrip_clients", label: "NTRIP clients", unit: "", group: "Corrections", format: (v) => v.toFixed(0), color: "var(--sys-glonass)" },
  { key: "cpu_pct", label: "CPU", unit: "%", group: "System", format: (v) => v.toFixed(0), color: "var(--ink-2)" },
  { key: "mem_pct", label: "Memory", unit: "%", group: "System", format: (v) => v.toFixed(0), color: "var(--ink-2)" },
  { key: "disk_free_gb", label: "Disk free", unit: "GB", group: "System", format: (v) => v.toFixed(1), color: "var(--ink-2)" },
  { key: "temp_c", label: "Temperature", unit: "°C", group: "System", format: (v) => v.toFixed(0), color: "var(--status-serious)" },
];
export const HISTORY_GROUPS = ["Position", "Satellites", "RF", "Corrections", "System"] as const;

/** Range chips; every one asks with `res=auto`, so the daemon picks 1 s or 1 min and says which. */
export const RANGES = [
  { label: "1 h", s: 3600 },
  { label: "6 h", s: 6 * 3600 },
  { label: "24 h", s: 86400 },
  { label: "7 d", s: 7 * 86400 },
  { label: "90 d", s: 90 * 86400 },
] as const;
const DEFAULT_RANGE_S = 86400;
const DEFAULT_METRICS = ["h_acc_m", "nsat_used", "cno_mean"];

const isStringList = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === "string");

/** The requested column's points, keyed on the name that was asked for (the daemon echoes it). */
export function seriesOf(columns: string[], rows: (number | null)[][], key: string): TsPoint[] {
  const idx = columns.indexOf(key);
  if (idx <= 0) return [];
  return rows.map((r) => ({ t: r[0] as number, v: r[idx] ?? null }));
}

/**
 * The sampler's history as one chart per metric — never two series on one axis. The range chips
 * pick the window, the daemon picks the resolution, the checkboxes pick the metrics (both
 * remembered per browser); a window the daemon refuses shows its own words.
 */
export default function History() {
  const [rangeRaw, setRange] = usePref<unknown>("historyRange", DEFAULT_RANGE_S);
  const rangeS = RANGES.some((r) => r.s === rangeRaw) ? (rangeRaw as number) : DEFAULT_RANGE_S;
  const [selectedRaw, setSelected] = usePref<unknown>("historyMetrics", DEFAULT_METRICS);
  const selected = isStringList(selectedRaw) ? selectedRaw : DEFAULT_METRICS;
  const [toMs, setToMs] = useState(() => Date.now());
  const catalogue = useHistoryMetrics();
  const known = useMemo(() => new Set(catalogue.data?.["1s"] ?? []), [catalogue.data]);
  const offered = useMemo(() => (catalogue.data ? HISTORY_METRICS.filter((m) => known.has(m.key)) : []), [catalogue.data, known]);
  const wanted = useMemo(() => (catalogue.data ? selected.filter((k) => known.has(k) && HISTORY_METRICS.some((m) => m.key === k)) : []), [catalogue.data, known, selected]);

  const fromMs = toMs - rangeS * 1000;
  const from = new Date(fromMs).toISOString();
  const to = new Date(toMs).toISOString();
  const data = useHistory(wanted, from, to, "auto");

  const pickRange = (s: number) => {
    setToMs(Date.now());
    setRange(s);
  };
  const toggle = (k: string) => setSelected(selected.includes(k) ? selected.filter((x) => x !== k) : [...selected, k]);
  const res = data.data?.res;
  const gapS = res === "1s" ? 10 : 300;

  return (
    <>
      <PageHeader title="History">
        <div className="flex flex-wrap items-center gap-1">
          {RANGES.map((r) => (
            <button
              key={r.s}
              type="button"
              aria-pressed={rangeS === r.s}
              onClick={() => pickRange(r.s)}
              className={cn("num rounded-full border px-3 py-1 text-[14px] leading-5", rangeS === r.s ? "border-brass text-ink" : "border-line text-ink-2 hover:text-ink")}
            >
              {r.label}
            </button>
          ))}
          <Button type="button" variant="ghost" size="icon-sm" aria-label="Refresh" title="Move the window to now" onClick={() => setToMs(Date.now())}>
            <RefreshCw aria-hidden />
          </Button>
        </div>
      </PageHeader>
      <div className="grid grid-cols-12 gap-4">
        <Panel className="col-span-12 lg:col-span-3" title="Metrics">
          {catalogue.isPending ? (
            <p className="text-ink-2">Loading the metric catalogue…</p>
          ) : catalogue.isError ? (
            <Alert variant="destructive">
              <AlertDescription className="text-[14px] leading-5">The metric catalogue could not be read: {describeError(catalogue.error)}</AlertDescription>
            </Alert>
          ) : offered.length === 0 ? (
            <EmptyState title="No metrics" body="The daemon lists nothing to chart yet." />
          ) : (
            HISTORY_GROUPS.filter((g) => offered.some((m) => m.group === g)).map((g) => (
              <div key={g} className="mb-3 last:mb-0">
                <p className="mb-1 text-[12px] leading-4 text-ink-2">{g}</p>
                {offered
                  .filter((m) => m.group === g)
                  .map((m) => (
                    <label key={m.key} className="flex items-center gap-2 py-0.5 text-[14px] leading-5">
                      <input type="checkbox" className="accent-brass" checked={selected.includes(m.key)} onChange={() => toggle(m.key)} />
                      {m.label}
                      {m.unit ? <span className="text-[12px] text-ink-2">{m.unit}</span> : null}
                    </label>
                  ))}
              </div>
            ))
          )}
        </Panel>

        <div className="col-span-12 flex min-w-0 flex-col gap-4 lg:col-span-9">
          {catalogue.data && wanted.length === 0 ? <EmptyState title="Pick a metric" body="Each metric gets its own chart so the scales stay honest." /> : null}
          {data.isError ? (
            <Alert variant="destructive">
              <AlertDescription className="text-[14px] leading-5">{describeError(data.error)}</AlertDescription>
            </Alert>
          ) : null}
          {data.data ? (
            <p className="num text-[12px] leading-4 text-ink-2">
              {data.data.rows.length} samples · {data.data.res === "1s" ? "1 s samples" : "1 min rollups"} · {from.slice(0, 16).replace("T", " ")} – {to.slice(0, 16).replace("T", " ")} UTC
            </p>
          ) : wanted.length > 0 && data.isPending ? (
            <p className="text-[12px] leading-4 text-ink-2">Loading…</p>
          ) : null}
          {wanted.map((k) => {
            const m = HISTORY_METRICS.find((x) => x.key === k)!;
            const points = data.data ? seriesOf(data.data.columns, data.data.rows, k) : [];
            return (
              <Panel key={k}>
                {data.data ? <TimeSeries points={points} label={m.label} unit={m.unit} format={m.format} color={m.color} domain={[fromMs / 1000, toMs / 1000]} gapS={gapS} /> : <p className="text-ink-2">{m.label}: loading…</p>}
              </Panel>
            );
          })}
        </div>
      </div>
    </>
  );
}
