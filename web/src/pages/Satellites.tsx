import { useMemo } from "react";
import { PageHeader } from "@/app/PageHeader";
import { EmptyState } from "@/components/EmptyState";
import { Panel } from "@/components/Panel";
import { DataTable, type Column } from "@/components/DataTable";
import { CnoBars } from "@/components/charts/CnoBars";
import { SkyPlot, satId } from "@/components/charts/SkyPlot";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useLive, useStale } from "@/lib/live";
import { SYSTEM_ORDER, systemColor } from "@/lib/palette";
import { usePref } from "@/lib/prefs";
import type { Satellite } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Preference keys (under `mtrtk:`): the systems the operator hid, and whether only used satellites show. */
export const SAT_SYSTEMS_HIDDEN_PREF = "satSystemsHidden";
export const SAT_USED_ONLY_PREF = "satUsedOnly";

/** UBX NAV-SAT qualityInd. */
const QUALITY = ["No signal", "Searching", "Acquired", "Unusable", "Code locked", "Code+carrier", "Code+carrier", "Code+carrier"];
/** UBX NAV-SAT health: 0 unknown, 1 healthy, 2 unhealthy. */
const HEALTH = ["unknown", "healthy", "unhealthy"];

const systemRank = (name: string) => {
  const i = (SYSTEM_ORDER as readonly string[]).indexOf(name);
  return i === -1 ? SYSTEM_ORDER.length : i;
};
/** u-blox reports −91° for an unknown elevation. */
const angle = (v: number | null) => (v == null || v < -90 ? "—" : `${Math.round(v)}°`);

const columns: Column<Satellite>[] = [
  { key: "sat", header: "Satellite", cell: (s) => satId(s), sortValue: (s) => systemRank(s.gnss) * 1000 + s.sv_id, align: "right", firstDir: "asc" },
  {
    key: "system",
    header: "System",
    cell: (s) => (
      <span className="flex items-center gap-2">
        <span className="inline-block size-2.5 shrink-0 rounded-sm" style={{ background: systemColor(s.gnss) }} aria-hidden />
        {s.gnss}
      </span>
    ),
    sortValue: (s) => systemRank(s.gnss) * 1000 + s.sv_id,
  },
  { key: "sv", header: "SV", cell: (s) => String(s.sv_id), sortValue: (s) => s.sv_id, align: "right" },
  { key: "cno", header: "C/N0", cell: (s) => (s.cno > 0 ? `${Math.round(s.cno)} dB-Hz` : "—"), sortValue: (s) => s.cno, align: "right" },
  { key: "elev", header: "Elevation", cell: (s) => angle(s.elev), sortValue: (s) => (s.elev == null || s.elev < -90 ? null : s.elev), align: "right" },
  { key: "az", header: "Azimuth", cell: (s) => angle(s.azim), sortValue: (s) => (s.azim == null || s.elev == null || s.elev < -90 ? null : s.azim), align: "right" },
  { key: "used", header: "Used", cell: (s) => (s.used ? "yes" : <span className="text-ink-3">no</span>), sortValue: (s) => s.used, firstDir: "desc" },
  { key: "health", header: "Health", cell: (s) => HEALTH[s.health] ?? String(s.health), sortValue: (s) => s.health },
  { key: "quality", header: "Quality", cell: (s) => QUALITY[s.quality_ind] ?? String(s.quality_ind), sortValue: (s) => s.quality_ind },
  { key: "res", header: "PR residual", cell: (s) => `${s.pr_res_m.toFixed(1)} m`, sortValue: (s) => Math.abs(s.pr_res_m), align: "right" },
  {
    key: "signals",
    header: "Signals",
    cell: (s) => (s.signals.length ? s.signals.map((g) => `${g.name} ${g.cno > 0 ? Math.round(g.cno) : "—"}`).join(" · ") : "—"),
    sortValue: (s) => s.signals.length,
  },
  { key: "eph", header: "Eph / Alm", cell: (s) => `${s.eph_avail ? "E" : "–"} ${s.alm_avail ? "A" : "–"}`, sortValue: (s) => Number(s.eph_avail) * 2 + Number(s.alm_avail) },
];

const chip = "flex items-center gap-2 rounded-full border px-3 py-1 text-[14px] leading-5";

/**
 * Every satellite the receiver tracks: the sky plot at full size, a C/N0 bar per signal, and a
 * sortable table. System chips and a "Used only" toggle filter all three views; both are kept
 * per browser. The header carries the used/tracked count from the receiver's own summary and
 * the number of rows the filters leave.
 */
