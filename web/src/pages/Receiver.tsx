import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "@/app/PageHeader";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { CopyButton } from "@/components/CopyButton";
import { EmptyState } from "@/components/EmptyState";
import { Panel } from "@/components/Panel";
import { Stat } from "@/components/Stat";
import { StatusBadge } from "@/components/StatusBadge";
import { Gauge } from "@/components/charts/Gauge";
import { Sparkline } from "@/components/charts/Sparkline";
import { Spectrum } from "@/components/charts/Spectrum";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { describeError, receiverPoll, receiverReapply, receiverReset } from "@/lib/api";
import { DASH, fmtBytes, fmtDuration, fmtLocal, fmtUtc, fmtUtcDate } from "@/lib/format";
import { useLive, useStale } from "@/lib/live";
import type { StatusLevel } from "@/lib/palette";
import { useReceiver } from "@/lib/queries";
import { type RfSample, useRfRing } from "@/lib/rfRing";
import type { Hardware, PollResponse, PortStats, ResetKind, RfBlock, TimeInfo } from "@/lib/types";
import { cn } from "@/lib/utils";

/** How long to wait for a first MON-SPAN while connected before concluding the firmware has none. */
export const SPAN_WAIT_MS = 10_000;
/** A reset's "reconnecting" state gives up after this; the receiver normally re-enumerates within seconds. */
export const RESET_WAIT_MS = 90_000;
/** Epochs this soon after the reset command are the old session still draining, not the receiver back. */
const RESET_SETTLE_MS = 3000;

// ---------------------------------------------------------------- decoders (u-blox MON-HW / MON-RF)

interface Word {
  word: string;
  level?: StatusLevel;
}
const ANT_STATUS: Record<number, Word> = {
  0: { word: "Initialising", level: "warning" },
  1: { word: "Unknown", level: "warning" },
  2: { word: "OK", level: "good" },
  3: { word: "Short circuit", level: "critical" },
  4: { word: "Open circuit", level: "critical" },
};
const ANT_POWER: Record<number, string> = { 0: "Off", 1: "On", 2: "Unknown" };
const JAMMING: Record<number, Word> = {
  0: { word: "unknown", level: "warning" },
  1: { word: "OK", level: "good" },
  2: { word: "warning", level: "warning" },
  3: { word: "critical", level: "critical" },
};
const antStatus = (code: number): Word => ANT_STATUS[code] ?? { word: `code ${code}`, level: "warning" };
const antPower = (code: number): string => ANT_POWER[code] ?? `code ${code}`;
const jamming = (code: number): Word => JAMMING[code] ?? { word: `code ${code}`, level: "warning" };

/** NAV-TIMEUTC utcStandard. */
const UTC_STANDARD: Record<number, string> = { 0: "not available", 1: "CRL (Japan)", 2: "NIST", 3: "USNO", 4: "BIPM", 5: "European laboratories", 6: "SU", 7: "NTSC (China)", 8: "NPLI (India)", 15: "unknown" };
/** MON-COMMS portId and the port it stands for. */
const PORT_NAMES: Record<number, string> = { 0x0000: "I2C", 0x0100: "UART1", 0x0201: "UART2", 0x0300: "USB", 0x0400: "SPI" };
const portHex = (id: number) => `0x${id.toString(16).padStart(4, "0")}`;

const RESET_KINDS: { kind: ResetKind; about: string }[] = [
  { kind: "hot", about: "Hot restarts the receiver and keeps ephemeris, almanac, position and time. Seconds to a fix." },
  { kind: "warm", about: "Warm drops the ephemeris; almanac, position and time stay. About half a minute to a fix." },
  { kind: "cold", about: "Cold drops all satellite data (ephemeris, almanac, position, time). Minutes to a fix. The configuration is kept." },
  {
    kind: "factory",
    about:
      "Factory clears BBR and flash: every setting the daemon wrote, including the base position and the message rates. When the receiver reconnects, the daemon re-persists the profile to every layer, flash included.",
  },
];
const TYPED_RESETS: ResetKind[] = ["cold", "factory"];

const QUICK_PICKS = ["MON-VER", "MON-HW", "MON-RF", "NAV-SIG"];

