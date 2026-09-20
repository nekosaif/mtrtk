import { useEffect, useState } from "react";
import { PageHeader } from "@/app/PageHeader";
import { CopyButton } from "@/components/CopyButton";
import { DataTable, type Column } from "@/components/DataTable";
import { EmptyState } from "@/components/EmptyState";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { Sparkline } from "@/components/charts/Sparkline";
import { type CoordMode, DASH, fmtBytes, fmtCoord, fmtDuration, fmtLocal, fmtRate, fmtUtcDate, parseUtc, relTime } from "@/lib/format";
import { type BaseInfo, useLive, useStale } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useCoordMode } from "@/lib/prefs";
import { useBaseMode, useNtrip, useNtripClients, useNtripHistory } from "@/lib/queries";
import { type Rates, useMessageRates, useRing, WINDOW_S } from "@/lib/rates";
import type { BaseModeView, NtripClient, NtripHistoryRecord, NtripInfo, RtcmMsgStats } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Seconds of bitrate history kept for the sparkline. */
export const BITRATE_RING_S = 300;
/** Past connections asked of `GET /api/ntrip/history`. */
export const HISTORY_LIMIT = 100;

/** What each RTCM 3 message number carries (the ones the daemon's profile can emit). */
export const RTCM_NAMES: Record<string, string> = {
  "1005": "Station ARP (base position)",
  "1006": "Station ARP + antenna height",
  "1074": "GPS MSM4",
  "1077": "GPS MSM7",
  "1084": "GLONASS MSM4",
  "1087": "GLONASS MSM7",
  "1094": "Galileo MSM4",
  "1097": "Galileo MSM7",
  "1124": "BeiDou MSM4",
  "1127": "BeiDou MSM7",
  "1230": "GLONASS code-phase biases",
  "4072": "u-blox proprietary",
};

/** A stable empty map, so a page without a state does not resample every render. */
const NO_MESSAGES: Record<string, RtcmMsgStats> = {};

/**
 * `last_seen_mono` is the receiver host's monotonic clock in seconds, not a timestamp: it can
 * only be read against another value of the same clock. `ageOf` gives the gap to the newest one.
 */
