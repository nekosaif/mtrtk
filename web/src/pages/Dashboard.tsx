import { Link } from "react-router";
import { PageHeader } from "@/app/PageHeader";
import { CoordinateReadout } from "@/components/CoordinateReadout";
import { CorrAgeGauge } from "@/components/CorrAgeGauge";
import { EmptyState } from "@/components/EmptyState";
import { MapPanel } from "@/components/MapPanel";
import { NtripStatus } from "@/components/NtripStatus";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { SystemChips } from "@/components/SystemChips";
import { SkyPlot } from "@/components/charts/SkyPlot";
import { Sparkline } from "@/components/charts/Sparkline";
import { RING_SIZE, useEpochRing } from "@/lib/epochRing";
import { DASH, fmtAcc, fmtBytes, fmtDuration, fmtMeters, fmtRate } from "@/lib/format";
import { type BaseInfo, useLive, useStale } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useBaseMode, useRover } from "@/lib/queries";
import { bearingToBase, fixLevel } from "@/lib/status";
import type { BaseModeView, ImuSample, RtkStatus, SurveyIn } from "@/lib/types";
import { cn } from "@/lib/utils";

const panelLink = "text-ink-2 hover:text-ink hover:underline";

function PositionMode({ svin, live, view, loading }: { svin: SurveyIn; live: BaseInfo; view: BaseModeView | undefined; loading: boolean }) {
  if (svin.active && !svin.valid) {
    return (
      <>
        <p className="mb-2">Survey-in running</p>
        <Stat label="Elapsed" value={fmtDuration(svin.dur_s)} />
        <Stat label="Observations" value={String(svin.obs)} />
        <Stat label="Mean 3D accuracy" value={fmtAcc(svin.mean_acc_m)} level="warning" />
      </>
    );
  }
  if (svin.valid) {
    return (
      <>
        <p className="mb-2">Survey-in complete</p>
        <Stat label="Mean 3D accuracy" value={fmtAcc(svin.mean_acc_m)} level="good" />
        <Stat label="Duration" value={fmtDuration(svin.dur_s)} />
        <Stat label="Observations" value={String(svin.obs)} />
      </>
    );
  }
  // The socket's base slice is authoritative once it has spoken; the query covers the first view.
  const mode = live.mode ?? view?.mode ?? null;
  const site = live.site ?? view?.site ?? null;
  if (view && !view.available) return <p className="text-ink-2">No position-mode manager on this source (replay or rover role).</p>;
  if (mode === "fixed") {
    const check = live.verified === true || (live.verified == null && view?.verified) ? "verified" : live.verified === false ? "mismatch" : "not yet verified";
    return (
      <>
        <p className="mb-2">Fixed site {site ?? "(unnamed)"}</p>
        <Stat label="RTCM 1005 check" value={check} level={check === "verified" ? "good" : check === "mismatch" ? "critical" : "warning"} />
        {live.mismatch?.dx != null ? <Stat label="Offset" value={`${live.mismatch.dx.toFixed(3)}, ${live.mismatch.dy?.toFixed(3)}, ${live.mismatch.dz?.toFixed(3)} m`} hint="ΔX, ΔY, ΔZ against the site" /> : null}
      </>
    );
  }
  if (mode === "survey-in") return <p className="text-ink-2">Survey-in is configured but has not started.</p>;
  if (mode === "off") return <p className="text-ink-2">Position mode is off. Start a survey-in or activate a site.</p>;
  if (loading) return <p className="text-ink-3">Loading…</p>;
  return <p className="text-ink-2">Position mode not reported yet.</p>;
}

function RtkSummary({ rtk }: { rtk: RtkStatus }) {
  const level: StatusLevel = rtk.carr_soln === 2 ? "good" : rtk.carr_soln === 1 ? "warning" : "serious";
  return (
    <>
      <div className="mb-2">
        <StatusBadge level={level} label={rtk.carr_soln_name} />
      </div>
      <CorrAgeGauge age={rtk.corr_age_s} />
      <div className="mt-2">
        <Stat label="Baseline" value={fmtMeters(rtk.baseline_m, 2)} />
        <Stat label="Bearing to base" value={rtk.heading_valid && rtk.heading_deg != null ? `${bearingToBase(rtk.heading_deg).toFixed(1)}°` : DASH} />
        <Stat label="Reference station" value={rtk.ref_station_id == null ? DASH : String(rtk.ref_station_id)} />
      </div>
    </>
  );
}

/** An INS rover's IMU at a glance: filter mode, temperature, the acceleration and rotation magnitudes. */
function ImuSummary({ imu, mode }: { imu: ImuSample; mode: string | undefined }) {
  const norm = (v: [number, number, number] | null) => (v ? Math.hypot(...v) : null);
  const accel = norm(imu.accel_mps2);
  const gyro = norm(imu.gyro_radps);
  return (
    <>
      <Stat label="INS mode" value={mode || DASH} />
      <Stat label="Temperature" value={imu.temperature_c == null ? DASH : `${imu.temperature_c.toFixed(1)} °C`} />
      <Stat label="|Acceleration|" value={accel == null ? DASH : `${accel.toFixed(2)} m/s²`} />
      <Stat label="|Rotation rate|" value={gyro == null ? DASH : `${((gyro * 180) / Math.PI).toFixed(2)} °/s`} />
    </>
  );
}

/**
 * Glance page: the position hero, the sky plot, the map; then fix, systems, position mode and
 * corrections (on a rover: the RTK solution and the NTRIP client); then sparklines of the last
 * epochs. Everything reads the live store; figures grey
 * when no epoch has arrived for 5 s or the socket is down.
 */