const yesNo = (v: boolean) => (v ? "yes" : "no");
const fmtCell = (v: unknown): string => (v == null ? DASH : typeof v === "object" ? JSON.stringify(v) : String(v));

// ------------------------------------------------------------------------------------- panels

function AntennaStats({ antStatusCode, antPowerCode }: { antStatusCode: number; antPowerCode: number }) {
  const a = antStatus(antStatusCode);
  return (
    <>
      <Stat label="Antenna" value={a.word} level={a.level} title={`antStatus ${antStatusCode}`} />
      <Stat label="Antenna power" value={antPower(antPowerCode)} title={`antPower ${antPowerCode}`} />
    </>
  );
}

function Trends({ jam, agc }: { jam: (number | null)[]; agc: (number | null)[] }) {
  return (
    <div className="grid grid-cols-2 gap-4">
      <Sparkline label="Jamming trend" values={jam} format={(v) => String(Math.round(v))} />
      <Sparkline label="AGC trend" values={agc} format={(v) => String(Math.round(v))} />
    </div>
  );
}

function RfBlockPanel({ b, ring }: { b: RfBlock; ring: RfSample[] }) {
  const j = jamming(b.jamming_state);
  const of = (pick: (r: { jam: number; agc: number }) => number) => ring.map((s) => {
    const r = s.blocks.find((x) => x.id === b.block_id);
    return r ? pick(r) : null;
  });
  return (
    <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title={`RF block ${b.block_id}`} actions={<StatusBadge level={j.level ?? "warning"} label={`Interference ${j.word}`} />}>
      <div className="flex flex-col gap-3">
        <Gauge label="Jamming indicator" value={b.jam_ind} max={255} level={j.level} />
        <Gauge label="AGC count" value={b.agc_cnt} max={8191} />
        <Trends jam={of((r) => r.jam)} agc={of((r) => r.agc)} />
        <div>
          <AntennaStats antStatusCode={b.ant_status} antPowerCode={b.ant_power} />
          <Stat label="Noise per ms" value={String(b.noise_per_ms)} />
          <Stat label="I/Q offset" value={`${b.ofs_i} · ${b.ofs_q}`} />
          <Stat label="I/Q magnitude" value={`${b.mag_i} · ${b.mag_q}`} />
          <Stat label="Self-test" value={b.post_status === 0 ? "passed" : `0x${b.post_status.toString(16)}`} title={`postStatus ${b.post_status}`} />
        </div>
      </div>
    </Panel>
  );
}

function HardwarePanel({ hw, withGauges, ring }: { hw: Hardware; withGauges: boolean; ring: RfSample[] }) {
  const j = jamming(hw.jamming_state);
  return (
    <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Antenna & hardware" actions={withGauges ? <StatusBadge level={j.level ?? "warning"} label={`Interference ${j.word}`} /> : undefined}>
      <div className="flex flex-col gap-3">
        {withGauges ? (
          <>
            <Gauge label="Jamming indicator" value={hw.jam_ind} max={255} level={j.level} />
            <Gauge label="AGC count" value={hw.agc_cnt} max={8191} />
            <Trends jam={ring.map((s) => s.hw?.jam ?? null)} agc={ring.map((s) => s.hw?.agc ?? null)} />
          </>
        ) : null}
        <div>
          <AntennaStats antStatusCode={hw.ant_status} antPowerCode={hw.ant_power} />
          {withGauges ? null : <Stat label="Interference" value={j.word} level={j.level} title={`jammingState ${hw.jamming_state}`} />}
          <Stat label="Noise per ms" value={String(hw.noise_per_ms)} />
          <Stat label="RTC calibrated" value={yesNo(hw.rtc_calib)} />
          <Stat label="Safe boot" value={yesNo(hw.safe_boot)} />
          <Stat label="Crystal" value={hw.xtal_absent ? "absent" : "present"} />
        </div>
      </div>
    </Panel>
  );
}

