import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageHeader } from "@/app/PageHeader";
import { CorrAgeGauge } from "@/components/CorrAgeGauge";
import { EmptyState } from "@/components/EmptyState";
import { FixTimeline } from "@/components/FixTimeline";
import { NtripStatus } from "@/components/NtripStatus";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ROUTES, describeError, put, route } from "@/lib/api";
import { DASH, fmtMeters, fmtUtc } from "@/lib/format";
import { useLive, useStale } from "@/lib/live";
import { STATUS_TEXT, type StatusLevel } from "@/lib/palette";
import { useRover } from "@/lib/queries";
import { bearingToBase, fixLevel } from "@/lib/status";
import type { NtripClientStatus, RoverOutputs, TimeMark } from "@/lib/types";
import { cn } from "@/lib/utils";

const RTCM_NAMES: Record<string, string> = {
  "1005": "Base position",
  "1006": "Base position + height",
  "1033": "Antenna / receiver",
  "1074": "GPS MSM4",
  "1077": "GPS MSM7",
  "1084": "GLONASS MSM4",
  "1087": "GLONASS MSM7",
  "1094": "Galileo MSM4",
  "1097": "Galileo MSM7",
  "1124": "BeiDou MSM4",
  "1127": "BeiDou MSM7",
  "1230": "GLONASS biases",
  "4072": "u-blox reference station",
};
/** The fix-state strip's window, re-anchored to "now" this often (each move is one refetch). */
const WINDOW_MS = 600_000;
const WINDOW_TICK_MS = 30_000;

/** "16:47:35.250000": the UTC second plus the microseconds the daemon reported. */
function fmtMarkUtc(iso: string | null): string {
  if (!iso) return DASH;
  const whole = fmtUtc(iso);
  if (whole === DASH) return DASH;
  const frac = /T\d{2}:\d{2}:\d{2}\.(\d+)/.exec(iso)?.[1] ?? "";
  return `${whole}.${frac.padEnd(6, "0").slice(0, 6)}`;
}

/**
 * "Now" on the receiver's clock when it has a valid time, else the browser's. The history's
 * timestamps are receiver UTC, and a field tablet with no NTP can be minutes off.
 */
function receiverNow(): number {
  const t = useLive.getState().state?.time;
  const ms = t?.utc && t.valid_date && t.valid_time ? Date.parse(t.utc) : NaN;
  return Number.isFinite(ms) ? ms : Date.now();
}

function useWindow(): [string, string] {
  const [end, setEnd] = useState(receiverNow);
  useEffect(() => {
    const id = setInterval(() => setEnd(receiverNow()), WINDOW_TICK_MS);
    return () => clearInterval(id);
  }, []);
  return [new Date(end - WINDOW_MS).toISOString(), new Date(end).toISOString()];
}

/**
 * `configuredUrl` is undefined until `GET /api/rover` has answered; the form stays shut until
 * then, because a URL rebuilt from the status carries no credentials and saving it would drop
 * the stored ones (only a `***` password is kept server-side).
 */
function NtripPanel({ ntrip, configuredUrl }: { ntrip: NtripClientStatus | null; configuredUrl: string | null | undefined }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [url, setUrl] = useState("");
  const setNtrip = useMutation({
    mutationFn: (u: string) => put(route(ROUTES.putRoverNtrip), { url: u }),
    onSuccess: () => {
      toast.success("Connecting to the new caster");
      setEditing(false);
      void qc.invalidateQueries({ queryKey: ["rover"], exact: true });
    },
  });
  const open = () => {
    setNtrip.reset();
    setEditing((e) => !e);
    // The configured URL comes back with its password as ***; posting it unchanged keeps it.
    setUrl(configuredUrl ?? "");
  };
  return (
    <Panel
      className="col-span-12 lg:col-span-4"
      title="NTRIP client"
      actions={
        <Button size="sm" variant="outline" onClick={open} aria-expanded={editing} disabled={configuredUrl === undefined} title={configuredUrl === undefined ? "Waiting for the rover's configuration" : undefined}>
          Change caster
        </Button>
      }
    >
      {editing ? (
        <form
          className="mb-3 flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setNtrip.mutate(url.trim());
          }}
        >
          <Label htmlFor="ntrip-url">NTRIP URL</Label>
          <Input id="ntrip-url" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="ntrip://user:password@base:2101/MTRK" autoComplete="off" spellCheck={false} />
          <p className="text-[12px] leading-4 text-ink-2">Saved to .env and applied now. Leave *** as the password to keep the stored one.</p>
          {setNtrip.isError ? (
            <p role="alert" className="text-status-critical-text">
              {describeError(setNtrip.error)}
            </p>
          ) : null}
          <div className="flex gap-2">
            <Button type="submit" disabled={!url.trim() || setNtrip.isPending}>
              Connect
            </Button>
            <Button type="button" variant="outline" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </form>
      ) : null}
      <NtripStatus ntrip={ntrip} detailed />
    </Panel>
  );
}

