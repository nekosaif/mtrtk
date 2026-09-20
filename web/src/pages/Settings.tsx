/**
 * The `.env` file as a form.
 *
 * Three rules shape this page, and all three come from what `PUT /api/config` actually does:
 *
 * 1. **Only what changed is sent.** The GET reports every field, read-only ones included, and a
 *    read-only key the request would *move* is a 422 — so the page diffs the draft against the
 *    values it was given and PUTs that subset. A save that changes one field is a body with one
 *    field in it, which is also what the daemon's log line ends up naming.
 * 2. **Masks travel back untouched.** A secret comes back as `***` and means "leave it alone";
 *    an untouched one is simply not in the diff. `ntrip_url` is masked in its password part
 *    only, so editing the host and leaving `***` alone keeps the stored password.
 * 3. **Writing `.env` is not applying it.** `pending` is the daemon's own account of where the
 *    file and the running process disagree, and it stays on screen until a restart settles it.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PageHeader } from "@/app/PageHeader";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Panel } from "@/components/Panel";
import { SignOutButton, usePasswordConfigured } from "@/components/SignOutButton";
import { ThemeSelect } from "@/components/ThemeToggle";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { describeError, putConfig, restartDaemon } from "@/lib/api";
import { useLive } from "@/lib/live";
import { useConfig, useReceiver } from "@/lib/queries";
import type { ConfigChange, ConfigKey, ConfigResponse, ConfigValues } from "@/lib/types";
import { cn } from "@/lib/utils";

// --------------------------------------------------------------------------- the field table

type Control = "text" | "number" | "toggle" | "select" | "list";
interface Option {
  value: string;
  label: string;
}
export interface Field {
  key: ConfigKey;
  label: string;
  control: Control;
  options?: Option[];
  help?: string;
}
export interface Group {
  title: string;
  /** One sentence under the panel title when the group needs framing. */
  note?: string;
  fields: Field[];
}

const opts = (...values: (string | number)[]): Option[] => values.map((v) => ({ value: String(v), label: String(v) }));

/**
 * Every `Settings` field the daemon reports, grouped the way an operator thinks about the box.
 * A key the API grows that is missing here still appears, as text, under "Other settings" —
 * invisible configuration is worse than an unpolished label.
 */
