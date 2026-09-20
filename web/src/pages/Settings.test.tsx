import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router";
import { auth } from "@/lib/api";
import { resetLiveForTests } from "@/lib/live";
import { resetPrefsForTests } from "@/lib/prefs";
import type { ConfigResponse, ConfigValues, ReceiverInfo, ValidationIssue } from "@/lib/types";
import Settings from "./Settings";

// ---- fixtures -----------------------------------------------------------------------------

const values: ConfigValues = {
  role: "base",
  mtrtk_source: "auto",
  baud: 115200,
  data_dir: "/data",
  mtrtk_env_file: "/app/.env",
  station_id: "MTRK",
  country: "BGD",
  marker_name: "MTRK",
  antenna_type: "NONE",
  antenna_height_m: 0,
  observer: "mtrtk",
  agency: "mtrtk",
  receiver_strict: true,
  replay_speed: 1,
  replay_loop: false,
  replay_log: false,
  base_mode: "survey-in",
  svin_min_duration_s: 300,
  svin_acc_limit_m: 2,
  active_site: null,
  rtcm_msm: 7,
  rtcm_1230_rate: 5,
  rtcm_station_id: 0,
  ntrip_bind: "tailscale",
  ntrip_port: 2101,
  mountpoint: "MTRK",
  ntrip_user: "rover",
  ntrip_password: "***",
  ntrip_max_clients: 32,
  web_bind: "tailscale",
  web_port: 8080,
  web_password: "***",
  web_allow_insecure: false,
  log_messages: ["RXM-RAWX", "RXM-SFRBX"],
  min_free_gb: 5,
  fsync_interval_s: 10,
  rover_driver: "ublox",
  rover_nav_hz: 5,
  rover_dynmodel: "portable",
  ntrip_url: "ntrip://rover:***@base.tail:2101/MTRK",
  ntrip_gga_interval_s: 10,
  nmea_tcp_port: 10110,
  nmea_udp_targets: [],
  nmea_serial: null,
  json_udp_port: null,
  alert_webhook_url: null,
  public_domain: null,
};

const config = (over: Partial<ConfigResponse> = {}): ConfigResponse => ({
  values,
  pending: {},
  env_file: "/app/.env",
  secret_keys: ["alert_webhook_url", "ntrip_password", "web_password"],
  live_keys: ["active_site", "base_mode", "svin_acc_limit_m", "svin_min_duration_s"],
  read_only_keys: ["mtrtk_env_file"],
  url_secret_keys: ["ntrip_url"],
  ...over,
});

const receiver: ReceiverInfo = {
  connected: true,
  passive: false,
  source: "serial:/dev/ttyACM0",
  capabilities: { protver: "27.12", fw_version: "HPG 1.13", module: "ZED-F9P", supported: ["MON-SPAN"], unsupported: [] },
  firmware: { sw_version: "EXT CORE 1.00", hw_version: "00190000", fw_version: "HPG 1.13", protver: "27.12", module: "ZED-F9P", extensions: [] },
};

interface Answers {
  config?: ConfigResponse;
  /** The PUT answer; default 200 with `changed` echoing the keys that were sent. */
  save?: { status: number; detail: string | ValidationIssue[] };
  saveResult?: { changed: string[]; restart_required: boolean };
  restart?: { status: number; detail: string };
}

let calls: [string, RequestInit | undefined][] = [];

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const bodyOf = (call: [string, RequestInit | undefined]) => JSON.parse(call[1]!.body as string) as { values: Record<string, unknown> };
const callsTo = (method: string, suffix: string) => calls.filter(([u, i]) => (i?.method ?? "GET") === method && u.split("?")[0].endsWith(suffix));

