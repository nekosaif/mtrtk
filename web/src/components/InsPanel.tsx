import { ConfirmDialog } from "@/components/ConfirmDialog";
import { DataTable, type Column } from "@/components/DataTable";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { DASH, fmtDuration, fmtMeters } from "@/lib/format";
import { STATUS_TEXT, type StatusLevel } from "@/lib/palette";
import type { Attitude, ImuSample, InsBlock, InsConfigItem, InsConfigReport, InsItemState, InsLeverArm, InsStatus } from "@/lib/types";

const VENDORS: Record<string, string> = { sbg: "SBG Systems", vectornav: "VectorNav" };
/** The filter counts as aligned (navigating) from this mode on: SBG EKF nav position, VectorNav tracking. */
const ALIGNED_MODE: Record<string, number> = { sbg: 4, vectornav: 2 };
/** GNSS fix codes with no position solution: SBG NO_SOLUTION; VectorNav "No fix" and "Time only". Mirrors alerts.GNSS_NO_FIX. */
const GNSS_NO_FIX: Record<string, readonly number[]> = { sbg: [0], vectornav: [0, 1] };
export const gnssHasNoFix = (status: InsStatus): boolean => status.gnss_fix != null && (GNSS_NO_FIX[status.vendor] ?? [0]).includes(status.gnss_fix);
const STATE_LEVEL: Partial<Record<InsItemState, StatusLevel>> = { applied: "good", pending: "warning", mismatched: "serious", error: "critical" };
const STATE_WORD: Record<InsItemState, string> = {
  applied: "applied",
  unchanged: "as wanted",
  pending: "differs",
  mismatched: "read back wrong",
  unsupported: "not on this unit",
  error: "error",
};

const fmtValue = (v: unknown): string => (v == null ? DASH : typeof v === "object" ? JSON.stringify(v) : String(v));
const fmtVec = (v: readonly number[] | null | undefined, digits = 3): string => (v ? v.map((x) => x.toFixed(digits)).join(", ") : DASH);
const deg = (v: number | null | undefined, digits = 2) => (v == null ? DASH : `${v.toFixed(digits)}°`);
const sigma = (v: number | null | undefined) => (v == null ? undefined : `±${v.toFixed(2)}° 1σ`);

/** True when applying the profile would change something on the unit. */
export function needsApply(report: InsConfigReport | null | undefined): boolean {
  return !!report && (report.pending.length > 0 || report.mismatched.length > 0);
}

function modeLevel(status: InsStatus): StatusLevel {
  const from = ALIGNED_MODE[status.vendor];
  if (from == null || status.mode == null) return "warning";
  return status.mode >= from ? "good" : "warning";
}

function flags(map: Record<string, boolean>, want: boolean): string[] {
  return Object.entries(map)
    .filter(([, v]) => v === want)
    .map(([k]) => k.replaceAll("_", " "));
}

function FilterPanel({ status, attitude }: { status: InsStatus | null; attitude: Attitude | null }) {
  if (!status) {
    return (
      <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="INS filter">
        <p className="text-ink-2">The unit has not reported its filter state yet.</p>
      </Panel>
    );
  }
  const unhealthy = flags(status.general_ok, false);
  const errors = flags(status.errors, true);
  const aiding = flags(status.aiding, true);
  return (
    <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="INS filter" actions={<StatusBadge level={modeLevel(status)} label={status.mode_name || `mode ${status.mode ?? "?"}`} />}>
      <Stat label="GNSS fix" value={status.gnss_fix_name || (status.gnss_fix == null ? DASH : String(status.gnss_fix))} level={gnssHasNoFix(status) ? "serious" : undefined} />
      <Stat label="Aiding" value={aiding.length ? aiding.join(", ") : "none"} />
      <Stat
        label="Health"
        value={unhealthy.length || errors.length ? [...unhealthy.map((f) => `${f} not OK`), ...errors.map((f) => `${f} error`)].join(", ") : Object.keys(status.general_ok).length || Object.keys(status.errors).length ? "OK" : DASH}
        level={unhealthy.length || errors.length ? "critical" : undefined}
      />
      <Stat label="Heading" value={deg(attitude?.heading_deg)} hint={sigma(attitude?.acc_heading_deg)} title={attitude?.source || undefined} />
      <Stat label="Roll" value={deg(attitude?.roll_deg)} hint={sigma(attitude?.acc_roll_deg)} />
      <Stat label="Pitch" value={deg(attitude?.pitch_deg)} hint={sigma(attitude?.acc_pitch_deg)} />
      {status.antenna_baseline_m != null ? <Stat label="Antenna baseline" value={fmtMeters(status.antenna_baseline_m, 2)} title="Separation of the unit's two GNSS antennas (GPS1_HDT), not the distance to the base" /> : null}
      {status.uptime_s != null ? <Stat label="Unit uptime" value={fmtDuration(status.uptime_s)} /> : null}
      {status.cpu_pct != null ? <Stat label="Unit CPU" value={`${status.cpu_pct}%`} /> : null}
    </Panel>
  );
}