function TimePanel({ t, uptimeMs }: { t: TimeInfo; uptimeMs: number | null }) {
  const valid = [t.valid_date && "date", t.valid_time && "time", t.valid_utc && "UTC", t.fully_resolved && "fully resolved"].filter(Boolean).join(" · ") || "none";
  const leap =
    t.leap_change == null || t.time_to_leap_event_s == null
      ? DASH
      : t.leap_change === 0
        ? "none scheduled"
        : `${t.leap_change > 0 ? "+" : ""}${t.leap_change} s in ${fmtDuration(t.time_to_leap_event_s)}`;
  return (
    <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Time">
      <Stat label="UTC" value={fmtUtcDate(t.utc)} title={t.utc ? `${fmtLocal(t.utc)} local` : undefined} />
      <Stat label="Valid" value={valid} />
      <Stat label="GPS week" value={t.gps_week == null ? DASH : String(t.gps_week)} />
      <Stat label="Time of week" value={t.gps_tow_s == null ? DASH : `${t.gps_tow_s.toFixed(3)} s`} />
      <Stat label="Leap seconds" value={t.leap_s == null ? DASH : `${t.leap_s} s`} />
      <Stat label="Leap event" value={leap} />
      <Stat label="UTC standard" value={t.utc_standard == null ? DASH : UTC_STANDARD[t.utc_standard] ?? `code ${t.utc_standard}`} title={t.utc_standard == null ? undefined : `utcStandard ${t.utc_standard}`} />
      <Stat label="Time accuracy" value={t.t_acc_ns == null ? DASH : `${t.t_acc_ns} ns`} />
      <Stat label="Clock bias" value={t.clk_bias_ns == null ? DASH : `${t.clk_bias_ns} ns`} />
      <Stat label="Clock drift" value={t.clk_drift_nsps == null ? DASH : `${t.clk_drift_nsps} ns/s`} />
      <Stat label="Receiver uptime" value={uptimeMs == null ? DASH : fmtDuration(uptimeMs / 1000)} />
    </Panel>
  );
}

function PortsPanel({ ports }: { ports: PortStats[] }) {
  return (
    <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Ports">
      {ports.length === 0 ? (
        <p className="text-ink-2">Port statistics (MON-COMMS) have not arrived.</p>
      ) : (
        ports.map((p) => (
          <div key={p.port_id} className="mb-3 last:mb-0">
            <p className="mb-1 flex items-baseline gap-2">
              <span>{PORT_NAMES[p.port_id] ?? "Port"}</span>
              <span className="num text-[12px] leading-4 text-ink-3">{portHex(p.port_id)}</span>
            </p>
            <Stat label="TX" value={`${fmtBytes(p.tx_bytes)} · ${p.tx_usage}% (peak ${p.tx_peak_usage}%)`} />
            <Stat label="RX" value={`${fmtBytes(p.rx_bytes)} · ${p.rx_usage}% (peak ${p.rx_peak_usage}%)`} />
            <Stat label="Pending TX · RX" value={`${p.tx_pending} · ${p.rx_pending}`} />
            <Stat label="Overruns · skipped" value={`${p.overrun_errs} · ${p.skipped}`} />
          </div>
        ))
      )}
    </Panel>
  );
}

// ----------------------------------------------------------------------------------- actions