function mockFetch(a: Answers = {}) {
  calls = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const p = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const method = init?.method ?? "GET";
    calls.push([p, init]);
    if (p.endsWith("/api/config") && method === "GET") return json(a.config ?? config());
    if (p.endsWith("/api/config") && method === "PUT") {
      if (a.save) return json({ detail: a.save.detail }, a.save.status);
      const sent = Object.keys((JSON.parse(init!.body as string) as { values: Record<string, unknown> }).values);
      return json(a.saveResult ?? { changed: sent, restart_required: true });
    }
    if (p.endsWith("/api/restart")) {
      if (a.restart && a.restart.status >= 400) return json({ detail: a.restart.detail }, a.restart.status);
      return json({ ok: true });
    }
    if (p.endsWith("/api/receiver")) return json(receiver);
    return json({ detail: `unexpected route ${p}` }, 404);
  }) as typeof fetch;
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Settings />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Settings page", () => {
  beforeEach(() => {
    resetLiveForTests();
    resetPrefsForTests();
    localStorage.clear();
    auth.reset();
    document.documentElement.removeAttribute("data-theme");
    mockFetch();
  });

  it("puts only the keys the operator changed in the PUT body", async () => {
    renderPage();
    const station = await screen.findByLabelText(/station id/i);
    expect(station).toHaveValue("MTRK");

    await userEvent.clear(station);
    await userEvent.type(station, "BASE");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));

    const put = callsTo("PUT", "/api/config");
    expect(put).toHaveLength(1);
    // Never the whole object: `mtrtk_env_file` is read-only and would be a 422.
    expect(bodyOf(put[0]).values).toEqual({ station_id: "BASE" });
  });

  it("keeps an untouched secret out of the PUT and a retyped one in it", async () => {
    renderPage();
    const secret = await screen.findByLabelText(/ntrip password/i);
    expect(secret).toHaveValue("***");
    expect(secret).toHaveAttribute("type", "password");

    // Touched but left as the mask: the stored password must survive.
    await userEvent.click(secret);
    await userEvent.click(screen.getByLabelText(/marker name/i));
    await userEvent.clear(screen.getByLabelText(/marker name/i));
    await userEvent.type(screen.getByLabelText(/marker name/i), "PILLAR");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(bodyOf(callsTo("PUT", "/api/config")[0]).values).toEqual({ marker_name: "PILLAR" });

    await userEvent.clear(secret);
    await userEvent.type(secret, "hunter2");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
    const second = bodyOf(callsTo("PUT", "/api/config")[1]).values;
    expect(second.ntrip_password).toBe("hunter2");
  });

  it("shows a URL secret with its password masked and sends the mask back untouched", async () => {
    renderPage();
    const url = await screen.findByLabelText(/rover ntrip url/i);
    expect(url).toHaveValue("ntrip://rover:***@base.tail:2101/MTRK");

    await userEvent.clear(url);
    await userEvent.type(url, "ntrip://rover:***@base2.tail:2101/MTRK");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(bodyOf(callsTo("PUT", "/api/config")[0]).values).toEqual({ ntrip_url: "ntrip://rover:***@base2.tail:2101/MTRK" });
  });

  it("disables the read-only keys and says why", async () => {
    renderPage();
    const envFile = await screen.findByLabelText(/env file/i);
    expect(envFile).toBeDisabled();
    expect(envFile).toHaveValue("/app/.env");
    expect(screen.getByText(/deployment decision/i)).toBeInTheDocument();
  });

  it("keeps a persistent pending banner and restarts behind a confirmation", async () => {
    mockFetch({ config: config({ pending: { station_id: "BASE", web_port: 8081 } }) });
    renderPage();

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("station_id");
    expect(banner).toHaveTextContent("web_port");
    expect(banner).toHaveTextContent(/docker compose up -d/i);
    expect(banner).toHaveTextContent(/systemd/i);

    await userEvent.click(within(banner).getByRole("button", { name: /restart now/i }));
    const dialog = await screen.findByRole("dialog");
    expect(callsTo("POST", "/api/restart")).toHaveLength(0);
    await userEvent.click(within(dialog).getByRole("button", { name: /^restart$/i }));
    expect(callsTo("POST", "/api/restart")).toHaveLength(1);
    expect(await screen.findByText(/reconnect/i)).toBeInTheDocument();
  });

  it("raises the banner after a save the daemon says needs a restart", async () => {
    renderPage();
    const station = await screen.findByLabelText(/station id/i);
    expect(screen.queryByRole("alert")).toBeNull();
    await userEvent.clear(station);
    await userEvent.type(station, "BASE");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(/station_id/);
    expect(banner).toHaveTextContent(/restart/i);
  });

  it("shows the daemon's 422 detail verbatim", async () => {
    mockFetch({ save: { status: 422, detail: [{ loc: ["body", "values", "station_id"], msg: "String should have at most 4 characters", type: "string_too_long" }] } });
    renderPage();
    const station = await screen.findByLabelText(/station id/i);
    await userEvent.clear(station);
    await userEvent.type(station, "TOOLONG");
    await userEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("values.station_id: String should have at most 4 characters");
  });

  it("offers the theme here as well as in the rail", async () => {
    renderPage();
    const theme = await screen.findByLabelText(/^theme$/i);
    await userEvent.selectOptions(theme, "light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("mtrtk:theme")).toBe('"light"');
  });

  it("offers sign out only when a password is configured", async () => {
    mockFetch({ config: config({ values: { ...values, web_password: null } }) });
    renderPage();
    await screen.findByLabelText(/station id/i);
    expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull();

    cleanup();
    mockFetch();
    renderPage();
    expect(await screen.findByRole("button", { name: /sign out/i })).toBeInTheDocument();
  });
});