function ImuPanel({ imu }: { imu: ImuSample }) {
  const gyroDeg = imu.gyro_radps ? imu.gyro_radps.map((r) => (r * 180) / Math.PI) : null;
  return (
    <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="IMU">
      <Stat label="Acceleration (m/s²)" value={fmtVec(imu.accel_mps2, 2)} hint="x, y, z (body)" />
      <Stat label="Rotation rate (°/s)" value={fmtVec(gyroDeg, 2)} hint="x, y, z (body)" />
      <Stat label="Temperature" value={imu.temperature_c == null ? DASH : `${imu.temperature_c.toFixed(1)} °C`} />
    </Panel>
  );
}

/** A value cell that wraps: configuration values can be long JSON. */
const wrap = (text: string) => <span className="num break-all whitespace-normal">{text}</span>;

const ARM_COLUMNS: Column<InsLeverArm>[] = [
  { key: "name", header: "Arm", cell: (a) => a.name },
  { key: "configured", header: "Configured (m)", cell: (a) => wrap(a.configured ? fmtVec(a.configured) : "not set") },
  { key: "read_back", header: "Unit (m)", cell: (a) => wrap(fmtVec(a.read_back)) },
];

function LeverArms({ arms }: { arms: InsLeverArm[] }) {
  return <DataTable aria-label="Lever arms" columns={ARM_COLUMNS} rows={arms} rowKey={(a) => a.name} empty="No lever arms for this unit." />;
}

const CONFIG_COLUMNS: Column<InsConfigItem>[] = [
  { key: "name", header: "Item", cell: (i) => <span className="num">{i.name}</span>, sortValue: (i) => i.name },
  {
    key: "state",
    header: "State",
    sortValue: (i) => i.state,
    cell: (i) => {
      const level = STATE_LEVEL[i.state];
      return (
        <span data-state={i.state} className={level ? undefined : "text-ink-2"} style={level ? { color: STATUS_TEXT[level] } : undefined} title={i.error}>
          {STATE_WORD[i.state] ?? i.state}
        </span>
      );
    },
  },
  { key: "current", header: "Unit", cell: (i) => wrap(fmtValue(i.current)) },
  { key: "wanted", header: "Wanted", cell: (i) => wrap(i.wanted == null ? "" : fmtValue(i.wanted)) },
];

function ConfigTable({ items }: { items: InsConfigItem[] }) {
  return <DataTable aria-label="INS configuration" className="max-h-96 overflow-auto" columns={CONFIG_COLUMNS} rows={items} rowKey={(i) => i.name} />;
}

/** What the apply will do with the result, in the words of the confirmation. */
function applyBody(ins: InsBlock): string {
  const never = " The baud rate is never written.";
  if (!ins.apply_config)
    return "INS_APPLY_CONFIG is off: the items that differ are written to the unit and read back, but not saved to flash, so they last until the unit restarts (some SBG settings may only take effect after a save and reboot). Turning INS_APPLY_CONFIG on later saves them at the next connect, although they then read back as unchanged." + never;
  if (ins.saved_this_run)
    return "Writes every item that differs to the unit and reads each one back. The settings were already saved to flash once since mtrtk started, and are saved at most once per run: these changes are not saved, so they last until the unit restarts." + never;
  return "Writes every item that differs to the unit, reads each one back, and saves the result to flash once everything matches (an SBG unit reboots to save)." + never;
}