export const SETTINGS_GROUPS: Group[] = [
  {
    title: "Station",
    note: "What goes into RINEX headers and file names.",
    fields: [
      { key: "station_id", label: "Station ID", control: "text", help: "Exactly four upper-case letters or digits" },
      { key: "marker_name", label: "Marker name", control: "text" },
      { key: "country", label: "Country code", control: "text", help: "Three letters, ISO 3166-1 alpha-3" },
      { key: "antenna_type", label: "Antenna type", control: "text", help: "IGS code, or NONE" },
      { key: "antenna_height_m", label: "Antenna height above the mark (m)", control: "number" },
      { key: "observer", label: "Observer", control: "text" },
      { key: "agency", label: "Agency", control: "text" },
    ],
  },
  {
    title: "Receiver",
    fields: [
      { key: "role", label: "Role", control: "select", options: opts("base", "rover") },
      { key: "mtrtk_source", label: "Receiver device", control: "text", help: "auto, a serial device path, or file:<path> to replay a capture" },
      { key: "baud", label: "Baud rate", control: "number" },
      { key: "receiver_strict", label: "Stop at startup if the receiver rejects a core setting", control: "toggle", help: "Off: log the warning and carry on with whatever it did accept" },
    ],
  },
  {
    title: "Base position",
    note: "These four reach the receiver as soon as they are saved; the Site page is the fuller way to drive them.",
    fields: [
      { key: "base_mode", label: "Position mode", control: "select", options: opts("survey-in", "fixed", "off") },
      { key: "active_site", label: "Active site", control: "text", help: "The saved site a fixed base sits on; empty means whichever site is marked active" },
      { key: "svin_min_duration_s", label: "Survey-in minimum duration (s)", control: "number" },
      { key: "svin_acc_limit_m", label: "Survey-in accuracy limit (m)", control: "number" },
    ],
  },
  {
    title: "RTCM output",
    fields: [
      { key: "rtcm_msm", label: "Observation format", control: "select", options: opts(7, 4).map((o) => ({ ...o, label: `MSM${o.value}` })), help: "MSM7 carries Doppler and full-precision phase; MSM4 is smaller on the wire" },
      { key: "rtcm_1230_rate", label: "GLONASS bias (1230) interval (s)", control: "number" },
      { key: "rtcm_station_id", label: "RTCM reference station number", control: "number", help: "0–4095; rovers see this in message 1005" },
    ],
  },
  {
    title: "NTRIP caster",
    fields: [
      { key: "ntrip_bind", label: "NTRIP bind address", control: "text", help: "tailscale, lan, all, or an IP address" },
      { key: "ntrip_port", label: "NTRIP port", control: "number" },
      { key: "mountpoint", label: "Mountpoint", control: "text" },
      { key: "ntrip_user", label: "Rover username", control: "text" },
      { key: "ntrip_password", label: "NTRIP password", control: "text", help: "Empty means anonymous rovers are allowed" },
      { key: "ntrip_max_clients", label: "Maximum connected rovers", control: "number" },
    ],
  },
  {
    title: "Web UI",
    fields: [
      { key: "web_bind", label: "Web UI bind address", control: "text", help: "tailscale, lan, all, or an IP address" },
      { key: "web_port", label: "Web UI port", control: "number" },
      { key: "web_password", label: "Web UI password", control: "text", help: "Required once the UI is reachable from anywhere but Tailscale" },
      { key: "web_allow_insecure", label: "Allow the UI without a password", control: "toggle", help: "Only for a network you already trust" },
      { key: "public_domain", label: "Public domain", control: "text", help: "The name a tunnel publishes this base under" },
    ],
  },
  {
    title: "Raw logging",
    fields: [
      { key: "log_messages", label: "Messages to log", control: "list", help: "Comma-separated UBX message names" },
      { key: "min_free_gb", label: "Keep at least this much disk free (GB)", control: "number" },
      { key: "fsync_interval_s", label: "fsync interval (s)", control: "number" },
    ],
  },
  { title: "Alerts", fields: [{ key: "alert_webhook_url", label: "Webhook URL", control: "text", help: "ntfy, Discord or any endpoint that takes a JSON POST" }] },
  {
    title: "Replay",
    note: "Only used when the receiver device is a file:<path> capture.",
    fields: [
      { key: "replay_speed", label: "Replay speed", control: "number", help: "1 is real time; 0 is as fast as the file can be read" },
      { key: "replay_loop", label: "Loop the capture", control: "toggle" },
      { key: "replay_log", label: "Write raw logs while replaying", control: "toggle" },
    ],
  },
  {
    title: "Rover",
    note: "Read by the rover role (Phase 6); a base ignores them.",
    fields: [
      { key: "rover_driver", label: "Rover driver", control: "select", options: opts("ublox", "sbg_ellipse", "vectornav") },
      { key: "rover_nav_hz", label: "Navigation rate (Hz)", control: "number" },
      { key: "rover_dynmodel", label: "Dynamic model", control: "select", options: opts("portable", "stationary", "pedestrian", "automotive", "airborne1g", "airborne2g", "airborne4g") },
      { key: "ntrip_url", label: "Rover NTRIP URL", control: "text", help: "ntrip://user:password@host:port/MOUNTPOINT — leave *** in place to keep the stored password" },
      { key: "ntrip_gga_interval_s", label: "GGA interval (s)", control: "number" },
      { key: "nmea_tcp_port", label: "NMEA TCP port", control: "number" },
      { key: "nmea_udp_targets", label: "NMEA UDP targets", control: "list", help: "Comma-separated host:port" },
      { key: "nmea_serial", label: "NMEA serial output", control: "text" },
      { key: "json_udp_port", label: "JSON UDP port", control: "number" },
    ],
  },
  {
    title: "Deployment",
    fields: [
      { key: "data_dir", label: "Data directory", control: "text" },
      { key: "mtrtk_env_file", label: "Env file", control: "text" },
    ],
  },
];