export function fmtAge(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return DASH;
  const s = Math.max(0, seconds);
  if (s < 0.05) return "latest";
  if (s < 60) return `${s.toFixed(1)} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

function newestMono(messages: Record<string, RtcmMsgStats>): number | null {
  let max: number | null = null;
  for (const m of Object.values(messages)) if (m.last_seen_mono != null && (max == null || m.last_seen_mono > max)) max = m.last_seen_mono;
  return max;
}

/** A once-a-second wall clock for durations that should tick. */
function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

const durationBetween = (fromIso: string | null | undefined, toMs: number): string => {
  const from = parseUtc(fromIso);
  return from ? fmtDuration((toMs - from.getTime()) / 1000) : DASH;
};

/** A UTC readout with the same instant in the browser's zone as its tooltip. */
function UtcTime({ iso }: { iso: string | null | undefined }) {
  return (
    <span className="num" title={iso ? `${fmtLocal(iso)} local` : undefined}>
      {fmtUtcDate(iso)}
    </span>
  );
}

// ------------------------------------------------------------------------------ 1005 check

interface SiteCheckView {
  level: StatusLevel;
  label: string;
  offset: string | null;
}

/**
 * What the 1005 row says about the active site. The socket's `base` slice is authoritative once
 * it has spoken (`site_verified` / `site_mismatch`); the query covers the first view, where the
 * slice is still empty. Nothing is said unless the receiver is on a fixed site: a survey-in base
 * has no site to check against.
 */
export function siteCheck(live: BaseInfo, view: BaseModeView | undefined): SiteCheckView | null {
  if (view && !view.available) return null;
  const mode = live.mode ?? view?.mode ?? null;
  if (mode !== "fixed" && live.verified == null) return null;
  const site = live.site ?? view?.site ?? null;
  const suffix = site ? ` · ${site}` : "";
  if (live.verified === true || (live.verified == null && view?.verified)) return { level: "good", label: `Site verified${suffix}`, offset: null };
  if (live.verified === false) {
    const m = live.mismatch;
    const offset = m?.dx != null && m.dy != null && m.dz != null ? `${m.dx.toFixed(3)}, ${m.dy.toFixed(3)}, ${m.dz.toFixed(3)} m` : m?.reason ?? null;
    return { level: "critical", label: `Site mismatch${suffix}`, offset };
  }
  return { level: "warning", label: `Not yet verified${suffix}`, offset: null };
}

// ------------------------------------------------------------------------------- columns

interface RtcmRow {
  type: string;
  count: number;
  bytes: number;
  lastSeenMono: number | null;
  /** True when the daemon's map no longer lists the type (the tracker remembers it). */
  gone: boolean;
}

function rtcmColumns(rates: Rates, newest: number | null, check: SiteCheckView | null): Column<RtcmRow>[] {
  const rateOf = (r: RtcmRow) => rates[r.type];
  return [
    { key: "type", header: "Type", cell: (r) => r.type, sortValue: (r) => Number(r.type) },
    {
      key: "what",
      header: "Content",
      cell: (r) => {
        const name = RTCM_NAMES[r.type] ?? "RTCM message";
        if (r.type !== "1005" || !check) return <span className="text-ink-2">{name}</span>;
        return (
          <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="text-ink-2">{name}</span>
            <StatusBadge level={check.level} label={check.label} className="text-[12px]" />
            {check.offset ? (
              <span className="num text-[12px] leading-4 text-ink-2" title="ΔX, ΔY, ΔZ of the 1005 against the site">
                {check.offset}
              </span>
            ) : null}
          </span>
        );
      },
      sortValue: (r) => RTCM_NAMES[r.type] ?? "RTCM message",
    },
    { key: "count", header: "Count", cell: (r) => String(r.count), sortValue: (r) => r.count, align: "right" },
    { key: "hz", header: "Rate", cell: (r) => (rateOf(r) ? `${rateOf(r).hz.toFixed(2)} Hz` : DASH), sortValue: (r) => rateOf(r)?.hz ?? null, align: "right" },
    { key: "bps", header: "Bytes/s", cell: (r) => (rateOf(r) ? fmtRate(rateOf(r).bytesPerS) : DASH), sortValue: (r) => rateOf(r)?.bytesPerS ?? null, align: "right" },
    {
      key: "seen",
      header: "Last seen",
      cell: (r) => (r.gone ? "no longer listed" : fmtAge(newest != null && r.lastSeenMono != null ? newest - r.lastSeenMono : null)),
      sortValue: (r) => (r.gone ? null : r.lastSeenMono),
      align: "right",
      firstDir: "desc",
    },
  ];
}

function clientColumns(now: number, mode: CoordMode): Column<NtripClient>[] {
  return [
    { key: "addr", header: "Address", cell: (c) => `${c.ip}:${c.port}` },
    { key: "ua", header: "Client", cell: (c) => c.user_agent || DASH },
    { key: "v", header: "NTRIP", cell: (c) => `v${c.version}` },
    { key: "user", header: "User", cell: (c) => c.username ?? "anonymous" },
    { key: "for", header: "Connected for", cell: (c) => durationBetween(c.connected_utc, now), sortValue: (c) => c.connected_utc, align: "right", firstDir: "asc" },
    { key: "sent", header: "Sent", cell: (c) => fmtBytes(c.bytes_sent), sortValue: (c) => c.bytes_sent, align: "right" },
    { key: "dropped", header: "Dropped frames", cell: (c) => String(c.dropped_frames), sortValue: (c) => c.dropped_frames, align: "right" },
    {
      key: "gga",
      header: "Last GGA",
      cell: (c) => {
        if (c.last_gga_lat == null || c.last_gga_lon == null) return DASH;
        const [a, b] = fmtCoord(c.last_gga_lat, c.last_gga_lon, mode);
        return (
          <span className="num inline-flex flex-col leading-5">
            <span>{a}</span>
            <span>{b}</span>
            {c.last_gga_utc ? <span className="text-[12px] leading-4 text-ink-2">{relTime(c.last_gga_utc, now)}</span> : null}
          </span>
        );
      },
      sortValue: (c) => c.last_gga_utc,
    },
  ];
}

function historyColumns(now: number): Column<NtripHistoryRecord>[] {
  return [
    { key: "ip", header: "Address", cell: (r) => r.ip ?? DASH },
    { key: "ua", header: "Client", cell: (r) => r.user_agent || DASH },
    { key: "from", header: "Connected", cell: (r) => <UtcTime iso={r.connected_utc} />, sortValue: (r) => r.connected_utc, firstDir: "desc" },
    { key: "to", header: "Disconnected", cell: (r) => (r.disconnected_utc ? <UtcTime iso={r.disconnected_utc} /> : <span className="text-ink-2">still connected</span>), sortValue: (r) => r.disconnected_utc },
    {
      key: "for",
      header: "Duration",
      cell: (r) => durationBetween(r.connected_utc, parseUtc(r.disconnected_utc)?.getTime() ?? now),
      sortValue: (r) => {
        const from = parseUtc(r.connected_utc);
        return from ? (parseUtc(r.disconnected_utc)?.getTime() ?? now) - from.getTime() : null;
      },
      align: "right",
    },
    { key: "sent", header: "Sent", cell: (r) => fmtBytes(r.bytes_sent), sortValue: (r) => r.bytes_sent, align: "right" },
    { key: "why", header: "Reason", cell: (r) => <span className="text-ink-2">{r.reason || DASH}</span>, sortValue: (r) => r.reason },
  ];
}

// -------------------------------------------------------------------------------- panels

function CasterPanel({ info, pending }: { info: NtripInfo | undefined; pending: boolean }) {
  if (!info) {
    return <p className="text-ink-2">{pending ? "Loading caster status…" : "The caster status could not be read."}</p>;
  }
  const where = info.host && info.port ? `${info.host}:${info.port}` : null;
  if (!info.running) {
    return (
      <EmptyState
        title="NTRIP caster is off"
        body={`This daemon runs without a caster (a replay source or the rover role), so nothing listens for rovers.${where ? ` Configured: ${info.bind_mode} ${where} /${info.mountpoint}.` : ""}`}
      />
    );
  }
  return (
    <>
      <div className="mb-3 flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <code className="num min-w-0 flex-1 overflow-x-auto rounded-md bg-panel-2 px-2 py-1 text-[13px] leading-5 whitespace-nowrap">{info.connection_url}</code>
          <CopyButton text={info.connection_url} label="Copy" />
        </div>
        {info.anonymous ? null : <p className="text-[12px] leading-4 text-ink-2">Password hidden: replace *** with the NTRIP password after copying.</p>}
      </div>
      <Stat label="Listening on" value={where ?? DASH} />
      <Stat label="Bind mode" value={info.bind_mode || DASH} />
      <Stat label="Mountpoint" value={`/${info.mountpoint}`} />
      <Stat label="Authentication" value={info.anonymous ? "anonymous" : `user ${info.username ?? DASH}`} />
      <Stat label="Clients" value={`${info.clients ?? 0} / ${info.max_clients}`} hint="connected / limit" />
      <Stat label="Rejected" value={String(info.rejected ?? 0)} title="Callers turned away because the caster was at max_clients" />
      {info.sourcetable ? (
        <details className="mt-3 text-[12px] leading-4">
          <summary className="cursor-pointer text-ink-2 hover:text-ink">Sourcetable</summary>
          <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-panel-2 p-2 font-mono whitespace-pre-wrap break-all text-ink-2">{info.sourcetable}</pre>
        </details>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------------- page

/**
 * The correction stream and who receives it: every RTCM type with its rate and age, the bitrate
 * and its last five minutes, the NTRIP caster's connection details, the rovers connected right
 * now and the recent connections. Figures grey when no epoch has arrived for 5 s or the socket
 * is down.
 */
export default function Corrections() {
  const state = useLive((s) => s.state);
  const liveClients = useLive((s) => s.ntripClients);
  const liveBase = useLive((s) => s.base);
  const stale = useStale();
  const now = useNow();
  const [coordMode] = useCoordMode();
  const ntrip = useNtrip();
  const clientsQuery = useNtripClients();
  const history = useNtripHistory(HISTORY_LIMIT);
  const baseMode = useBaseMode();
  const messages = state?.rtcm_out.messages ?? NO_MESSAGES;
  const rates = useMessageRates(messages);
  const ring = useRing(state ? state.rtcm_out.bytes_per_s : null, BITRATE_RING_S);

  if (!state) {
    return (
      <>
        <PageHeader title="Corrections" />
        <EmptyState title="Waiting for the receiver" body="The RTCM stream and the caster's clients appear as soon as the daemon reports a snapshot." />
      </>
    );
  }

  // The socket lists clients on every change; until it has, the query is the only source.
  const clients = liveClients.length ? liveClients : Array.isArray(clientsQuery.data) ? clientsQuery.data : [];
  const flowing = state.rtcm_out.bytes_per_s > 0;
  const roverWord = `${clients.length} rover${clients.length === 1 ? "" : "s"} connected`;
  const newest = newestMono(messages);
  // Types the daemon lists now, plus any the tracker still remembers (ruling: they stay listed).
  const rows: RtcmRow[] = Object.entries(messages).map(([type, m]) => ({ type, count: m.count, bytes: m.bytes, lastSeenMono: m.last_seen_mono, gone: false }));
  for (const [type, r] of Object.entries(rates)) if (!(type in messages)) rows.push({ type, count: r.count, bytes: r.bytes, lastSeenMono: null, gone: true });
  rows.sort((a, b) => Number(a.type) - Number(b.type));
  const check = siteCheck(liveBase, baseMode.data);
  const spanS = ring.length > 1 ? (ring[ring.length - 1].t - ring[0].t) / 1000 : 0;
  const bitrateLabel = spanS >= BITRATE_RING_S - 5 ? "Bitrate, last 5 min" : `Bitrate, last 5 min (${fmtDuration(spanS)} so far)`;
  const historyRows = Array.isArray(history.data) ? history.data : [];

  return (
    <>
      <PageHeader title="Corrections">
        <StatusBadge level={flowing ? "good" : "serious"} label={flowing ? `Corrections flowing · ${roverWord}` : "No RTCM output"} />
      </PageHeader>
      <div data-testid="corrections-grid" data-stale={stale} className={cn("grid grid-cols-12 gap-4", stale && "[&_.num]:text-ink-3")}>
        <Panel className="col-span-12 lg:col-span-7" title="RTCM 3 output" bodyClassName="p-2">
          <DataTable
            columns={rtcmColumns(rates, newest, check)}
            rows={rows}
            rowKey={(r) => r.type}
            dense
            empty={
              <EmptyState
                title="No RTCM messages yet"
                body="The receiver starts sending observations once its position is known: a valid survey-in or an active fixed site."
              />
            }
          />
          {rows.length ? (
            <p className="px-2 pt-2 text-[12px] leading-4 text-ink-3">
              Rates are averaged over the last {WINDOW_S} s of epochs. Last seen is the receiver host's own clock, read against the newest message.
            </p>
          ) : null}
        </Panel>

        <Panel className="col-span-12 lg:col-span-5" title="Stream">
          <Stat label="Bitrate" value={fmtRate(state.rtcm_out.bytes_per_s)} />
          <Stat label="Total output" value={fmtBytes(state.rtcm_out.total_bytes)} />
          <Stat label="Messages" value={String(state.rtcm_out.total_count)} />
          <div className="mt-3">
            <Sparkline label={bitrateLabel} values={ring.map((p) => p.v)} format={fmtRate} />
          </div>
        </Panel>

        <Panel className="col-span-12 lg:col-span-4" title="NTRIP caster">
          <CasterPanel info={ntrip.data} pending={ntrip.isPending} />
        </Panel>

        <Panel className="col-span-12 lg:col-span-8" title={`Connected rovers (${clients.length})`} bodyClassName="p-2">
          <DataTable
            columns={clientColumns(now, coordMode)}
            rows={clients}
            rowKey={(c) => String(c.id)}
            dense
            empty={<EmptyState title="No rovers connected" body="Point an NTRIP client at the caster's connection URL; it appears here as soon as it logs in." />}
          />
        </Panel>

        <Panel className="col-span-12" title="Recent connections" bodyClassName="p-2">
          <DataTable
            columns={historyColumns(now)}
            rows={historyRows}
            rowKey={(r) => String(r.id)}
            dense
            empty={
              history.isPending ? (
                <span className="text-ink-3">Loading…</span>
              ) : (
                <EmptyState title="No connections recorded yet" body="Every rover that logs in is recorded here, including ones from earlier runs of the daemon." />
              )
            }
          />
        </Panel>
      </div>
    </>
  );
}