export default function Dashboard() {
  const state = useLive((s) => s.state);
  const ntripClients = useLive((s) => s.ntripClients);
  const liveBase = useLive((s) => s.base);
  const receiverConnected = useLive((s) => s.receiverConnected);
  const stale = useStale();
  const ring = useEpochRing();
  const isRover = useLive((s) => s.role === "rover");
  const liveNtrip = useLive((s) => s.ntripClient);
  const baseMode = useBaseMode(!isRover);
  // The socket reports the client every few seconds; the overview covers the first view.
  const rover = useRover(isRover);

  if (!state) {
    return (
      <>
        <PageHeader title="Dashboard" />
        <EmptyState title="Waiting for the receiver" body="The daemon has not reported a state yet. Check the Receiver page if this persists." />
      </>
    );
  }

  const fix = fixLevel(state.fix, receiverConnected, stale);
  const msgTypes = Object.keys(state.rtcm_out.messages).sort();
  const spanS = ring.length > 1 ? (ring[ring.length - 1].t - ring[0].t) / 1000 : 0;

  return (
    <>
      <PageHeader title="Dashboard" />
      <div data-testid="dashboard-grid" data-stale={stale} className={cn("grid grid-cols-12 gap-4", stale && "[&_.num]:text-ink-3")}>
        <Panel className="col-span-12 lg:col-span-4" bodyClassName="h-full">
          <CoordinateReadout position={state.position} accuracy={state.accuracy} />
        </Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Sky">
          <SkyPlot sats={state.sats} />
        </Panel>
        <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Map" bodyClassName="flex p-0">
          <MapPanel lat={state.position.lat} lon={state.position.lon} hAcc={state.accuracy.h_acc_m} rovers={ntripClients} />
        </Panel>

        <Panel className="col-span-12 md:col-span-6 lg:col-span-3" title="Fix">
          <div className="mb-2">
            <StatusBadge level={fix.level} label={fix.label} />
          </div>
          <Stat label="Fix type" value={state.fix.fix_type_name} />
          <Stat label="Carrier solution" value={state.fix.carr_soln_name} />
          <Stat label="Satellites used" value={`${state.sat_summary.used}/${state.sat_summary.tracked}`} />
          <Stat label="PDOP" value={state.dops.p != null ? state.dops.p.toFixed(1) : DASH} />
          <Stat label="Uptime" value={fmtDuration(state.fix.uptime_ms != null ? state.fix.uptime_ms / 1000 : null)} />
        </Panel>
        <Panel
          className="col-span-12 md:col-span-6 lg:col-span-3"
          title="Satellites by system"
          actions={
            <Link to="/satellites" className={panelLink}>
              Details
            </Link>
          }
        >
          <SystemChips summary={state.sat_summary} />
        </Panel>
        {state.imu ? (
          <Panel
            className="col-span-12 md:col-span-6 lg:col-span-3"
            title="IMU"
            actions={
              <Link to="/receiver" className={panelLink}>
                Details
              </Link>
            }
          >
            <ImuSummary imu={state.imu} mode={state.ins?.mode_name} />
          </Panel>
        ) : null}
        {isRover ? (
          <>
            <Panel
              className="col-span-12 md:col-span-6 lg:col-span-3"
              title="RTK"
              actions={
                <Link to="/rtk" className={panelLink}>
                  Details
                </Link>
              }
            >
              <RtkSummary rtk={state.rtk} />
            </Panel>
            <Panel
              className="col-span-12 md:col-span-6 lg:col-span-3"
              title="NTRIP client"
              actions={
                <Link to="/rtk" className={panelLink}>
                  Manage
                </Link>
              }
            >
              <NtripStatus ntrip={liveNtrip ?? rover.data?.ntrip ?? null} configuredUrl={rover.data?.ntrip_url} />
            </Panel>
          </>
        ) : (
          <>
            <Panel
              className="col-span-12 md:col-span-6 lg:col-span-3"
              title="Position mode"
              actions={
                <Link to="/site" className={panelLink}>
                  Manage
                </Link>
              }
            >
              <PositionMode svin={state.survey_in} live={liveBase} view={baseMode.data} loading={baseMode.isPending} />
            </Panel>
            <Panel
              className="col-span-12 md:col-span-6 lg:col-span-3"
              title="Corrections"
              actions={
                <Link to="/corrections" className={panelLink}>
                  Details
                </Link>
              }
            >
              <Stat label="RTCM out" value={fmtRate(state.rtcm_out.bytes_per_s)} level={state.rtcm_out.bytes_per_s > 0 ? "good" : "serious"} />
              <Stat label="Message types" value={String(msgTypes.length)} hint={msgTypes.join(", ") || undefined} />
              <Stat label="Rovers connected" value={String(ntripClients.length)} />
              <Stat label="Sent" value={fmtBytes(state.rtcm_out.total_bytes)} />
            </Panel>
          </>
        )}

        <Panel
          className="col-span-12"
          title="Recent"
          actions={
            ring.length >= 2 ? (
              <span className="num text-ink-2">
                {ring.length} epochs · {fmtDuration(spanS)}
              </span>
            ) : null
          }
        >
          {ring.length < 2 ? (
            <EmptyState title="Collecting epochs" body={`Horizontal accuracy, satellites used and mean C/N0 appear after two epochs; the last ${RING_SIZE} are kept while this page is open.`} />
          ) : (
            <div className="grid gap-4 md:grid-cols-3">
              <Sparkline label="Horizontal accuracy" values={ring.map((r) => r.hAcc)} format={fmtAcc} />
              <Sparkline label="Satellites used" values={ring.map((r) => r.nsatUsed)} format={(v) => v.toFixed(0)} />
              <Sparkline label="Mean C/N0" values={ring.map((r) => r.cnoMean)} format={(v) => `${v.toFixed(0)} dB-Hz`} />
            </div>
          )}
        </Panel>
      </div>
    </>
  );
}