const LISTED = new Set<string>(SETTINGS_GROUPS.flatMap((g) => g.fields.map((f) => f.key)));

// ------------------------------------------------------------------------------- the page

type Draft = Record<string, unknown>;

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

export default function Settings() {
  const config = useConfig();
  const qc = useQueryClient();
  const values = config.data?.values;

  const [baseline, setBaseline] = useState<ConfigValues | null>(null);
  const [draft, setDraft] = useState<Draft>({});
  /** What a number or list field currently shows, so a half-typed "1." survives a render. */
  const [text, setText] = useState<Record<string, string>>({});
  const [result, setResult] = useState<ConfigChange | null>(null);
  const [restarted, setRestarted] = useState(false);
  const dirty = useRef(false);

  const save = useMutation({
    mutationFn: (payload: Partial<ConfigValues>) => putConfig(payload),
    onSuccess: (change) => {
      setResult(change);
      setBaseline(null); // re-seed the form from the daemon's own account of the new values
      void qc.invalidateQueries({ queryKey: ["config"] });
    },
  });

  useEffect(() => {
    if (!values || values === baseline) return;
    if (baseline !== null && dirty.current) return; // never destroy edits under the operator
    setBaseline(values);
    setDraft({ ...values });
    setText({});
  }, [values, baseline]);

  const base = baseline ?? values;
  const changed = useMemo(() => (base ? Object.keys(draft).filter((k) => !same(draft[k], (base as unknown as Draft)[k])) : []), [draft, base]);
  dirty.current = changed.length > 0;

  if (!config.data || !values) {
    return (
      <>
        <PageHeader title="Settings" />
        <p className="text-ink-2">{config.isError ? describeError(config.error) : "Reading the daemon's configuration…"}</p>
      </>
    );
  }

  const cfg = config.data;
  const payload = Object.fromEntries(changed.map((k) => [k, draft[k]])) as unknown as Partial<ConfigValues>;
  const groups: Group[] = [...SETTINGS_GROUPS, { title: "Other settings", fields: Object.keys(values).filter((k) => !LISTED.has(k)).map((k) => ({ key: k as ConfigKey, label: k, control: "text" as Control })) }];

  return (
    <>
      <PageHeader title="Settings">
        <span className="num text-[12px] leading-4 text-ink-2" title="Where changes are written">
          {cfg.env_file}
        </span>
      </PageHeader>

      <PendingBanner cfg={cfg} result={result} restarted={restarted} onRestarted={() => setRestarted(true)} />

      {save.isError ? (
        <Alert variant="destructive" className="mb-4">
          <AlertDescription className="text-[14px] leading-5">{describeError(save.error)}</AlertDescription>
        </Alert>
      ) : null}
      {result && !save.isError ? (
        <p role="status" className="mb-4 text-ink-2">
          {result.changed.length === 0 ? "Nothing to change: the daemon already had those values." : `Saved ${result.changed.join(", ")}.`}
          {result.changed.length > 0 && !result.restart_required ? " Applied to the running base." : ""}
        </p>
      ) : null}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(payload);
        }}
        className="grid grid-cols-12 gap-4"
      >
        {groups.map((g) => {
          const fields = g.fields.filter((f) => f.key in values);
          if (fields.length === 0) return null;
          return (
            <Panel key={g.title} className="col-span-12 lg:col-span-6" title={g.title}>
              {g.note ? <p className="mb-3 text-[12px] leading-4 text-ink-2">{g.note}</p> : null}
              <div className="flex flex-col gap-3">
                {fields.map((f) => (
                  <FieldRow
                    key={f.key}
                    field={f}
                    cfg={cfg}
                    value={draft[f.key]}
                    text={text[f.key]}
                    changed={changed.includes(f.key)}
                    onValue={(v) => setDraft((d) => ({ ...d, [f.key]: v }))}
                    onText={(s) => setText((t) => ({ ...t, [f.key]: s }))}
                  />
                ))}
              </div>
            </Panel>
          );
        })}

        <Panel className="col-span-12 lg:col-span-6" title="This browser">
          <div className="flex flex-col gap-3">
            <ThemeSelect />
            <SessionRow />
            <ReceiverProfile />
          </div>
        </Panel>

        <div className="panel sticky bottom-0 z-10 col-span-12 flex flex-wrap items-center justify-end gap-3 px-4 py-3">
          <span className="mr-auto text-ink-2">
            {changed.length === 0 ? "No changes" : `${changed.length} change${changed.length === 1 ? "" : "s"}: `}
            {changed.length ? <span className="num text-ink">{changed.join(", ")}</span> : null}
          </span>
          <Button
            type="button"
            variant="outline"
            disabled={changed.length === 0 || save.isPending}
            onClick={() => {
              setDraft({ ...values });
              setText({});
              setBaseline(values);
            }}
          >
            Discard changes
          </Button>
          <Button type="submit" disabled={changed.length === 0 || save.isPending}>
            {save.isPending ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </form>
    </>
  );
}

