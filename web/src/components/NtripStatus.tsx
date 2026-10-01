import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { DASH, fmtBytes, fmtDuration } from "@/lib/format";
import type { NtripClientStatus } from "@/lib/types";

/**
 * The rover's NTRIP client: a connected badge, the caster, what it has received and its last
 * error. `detailed` adds the protocol, how long it has been connected, the last RTCM's age, the
 * bad-CRC drops and the reconnects (the RTK page); without it the Dashboard's short form.
 */
export function NtripStatus({ ntrip, detailed = false }: { ntrip: NtripClientStatus | null; detailed?: boolean }) {
  if (!ntrip) return <p className="text-ink-2">No caster configured. Set NTRIP_URL or use "Change caster" on the RTK page.</p>;
  return (
    <>
      <div className="mb-2">
        <StatusBadge level={ntrip.connected ? "good" : "critical"} label={ntrip.connected ? "Connected" : "Disconnected"} />
      </div>
      <Stat label="Caster" value={`${ntrip.host}:${ntrip.port}/${ntrip.mountpoint}`} />
      {detailed ? (
        <>
          <Stat label="Protocol" value={ntrip.version ? `NTRIP v${ntrip.version}` : DASH} />
          <Stat label="Connected for" value={ntrip.connected ? fmtDuration(ntrip.connected_for_s) : DASH} />
          <Stat label="Last RTCM" value={ntrip.last_rtcm_age_s == null ? DASH : `${ntrip.last_rtcm_age_s.toFixed(1)} s ago`} />
        </>
      ) : null}
      <Stat label="Received" value={fmtBytes(ntrip.bytes_received)} />
      <Stat label="Frames injected" value={String(ntrip.frames_injected)} />
      {detailed ? (
        <>
          <Stat label="Dropped (bad CRC)" value={String(ntrip.crc_dropped)} level={ntrip.crc_dropped ? "warning" : undefined} />
          <Stat label="Reconnects" value={String(ntrip.reconnects)} />
        </>
      ) : null}
      {ntrip.last_error ? (
        <p className="mt-2 text-status-critical-text">
          {ntrip.last_error}
          {!ntrip.connected && ntrip.next_retry_s ? ` · retrying after ${ntrip.next_retry_s.toFixed(0)} s` : ""}
        </p>
      ) : null}
    </>
  );
}