export default function Satellites() {
  const state = useLive((s) => s.state);
  const stale = useStale();
  const [hidden, setHidden] = usePref<string[]>(SAT_SYSTEMS_HIDDEN_PREF, []);
  const [usedOnly, setUsedOnly] = usePref<boolean>(SAT_USED_ONLY_PREF, false);

  const systems = useMemo(() => {
    if (!state) return [];
    const names = new Set<string>([...Object.keys(state.sat_summary.per_gnss), ...state.sats.map((s) => s.gnss)]);
    return [...names].sort((a, b) => systemRank(a) - systemRank(b) || a.localeCompare(b));
  }, [state]);

  const visible = useMemo(
    () => (state ? state.sats.filter((s) => !hidden.includes(s.gnss) && (!usedOnly || s.used)) : []),
    [state, hidden, usedOnly],
  );

  if (!state) {
    return (
      <>
        <PageHeader title="Satellites" />
        <EmptyState title="Waiting for the receiver" body="Satellites are listed as soon as the daemon reports an epoch." />
      </>
    );
  }
  if (state.sats.length === 0) {
    return (
      <>
        <PageHeader title="Satellites" />
        <EmptyState title="No satellites tracked yet" body="The receiver has not acquired a signal. Check the antenna on the Receiver page if this persists." />
      </>
    );
  }

  const { used, tracked } = state.sat_summary;
  const filtered = visible.length !== state.sats.length;
  const toggleSystem = (name: string) => setHidden(hidden.includes(name) ? hidden.filter((h) => h !== name) : [...hidden, name]);
  const showAll = () => {
    setHidden([]);
    setUsedOnly(false);
  };
  const nothingLeft = <EmptyState title="No satellites match the filters" action={<button type="button" onClick={showAll} className="rounded-md border border-line px-3 py-1 text-ink hover:bg-panel-2">Show all</button>} />;

  return (
    <>
      <PageHeader title="Satellites">
        <span className="num text-ink-2">{used} used of {tracked} tracked</span>
        {filtered ? <span className="num text-ink-3">{visible.length} shown</span> : null}
      </PageHeader>
      <div data-testid="satellites-body" data-stale={stale} className={cn("flex flex-col gap-3", stale && "[&_.num]:text-ink-3")}>
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
          {systems.map((name) => {
            const on = !hidden.includes(name);
            const counts = state.sat_summary.per_gnss[name];
            return (
              <button key={name} type="button" aria-pressed={on} onClick={() => toggleSystem(name)} className={cn(chip, on ? "border-line bg-panel text-ink" : "border-dashed border-line/70 text-ink-3")}>
                <span className="inline-block size-2.5 shrink-0 rounded-sm" style={{ background: on ? systemColor(name) : "var(--ink-3)" }} aria-hidden />
                {name}
                {counts ? (
                  <span className={cn("num", on ? "text-ink-2" : "text-ink-3")}>
                    {counts.used}/{counts.tracked}
                  </span>
                ) : null}
              </button>
            );
          })}
          <button type="button" aria-pressed={usedOnly} onClick={() => setUsedOnly(!usedOnly)} className={cn(chip, "ml-auto", usedOnly ? "border-brass text-ink" : "border-line text-ink-2 hover:text-ink")}>
            Used only
          </button>
        </div>

        <Tabs defaultValue="sky">
          <TabsList variant="line" aria-label="Satellite views" className="border-b border-line">
            {[
              ["sky", "Sky"],
              ["signals", "Signals"],
              ["table", "Table"],
            ].map(([value, label]) => (
              <TabsTrigger key={value} value={value} className="flex-none px-3 text-[14px] text-ink-2 hover:text-ink data-[state=active]:text-ink data-[state=active]:after:bg-brass">
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
          <TabsContent value="sky">
            <Panel>{visible.length ? <SkyPlot sats={visible} size={480} /> : nothingLeft}</Panel>
          </TabsContent>
          <TabsContent value="signals">
            <Panel>{visible.length ? <CnoBars sats={visible} height={240} /> : nothingLeft}</Panel>
          </TabsContent>
          <TabsContent value="table">
            <Panel bodyClassName="p-2">
              <DataTable columns={columns} rows={visible} rowKey={(s) => `${s.gnss}-${s.sv_id}`} initialSort={{ key: "sat", dir: "asc" }} dense empty={nothingLeft} />
            </Panel>
          </TabsContent>
        </Tabs>
      </div>
    </>
  );
}