// ------------------------------------------------------------------------------ one field

function FieldRow({
  field,
  cfg,
  value,
  text,
  changed,
  onValue,
  onText,
}: {
  field: Field;
  cfg: ConfigResponse;
  value: unknown;
  text: string | undefined;
  changed: boolean;
  onValue: (v: unknown) => void;
  onText: (s: string) => void;
}) {
  const id = useId();
  const key = field.key as string;
  const readOnly = (cfg.read_only_keys as string[]).includes(key);
  const secret = (cfg.secret_keys as string[]).includes(key);
  const urlSecret = (cfg.url_secret_keys as string[]).includes(key);
  const live = (cfg.live_keys as string[]).includes(key);
  const stored = (cfg.values as unknown as Draft)[key];

  // An emptied box on a key whose stored value is null is not a change: it is the same nothing.
  const asText = (s: string) => (s === "" && stored === null ? null : s);
  const shown = text ?? (field.control === "list" ? (Array.isArray(value) ? value.join(", ") : "") : value == null ? "" : String(value));

  const control = () => {
    if (readOnly) return <Input id={id} value={shown} disabled readOnly className="num" />;
    if (secret) {
      return (
        <Input
          id={id}
          type="password"
          autoComplete="new-password"
          spellCheck={false}
          value={shown}
          placeholder="not set"
          onChange={(e) => onValue(asText(e.target.value))}
        />
      );
    }
    switch (field.control) {
      case "toggle":
        return <Switch id={id} checked={Boolean(value)} onCheckedChange={(c) => onValue(c)} />;
      case "select":
        return (
          <select
            id={id}
            value={String(value ?? "")}
            onChange={(e) => onValue(typeof stored === "number" ? Number(e.target.value) : e.target.value)}
            className="h-9 rounded-md border border-line bg-panel-2 px-2 text-[14px] text-ink"
          >
            {field.options?.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        );
      case "number":
        return (
          <Input
            id={id}
            inputMode="decimal"
            className="num"
            value={shown}
            onChange={(e) => {
              const s = e.target.value;
              onText(s);
              const t = s.trim();
              if (t === "") return onValue(null);
              const n = Number(t);
              // A box holding "12x" is sent as written: the daemon's 422 names the field and
              // says what it wanted, which beats this page guessing.
              onValue(Number.isFinite(n) ? n : s);
            }}
          />
        );
      case "list":
        return (
          <Input
            id={id}
            className="num"
            value={shown}
            onChange={(e) => {
              onText(e.target.value);
              onValue(
                e.target.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean),
              );
            }}
          />
        );
      default:
        return <Input id={id} className={cn(urlSecret && "num")} spellCheck={false} value={shown} onChange={(e) => onValue(asText(e.target.value))} />;
    }
  };

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_minmax(140px,210px)] items-start gap-3 max-sm:grid-cols-1">
      <div className="min-w-0">
        <Label htmlFor={id} className="text-[14px] leading-5">
          {field.label}
        </Label>
        {changed ? <span className="ml-2 rounded-full border border-brass px-1.5 text-[12px] leading-4 text-ink-2">changed</span> : null}
        {field.help ? <p className="text-[12px] leading-4 text-ink-2">{field.help}</p> : null}
        {readOnly ? <p className="text-[12px] leading-4 text-ink-3">Set where the daemon starts — a deployment decision this page cannot move.</p> : null}
        {secret ? <p className="text-[12px] leading-4 text-ink-3">Stored value shown as ***; leave it to keep it, type to replace it.</p> : null}
        {urlSecret ? <p className="text-[12px] leading-4 text-ink-3">The password inside the URL is shown as ***; leave that part alone to keep it.</p> : null}
        {live ? <p className="text-[12px] leading-4 text-ink-3">Applies to the running base as soon as it is saved.</p> : null}
      </div>
      {control()}
    </div>
  );
}