/**
 * An INS rover's unit (SBG Ellipse-D or VectorNav VN-200) on the Receiver page: identity, the
 * filter mode with its aiding and health, heading / roll / pitch with their 1σ, the IMU, the
 * lever arms as configured against what the unit read back, and the configuration report with
 * one row per profile item. "Apply INS configuration" writes the profile behind a confirmation,
 * and is offered only while something differs; `report` (the live `ins.config`) wins over the
 * block's own.
 */
export function InsPanel({
  ins,
  status,
  imu,
  attitude,
  report,
  actionable,
  onApply,
  onReread,
}: {
  ins: InsBlock;
  status: InsStatus | null;
  imu: ImuSample | null;
  attitude: Attitude | null;
  report?: InsConfigReport | null;
  actionable: boolean;
  onApply: () => Promise<unknown>;
  onReread: () => Promise<unknown>;
}) {
  const info = ins.info;
  const config = report ?? ins.config_report;
  const changes = needsApply(config);
  const vendor = VENDORS[ins.vendor] ?? ins.vendor;
  return (
    <>
      <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="INS unit" actions={<StatusBadge level={ins.connected ? "good" : "critical"} label={ins.connected ? "Connected" : "Disconnected"} />}>
        <Stat label="Vendor" value={vendor} />
        <Stat label="Model" value={info?.model || DASH} />
        <Stat label="Firmware" value={info?.firmware || DASH} />
        <Stat label="Serial" value={info?.serial || DASH} />
        <Stat label="Hardware" value={info?.hardware || DASH} />
        <Stat label="Port" value={ins.port || DASH} />
        <Stat label="Raw GNSS" value={ins.raw_gnss_format ?? DASH} />
        {ins.dropped_rtcm_bytes ? <Stat label="RTCM dropped" value={`${ins.dropped_rtcm_bytes} B`} level="warning" /> : null}
        {!info ? <p className="mt-2 text-[12px] leading-4 text-ink-3">The unit's identity is read when it connects.</p> : null}
      </Panel>
      <FilterPanel status={status} attitude={attitude} />
      {imu ? <ImuPanel imu={imu} /> : null}
      <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Lever arms" bodyClassName="p-2">
        <LeverArms arms={ins.lever_arms} />
      </Panel>
      <Panel className="col-span-12" title="INS configuration" actions={config?.saved ? <span className="text-ink-2">saved to flash</span> : null}>
        <div className="flex flex-col gap-3">
          {config ? <ConfigTable items={config.items} /> : <p className="text-ink-2">The unit's configuration is read when it connects.</p>}
          {config?.errors.length ? (
            <ul className="flex flex-col gap-1 text-[12px] leading-4" style={{ color: STATUS_TEXT.critical }}>
              {config.errors.map((e) => (
                <li key={e}>{e}</li>
              ))}
            </ul>
          ) : null}
          {config?.notes.length ? (
            <ul className="flex flex-col gap-1 text-[12px] leading-4 text-ink-2">
              {config.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
            </ul>
          ) : null}
          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" variant="outline" disabled={!actionable} onClick={() => void onReread().catch(() => undefined)}>
              Re-read configuration
            </Button>
            <ConfirmDialog
              trigger={
                <Button type="button" disabled={!actionable || !changes}>
                  Apply INS configuration
                </Button>
              }
              title="Apply the INS configuration?"
              body={applyBody(ins)}
              confirmLabel="Apply"
              onConfirm={onApply}
            />
            {!changes && config ? <span className="text-ink-2">The unit matches the profile.</span> : null}
          </div>
        </div>
      </Panel>
    </>
  );
}