function PollDialog({ disabled }: { disabled: boolean }) {
  const [msgClass, setMsgClass] = useState("MON");
  const [msgId, setMsgId] = useState("MON-VER");
  const [result, setResult] = useState<{ msgId: string; data: PollResponse } | null>(null);
  const poll = useMutation({
    mutationFn: () => receiverPoll(msgClass.trim(), msgId.trim()),
    onSuccess: (data) => setResult({ msgId: msgId.trim(), data }),
  });
  const pick = (name: string) => {
    setMsgClass(name.split("-")[0]);
    setMsgId(name);
  };
  const refused = result?.data.identity === "ACK-NAK";
  const rows = result ? Object.entries(result.data) : [];
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" disabled={disabled}>
          Poll a message…
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle className="text-[16px] leading-6 font-medium">Poll a UBX message</DialogTitle>
          <DialogDescription className="text-[14px] leading-5 text-ink-2">The parsed fields of the receiver's reply. A firmware that does not know the message answers ACK-NAK.</DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Quick picks">
          {QUICK_PICKS.map((name) => (
            <Button key={name} type="button" variant="outline" size="sm" className="num" onClick={() => pick(name)}>
              {name}
            </Button>
          ))}
        </div>
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            poll.mutate();
          }}
        >
          <Input aria-label="Message class" value={msgClass} onChange={(e) => setMsgClass(e.target.value.toUpperCase())} className="num w-24" autoComplete="off" spellCheck={false} />
          <Input aria-label="Message id" value={msgId} onChange={(e) => setMsgId(e.target.value.toUpperCase())} className="num w-40" autoComplete="off" spellCheck={false} />
          <Button type="submit" disabled={poll.isPending || !msgClass.trim() || !msgId.trim()}>
            Poll
          </Button>
        </form>
        {poll.isError ? (
          <Alert variant="destructive">
            <AlertDescription className="text-[14px] leading-5">{describeError(poll.error)}</AlertDescription>
          </Alert>
        ) : null}
        {result ? (
          <div className="flex flex-col gap-2">
            {refused ? <p className="text-ink-2">The receiver refused this poll (ACK-NAK): this firmware does not know {result.msgId}.</p> : null}
            <div className="flex items-center justify-between gap-3">
              <span className="text-ink-2">
                {result.msgId} · {rows.length} field{rows.length === 1 ? "" : "s"}
              </span>
              <CopyButton text={JSON.stringify(result.data, null, 2)} label="Copy JSON" />
            </div>
            <div className="max-h-80 overflow-auto rounded-md border border-line">
              <table className="w-full text-[12px] leading-4">
                <thead>
                  <tr className="text-ink-2">
                    <th scope="col" className="px-2 py-1 text-left font-medium">Field</th>
                    <th scope="col" className="px-2 py-1 text-left font-medium">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(([k, v]) => (
                    <tr key={k} className="border-t border-line">
                      <td className="px-2 py-1 align-top whitespace-nowrap">{k}</td>
                      <td className="num px-2 py-1 break-all">{fmtCell(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ) : (
          <p className="text-ink-2">Class and id are UBX names: MON and MON-VER. CFG-VALGET cannot be polled here.</p>
        )}
      </DialogContent>
    </Dialog>
  );
}

interface PendingReset {
  kind: ResetKind;
  at: number;
}

/** True while a reset is in flight: cleared when the receiver drops and comes back, or produces epochs again. */
function useResetWatch(pending: PendingReset | null, clear: () => void) {
  useEffect(() => {
    if (!pending) return;
    let wasDown = useLive.getState().receiverConnected === false;
    const timer = setTimeout(clear, RESET_WAIT_MS);
    const unsub = useLive.subscribe((s, prev) => {
      if (s.receiverConnected === false) wasDown = true;
      const cameBack = wasDown && s.receiverConnected === true && prev.receiverConnected !== true;
      const producing = s.receiverConnected === true && (s.lastEpochAt ?? 0) > pending.at + RESET_SETTLE_MS;
      if (cameBack || producing) clear();
    });
    return () => {
      clearTimeout(timer);
      unsub();
    };
  }, [pending, clear]);
}

// -------------------------------------------------------------------------------------- page

/**
 * The receiver itself: RF health per block (jamming and AGC gauges with their trends, antenna
 * words), the spectrum analyser when the firmware has one, firmware and capabilities, time,
 * port statistics, and the three actions the daemon offers (re-apply the profile, reset, poll a
 * message). The actions are disabled with a note on a passive (replay) source or without a
 * receiver, since the daemon would answer 409 anyway.
 */
export default function Receiver() {
  const state = useLive((s) => s.state);
  const liveConnected = useLive((s) => s.receiverConnected);
  const liveCaps = useLive((s) => s.receiverCapabilities);
  const stale = useStale();
  const info = useReceiver();
  const qc = useQueryClient();
  const ring = useRfRing();
  const [resetKind, setResetKind] = useState<ResetKind>("hot");
  const [pendingReset, setPendingReset] = useState<PendingReset | null>(null);
  const [lastReset, setLastReset] = useState<ResetKind | null>(null);
  const [reapplied, setReapplied] = useState<{ at: string; unsupported: number } | null>(null);
  const [spanTimedOut, setSpanTimedOut] = useState(false);

  const caps = liveCaps ?? info.data?.capabilities ?? null;
  const connected = liveConnected ?? info.data?.connected ?? state?.connected ?? false;
  const passive = info.data?.passive ?? false;
  const spanUnsupported = caps?.unsupported.includes("MON-SPAN") ?? false;
  // The spectrum panel's meaning depends on the capabilities; hold it until the first answer.
  const capsUnknown = caps === null && info.isPending;
  const hasSpectra = (state?.spectrum.length ?? 0) > 0;

  useEffect(() => {
    if (hasSpectra) {
      setSpanTimedOut(false);
      return;
    }
    if (!connected || spanUnsupported) return;
    const timer = setTimeout(() => setSpanTimedOut(true), SPAN_WAIT_MS);
    return () => clearTimeout(timer);
  }, [hasSpectra, connected, spanUnsupported]);

  useResetWatch(pendingReset, () => {
    if (pendingReset) setLastReset(pendingReset.kind);
    setPendingReset(null);
  });

  const reapply = useMutation({
    mutationFn: receiverReapply,
    onSuccess: (r) => {
      setReapplied({ at: new Date().toISOString(), unsupported: r.capabilities.unsupported.length });
      void qc.invalidateQueries({ queryKey: ["receiver"] });
    },
  });
  const reset = useMutation({
    mutationFn: receiverReset,
    onSuccess: (r) => {
      setLastReset(null);
      setPendingReset({ kind: r.kind, at: Date.now() });
    },
  });

  if (!state) {
    return (
      <>
        <PageHeader title="Receiver" />
        <EmptyState title="Waiting for the receiver" body="Hardware, firmware and time appear as soon as the daemon reports a snapshot." />
      </>
    );
  }

  // The configured source ("auto", "serial:/dev/ttyACM0"); the state carries what it resolved to.
  const source = info.data?.source ?? state.source;
  const resolved = state.source && state.source !== source ? state.source : undefined;
  const actionable = connected && !passive;
  const fw = state.firmware;
  const typed = TYPED_RESETS.includes(resetKind);
  const link = pendingReset
    ? { level: "warning" as const, label: "Resetting…" }
    : connected
      ? { level: "good" as const, label: "Connected" }
      : { level: "critical" as const, label: "Receiver disconnected" };

  return (
    <>
      <PageHeader title="Receiver">
        <StatusBadge level={link.level} label={link.label} />
        <span className="num text-ink-2">{source}</span>
      </PageHeader>
      <div data-testid="receiver-grid" data-stale={stale} className={cn("grid grid-cols-12 gap-4", stale && "[&_.num]:text-ink-3")}>
        {state.rf.map((b) => (
          <RfBlockPanel key={b.block_id} b={b} ring={ring} />
        ))}
        {state.hardware ? <HardwarePanel hw={state.hardware} withGauges={state.rf.length === 0} ring={ring} /> : null}
        {state.rf.length === 0 && !state.hardware ? (
          <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="RF health">
            <EmptyState title="No hardware report yet" body="MON-RF and MON-HW arrive about once a second once the receiver is configured." />
          </Panel>
        ) : null}

        <Panel className="col-span-12" title="Spectrum">
          {capsUnknown ? (
            <EmptyState title="Checking the firmware's capabilities" />
          ) : spanUnsupported || (!hasSpectra && spanTimedOut) ? (
            <EmptyState
              title="Spectrum analyser (MON-SPAN) is not supported by this firmware."
              body={
                spanUnsupported
                  ? `The receiver refused the MON-SPAN rate. HPG 1.30 and later report a spectrum; this receiver runs ${caps?.fw_version || fw.fw_version || "an older firmware"}.`
                  : `No MON-SPAN message arrived in ${SPAN_WAIT_MS / 1000} s. HPG 1.30 and later report a spectrum; this receiver runs ${caps?.fw_version || fw.fw_version || "an older firmware"}.`
              }
            />
          ) : hasSpectra ? (
            <Spectrum spectra={state.spectrum} />
          ) : (
            <EmptyState title="Waiting for spectrum data" body={connected ? "MON-SPAN arrives about once a second once the receiver reports it." : "The receiver is not connected."} />
          )}
        </Panel>

        <Panel className="col-span-12 md:col-span-6 lg:col-span-4" title="Firmware">
          <Stat label="Module" value={caps?.module || fw.module || DASH} />
          <Stat label="Firmware" value={caps?.fw_version || fw.fw_version || DASH} />
          <Stat label="Protocol version" value={caps?.protver || fw.protver || DASH} />
          <Stat label="Core" value={fw.sw_version || DASH} />
          <Stat label="Hardware version" value={fw.hw_version || DASH} />
          <Stat label="Source" value={source || DASH} hint={resolved} />
          {caps ? (
            <div className="mt-3 flex flex-col gap-1 text-[12px] leading-4 text-ink-2">
              <p>Supported: {caps.supported.length ? caps.supported.join(", ") : "nothing probed yet"}</p>
              <p>Unsupported: {caps.unsupported.length ? caps.unsupported.join(", ") : "nothing"}</p>
            </div>
          ) : (
            <p className="mt-3 text-[12px] leading-4 text-ink-3">Capabilities are probed when the receiver connects.</p>
          )}
        </Panel>

        <TimePanel t={state.time} uptimeMs={state.fix.uptime_ms} />
        <PortsPanel ports={state.ports} />

        <Panel className="col-span-12" title="Actions">
          <div className="flex flex-col gap-3">
            {!connected ? (
              <p className="text-ink-2">The receiver is not connected: re-apply, reset and poll wait until it is back.</p>
            ) : passive ? (
              <p className="text-ink-2">This source is passive (a replay file): the daemon only listens and writes nothing, so re-apply, reset and poll are unavailable.</p>
            ) : null}
            <div className="flex flex-wrap gap-2">
              <ConfirmDialog
                trigger={
                  <Button type="button" variant="outline" disabled={!actionable}>
                    Re-apply profile
                  </Button>
                }
                title="Re-apply the receiver profile?"
                body="Re-probes the firmware and rewrites every configuration key (RAM, BBR and flash), then reads them back. Nothing is written where the receiver already matches. Takes a few seconds; corrections pause briefly."
                confirmLabel="Re-apply"
                onConfirm={() => reapply.mutateAsync()}
              />
              <ConfirmDialog
                trigger={
                  <Button type="button" variant="outline" disabled={!actionable}>
                    Reset…
                  </Button>
                }
                title="Reset the receiver"
                body="A hardware reset: the USB device drops off the bus and comes back, and corrections pause until it does. Expect the receiver to reconnect within a few seconds."
                confirmLabel="Confirm reset"
                destructive={typed}
                requireText={typed ? resetKind : undefined}
                onConfirm={() => reset.mutateAsync(resetKind)}
              >
                <div className="flex flex-col gap-2 text-[14px] leading-5">
                  <label className="flex items-center gap-2">
                    Reset type
                    <select aria-label="Reset type" value={resetKind} onChange={(e) => setResetKind(e.target.value as ResetKind)} className="rounded-md border border-line bg-panel-2 px-2 py-1 text-ink">
                      {RESET_KINDS.map((k) => (
                        <option key={k.kind} value={k.kind}>
                          {k.kind}
                        </option>
                      ))}
                    </select>
                  </label>
                  <p className="text-ink-2">{RESET_KINDS.find((k) => k.kind === resetKind)?.about}</p>
                </div>
              </ConfirmDialog>
              <PollDialog disabled={!actionable} />
            </div>
            {pendingReset ? (
              <p role="status" aria-label="Reset progress" className="text-ink-2">
                {pendingReset.kind.charAt(0).toUpperCase() + pendingReset.kind.slice(1)} reset sent at <span className="num">{fmtUtc(new Date(pendingReset.at).toISOString())} UTC</span> · waiting for the receiver to reconnect…
              </p>
            ) : lastReset ? (
              <p className="text-ink-2">Receiver reconnected after the {lastReset} reset.</p>
            ) : null}
            {reapplied ? (
              <p role="status" aria-label="Re-apply result" className="text-ink-2">
                Profile re-applied at <span className="num">{fmtUtc(reapplied.at)} UTC</span>
                {reapplied.unsupported ? ` · ${reapplied.unsupported} feature${reapplied.unsupported === 1 ? "" : "s"} this firmware lacks` : ""}.
              </p>
            ) : null}
          </div>
        </Panel>
      </div>
    </>
  );
}