function OutputsLine({ outputs }: { outputs: RoverOutputs }) {
  const tcp = outputs.nmea_tcp;
  return (
    <p className="text-ink-2">
      NMEA over TCP {tcp ? `on port ${tcp.port} (${tcp.clients} client${tcp.clients === 1 ? "" : "s"})` : "off"} · UDP {outputs.nmea_udp.length ? outputs.nmea_udp.join(", ") : "off"} · serial {outputs.nmea_serial ?? "off"} · JSON UDP {outputs.json_udp ?? "off"} · sentences {outputs.sentences.join(", ") || "none"}.
    </p>
  );
}

function TimeMarks({ marks }: { marks: TimeMark[] }) {
  if (marks.length === 0) return <p className="p-2 text-ink-2">No pulses on EXTINT yet.</p>;
  return (
    <table aria-label="Time marks" className="w-full text-[14px]">
      <thead>
        <tr className="border-b border-line text-left text-ink-2">
          <th className="py-1.5 pr-3 font-medium">#</th>
          <th className="py-1.5 pr-3 font-medium">UTC (rising)</th>
          <th className="py-1.5 pr-3 text-right font-medium">TOW (s)</th>
        </tr>
      </thead>
      <tbody>
        {marks.slice(0, 20).map((m) => (
          <tr key={`${m.channel}-${m.count}-${m.rising_tow_s}`} className="border-b border-line/60 last:border-0">
            <td className="num py-1.5 pr-3">{m.count}</td>
            <td className="num py-1.5 pr-3">{fmtMarkUtc(m.rising_utc)}</td>
            <td className="num py-1.5 pr-3 text-right">{m.rising_tow_s?.toFixed(6) ?? DASH}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The rover's RTK page: the carrier solution and the baseline to the base, the correction age,
 * the NTRIP client (with a form to switch casters), the RTCM the receiver reports receiving, a
 * strip of the last ten minutes' fix state, camera time marks and the configured outputs.
 */
export default function Rtk() {
  const role = useLive((s) => s.role);
  const state = useLive((s) => s.state);
  const liveNtrip = useLive((s) => s.ntripClient);
  const receiverConnected = useLive((s) => s.receiverConnected);
  const timeMarks = useLive((s) => s.timeMarks);
  const stale = useStale();
  const isRover = role === "rover";
  const rover = useRover(isRover);
  const [from, to] = useWindow();

  if (role != null && !isRover) {
    return (
      <>
        <PageHeader title="RTK" />
        <EmptyState title="This daemon runs as a base station" body="The RTK page shows a rover's solution. Set ROLE=rover in Settings to use it." />
      </>
    );
  }
  if (!state) {
    return (
      <>
        <PageHeader title="RTK" />
        <EmptyState title="Waiting for the receiver" />
      </>
    );
  }
  const rtk = state.rtk;
  const ntrip = liveNtrip ?? rover.data?.ntrip ?? null;
  const fix = fixLevel(state.fix, receiverConnected, stale);
  const types = Object.keys(rtk.rtcm_rx).sort((a, b) => Number(a) - Number(b));
  const carrLevel: StatusLevel = rtk.carr_soln === 2 ? "good" : rtk.carr_soln === 1 ? "warning" : "serious";
  const driver = rover.data?.driver;
  const rejectsRtcm = driver?.capabilities.accepts_rtcm === false;
  // "Unverified" is about corrections the unit is sent: with no caster configured and no client running, none are.
  const sendsRtcm = Boolean(rover.data?.ntrip_url) || ntrip != null;
  return (
    <>
      <PageHeader title="RTK">
        <StatusBadge level={fix.level} label={fix.label} />
      </PageHeader>
      <div data-stale={stale} className={cn("grid grid-cols-12 gap-4", stale && "[&_.num]:text-ink-3")}>
        {rejectsRtcm || (driver?.rtcm_unverified && sendsRtcm) ? (
          <Panel className="col-span-12" title="Corrections path">
            {rejectsRtcm ? (
              <p role="status" aria-label="RTCM notice" style={{ color: STATUS_TEXT.serious }}>
                {driver?.name === "vectornav" ? "This receiver does not accept RTCM corrections (VN-200)" : "This receiver does not accept RTCM corrections"}
                <span className="text-ink-2"> · the NTRIP client is not started. Set INS_VN_RTCM=1 to forward corrections to a VectorNav unit anyway.</span>
              </p>
            ) : (
              <p role="status" aria-label="RTCM notice" style={{ color: STATUS_TEXT.warning }}>
                RTCM path unverified on this unit
                <span className="text-ink-2"> · the unit has not yet echoed any corrections or reported an RTK solution, so whether it uses the RTCM it is sent is not confirmed.</span>
              </p>
            )}
          </Panel>
        ) : null}
        <Panel className="col-span-12 lg:col-span-4" title="Solution">
          <Stat label="Carrier solution" value={rtk.carr_soln_name} level={carrLevel} />
          <Stat label="Baseline" value={fmtMeters(rtk.baseline_m, 2)} />
          <Stat label="Baseline N / E / D" value={`${fmtMeters(rtk.rel_pos_n_m, 3)} / ${fmtMeters(rtk.rel_pos_e_m, 3)} / ${fmtMeters(rtk.rel_pos_d_m, 3)}`} />
          <Stat label="Bearing to base" value={rtk.heading_valid && rtk.heading_deg != null ? `${bearingToBase(rtk.heading_deg).toFixed(2)}°` : DASH} />
          <Stat label="Baseline accuracy" value={fmtMeters(rtk.acc_length_m, 3)} />
          <Stat label="Reference station" value={rtk.ref_station_id == null ? DASH : String(rtk.ref_station_id)} />
          <div className="mt-3">
            <CorrAgeGauge age={rtk.corr_age_s} />
          </div>
          {rtk.corr_age_receiver_s != null ? <p className="mt-1 text-[12px] leading-4 text-ink-2">Receiver reports corrections ≤ {rtk.corr_age_receiver_s} s old</p> : null}
          {rtk.ref_obs_missing || rtk.ref_pos_missing ? (
            <p className="mt-1 text-[12px] leading-4 text-status-warning-text">
              Missing from the base: {[rtk.ref_pos_missing ? "position (1005)" : null, rtk.ref_obs_missing ? "observations (MSM)" : null].filter(Boolean).join(", ")}
            </p>
          ) : null}
        </Panel>
        <NtripPanel ntrip={ntrip} configuredUrl={rover.data?.ntrip_url} />
        <Panel className="col-span-12 lg:col-span-4" title="Corrections received" bodyClassName="p-2">
          {types.length === 0 ? (
            <p className="p-2 text-ink-2">No RTCM messages seen by the receiver yet.</p>
          ) : (
            <table aria-label="RTCM received" className="w-full text-[14px]">
              <thead>
                <tr className="border-b border-line text-left text-ink-2">
                  <th className="py-1.5 pr-3 font-medium">Type</th>
                  <th className="py-1.5 pr-3 font-medium">Content</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Count</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Used</th>
                  <th className="py-1.5 pr-3 text-right font-medium">Bad CRC</th>
                </tr>
              </thead>
              <tbody>
                {types.map((t) => {
                  const s = rtk.rtcm_rx[t];
                  return (
                    <tr key={t} className="border-b border-line/60 last:border-0">
                      <td className="num py-1.5 pr-3">{t}</td>
                      <td className="py-1.5 pr-3 text-ink-2">{RTCM_NAMES[t] ?? ""}</td>
                      <td className="num py-1.5 pr-3 text-right">{s.count}</td>
                      <td className="num py-1.5 pr-3 text-right">{s.used}</td>
                      <td className="num py-1.5 pr-3 text-right">{s.crc_failed}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel className="col-span-12 lg:col-span-8" title="Fix state, last 10 minutes">
          <FixTimeline from={from} to={to} />
        </Panel>
        <Panel className="col-span-12 lg:col-span-4" title="Camera time marks" bodyClassName="p-2">
          <TimeMarks marks={timeMarks} />
        </Panel>
        {state.attitude ? (
          <Panel className="col-span-12 lg:col-span-4" title={`Attitude (${state.attitude.source || "receiver"})`}>
            <Stat label="Heading" value={state.attitude.heading_deg == null ? DASH : `${state.attitude.heading_deg.toFixed(2)}°`} />
            <Stat label="Roll / pitch" value={`${state.attitude.roll_deg?.toFixed(2) ?? DASH}° / ${state.attitude.pitch_deg?.toFixed(2) ?? DASH}°`} />
          </Panel>
        ) : null}
        {rover.data ? (
          <Panel className="col-span-12" title="Outputs">
            <OutputsLine outputs={rover.data.outputs} />
          </Panel>
        ) : null}
      </div>
    </>
  );
}