// ------------------------------------------------------------------------- pending / restart

function PendingBanner({ cfg, result, restarted, onRestarted }: { cfg: ConfigResponse; result: ConfigChange | null; restarted: boolean; onRestarted: () => void }) {
  const status = useLive((s) => s.status);
  const pendingKeys = Object.keys(cfg.pending ?? {});
  const keys = pendingKeys.length ? pendingKeys : result?.restart_required ? result.changed : [];
  const restart = useMutation({ mutationFn: () => restartDaemon(), onSuccess: onRestarted });

  if (keys.length === 0) return null;
  return (
    <div role="alert" className="mb-4 flex flex-col gap-2 rounded-md border px-4 py-3" style={{ borderColor: "var(--status-warning)" }}>
      <p>
        Saved to <span className="num">{cfg.env_file}</span>, but the running daemon is still using the old values:{" "}
        <span className="num text-ink">{keys.join(", ")}</span>.
      </p>
      <p className="text-[12px] leading-4 text-ink-2">
        On a systemd install, Restart now is enough — the service comes back reading the file. Under Docker Compose a plain restart keeps the environment that was baked into
        the container when it was created, so only <span className="num">docker compose up -d</span> applies a changed .env.
      </p>
      {restart.isError ? <p className="text-[14px] leading-5 text-ink">{describeError(restart.error)}</p> : null}
      {restarted ? (
        <p role="status" className="text-[12px] leading-4 text-ink-2">
          {status === "open" ? "The daemon is back and this page has reconnected." : `The daemon is restarting; this page reconnects on its own (socket: ${status}).`}
        </p>
      ) : (
        <div>
          <ConfirmDialog
            trigger={<Button variant="outline" size="sm">Restart now…</Button>}
            title="Restart the daemon?"
            body="The receiver link, the NTRIP caster and the raw logger all stop and start again. Connected rovers reconnect; the hour being logged is closed and a new file is opened."
            confirmLabel="Restart"
            onConfirm={() => restart.mutateAsync()}
          />
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------------- session

/** Sign out lives here as well as in the rail's foot: on a phone the rail has no foot. */
function SessionRow() {
  const configured = usePasswordConfigured();
  if (!configured) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
      <div>
        <p className="text-[14px] leading-5">Session</p>
        <p className="text-[12px] leading-4 text-ink-2">Signing out clears this browser's cookie; the password itself is unchanged.</p>
      </div>
      <SignOutButton variant="outline" />
    </div>
  );
}

// ------------------------------------------------------------------------- receiver profile

function ReceiverProfile() {
  const receiver = useReceiver().data;
  const fw = receiver?.firmware;
  const caps = receiver?.capabilities;
  return (
    <div className="border-t border-line pt-3">
      <p className="text-[14px] leading-5">Receiver profile</p>
      <p className="text-[12px] leading-4 text-ink-2">What the daemon found on the wire, not something this page sets.</p>
      {/* minmax(0,…) and a break: `file:tests/fixtures/…ubx` is one unbreakable token, and a
          plain 1fr column would be sized by it — 13 px past the edge of a 360 px phone. */}
      <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[12px] leading-4">
        {[
          ["Module", fw?.module || "—"],
          ["Firmware", fw?.fw_version || "—"],
          ["Protocol", fw?.protver || "—"],
          ["Source", receiver?.source || "—"],
          ["Messages it refused", caps ? String(caps.unsupported.length) : "—"],
        ].map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-ink-2">{label}</dt>
            <dd className="num min-w-0 break-all">{value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
