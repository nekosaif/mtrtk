<div align="center">

# mtrtk

**An RTK base station, rover and post-processing toolkit for the u-blox ZED-F9P, with its own NTRIP caster and a live web UI.**

[![CI](https://github.com/nekosaif/mtrtk/actions/workflows/ci.yml/badge.svg)](https://github.com/nekosaif/mtrtk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-linux%2Famd64%20%7C%20linux%2Farm64-555.svg?logo=linux&logoColor=white)](docker/Dockerfile)

[Highlights](#highlights) ·
[Screenshots](#screenshots) ·
[Quick start](#quick-start-docker-compose) ·
[Workflows](#workflows) ·
[Configuration](#configuration) ·
[Docs](#documentation)

<br>

<img src="docs/images/ui-dashboard.png" alt="mtrtk dashboard on a replayed base station with one rover connected: position, sky plot, map, fix, satellites by system, RTCM output, recent trends and host health" width="100%">

</div>

<br>

mtrtk turns a u-blox ZED-F9P on any Linux host (a Raspberry Pi, an x86 box or a Jetson) into a
complete GNSS station. As a **base** it surveys or fixes its position, logs raw UBX by the hour,
broadcasts RTCM3 from a built-in NTRIP caster and checks every 1005 it sends against the saved
site. As a **rover** it pulls corrections, streams NMEA and JSON, and collects survey points. An
SBG Ellipse-D or VectorNav VN-200 can stand in as the rover. Raw hours export to RINEX for
CSRS-PPP, AUSPOS or OPUS, PPP results come back in as a centimetre-level site, and rover logs
post-process against the base with RTKLIB.

It is one asyncio process, one container and one `.env`. By default it listens only on your
Tailscale address.

## Contents

- [Highlights](#highlights)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [Supported hardware](#supported-hardware)
- [Setup](#setup): [requirements](#requirements), [Docker Compose](#quick-start-docker-compose), [native install](#native-install-systemd), [from source](#from-source), [Pi and Jetson](#raspberry-pi-and-jetson), [exposure](#exposure-beyond-tailscale-optional), [rovers](#connecting-rovers), [remote receiver](#remote-receiver-over-tailscale), [replay](#running-without-hardware-replay), [`doctor`](#mtrtk-doctor)
- [Workflows](#workflows)
- [Configuration](#configuration)
- [Command line](#command-line)
- [HTTP API](#http-api)
- [Documentation](#documentation)
- [Development](#development)
- [Project status](#project-status)
- [License](#license)

## Highlights

| | |
|---|---|
| **Receiver control** | Finds the F9P on USB, writes its profile with `CFG-VALSET` and reads every key back. Reconnects with backoff and re-applies the profile. One build runs on HPG 1.13 and 1.51. |
| **Base station** | Survey-in, or a fixed site from a verified TMODE3 write. RTCM3 MSM7 (or MSM4), 1005 and 1230. Each 1005 it broadcasts is decoded and checked against the saved site to 0.5 mm. |
| **NTRIP caster** | Built in. NTRIP v1 and v2 on one port, Basic auth or anonymous, a sourcetable, and a log of every connection with the rover's GGA position. |
| **Raw logs, RINEX and PPP** | Hourly UBX files aligned on receiver UTC, with sha256 sidecars and disk-based retention. RINEX export through RTKLIB for CSRS-PPP, AUSPOS and OPUS. PPP results import as a centimetre site. |
| **Rover** | An NTRIP client with GGA upload. NMEA over TCP, UDP, serial or a pty, and JSON over UDP. Sessions and averaged survey points that export as CSV, GeoJSON, KML or GPX. |
| **PPK** | RTKLIB `rnx2rtkp` against a remote mtrtk base, an upload or local logs. Camera pulses (TIM-TM2) are interpolated onto the track for geotagging. |
| **INS rovers** | SBG Ellipse-D and VectorNav VN-200 drivers that feed the same outputs, UI and ROS topics. Read-only by default. |
| **ROS 2** | A Humble and Jazzy bridge that runs as a WebSocket client of the daemon (websocket-client), so the daemon has no ROS dependency. NavSatFix, velocity, time reference, RTK status, time marks and NMEA on `/mtrtk/*`, plus `/mtrtk/imu` and `/mtrtk/heading` from an INS rover's attitude. |
| **Web UI and API** | A React app updated live over one WebSocket, with dark and light themes and a phone layout. FastAPI REST with interactive docs, history to 90 days and alerts to a webhook. |
| **Operations** | Tailscale-only by default, with optional Caddy and Cloudflare Tunnel profiles. `mtrtk doctor`, backup and restore, a systemd installer, and a non-root multi-arch container. |

## Screenshots

These are the real UI, captured on a test bench. The live pages run on two replay daemons that
play `tests/fixtures/f9p_hpg113_base_30s.ubx`: one as a base, and one as a rover connected to its
caster. A replay sends nothing to a receiver, so it runs no position-mode manager. The fixture is
a base recording, so it holds no RTK solution. The Site and RTK pages show those panels empty for
that reason. The Logs and History pages show raw hours and history recorded by a live base.

<table>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-satellites.png"><img src="docs/images/ui-satellites.png" alt="Satellites page" width="100%"></a>
      <p><b>Satellites.</b> Constellation filter chips and a full sky plot, with Sky, Signals and Table tabs. Filled discs are used in the fix.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-receiver.png"><img src="docs/images/ui-receiver.png" alt="Receiver page" width="100%"></a>
      <p><b>Receiver.</b> Per-RF-block jamming and AGC with trends, antenna and hardware status, and the MON-SPAN spectrum for L1 and L2/L5.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-corrections.png"><img src="docs/images/ui-corrections.png" alt="Corrections page" width="100%"></a>
      <p><b>Corrections.</b> RTCM 3 MSM7 output per message type, stream bitrate, the built-in NTRIP caster, the connected rover with its last GGA position, and the connection log.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-site.png"><img src="docs/images/ui-site.png" alt="Site page" width="100%"></a>
      <p><b>Site.</b> Saved ECEF sites with per-axis sigma, a site map and the guided PPP workflow. The site shown is the sample CSRS-PPP result from <code>tests/fixtures/ppp</code>, imported with <code>mtrtk ppp-import</code>. The position-mode, survey-in and verification panels are empty because a replay has no position-mode manager.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-logs.png"><img src="docs/images/ui-logs.png" alt="Logs page" width="100%"></a>
      <p><b>Logs</b> (live base). A 48-hour availability strip, raw-window download, RINEX export (CSRS-PPP preset) with finished export jobs, and the hourly UBX files.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-history.png"><img src="docs/images/ui-history.png" alt="History page" width="100%"></a>
      <p><b>History</b> (live base). The 24 h view of horizontal and vertical accuracy and PDOP at 1-minute rollups. The base had run for about 5 hours, so the right side of each chart is empty.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-rtk.png"><img src="docs/images/ui-rtk.png" alt="RTK page" width="100%"></a>
      <p><b>RTK (rover).</b> The NTRIP client connected to the base's caster, correction age and the NMEA outputs. The solution, received-RTCM, fix-state and camera-mark panels have no data here, because the replayed base recording has no RTK solution.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-survey.png"><img src="docs/images/ui-survey.png" alt="Survey page" width="100%"></a>
      <p><b>Survey (rover).</b> An open session, point collection with the RTK-fixed filter off, three 10-epoch points with sigma N/E/U, CSV/GeoJSON/KML/GPX export and a map. At 1440 px the points table scrolls sideways.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-ppk.png"><img src="docs/images/ui-ppk.png" alt="PPK page" width="100%"></a>
      <p><b>PPK.</b> A new RTKLIB run: the rover as a session, a window or an upload; the base as a remote base, an upload or local logs; the base position, camera events and the elevation mask.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-events.png"><img src="docs/images/ui-events.png" alt="Events page" width="100%"></a>
      <p><b>Events (rover).</b> The rover's events after its base was restarted: two acknowledged warnings, then the recovery, with a level filter.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-dashboard-light.png"><img src="docs/images/ui-dashboard-light.png" alt="Dashboard in the light theme" width="100%"></a>
      <p><b>Light theme.</b> The dashboard with the light theme selected.</p>
    </td>
    <td width="50%" valign="top" align="center">
      <a href="docs/images/ui-dashboard-mobile.png"><img src="docs/images/ui-dashboard-mobile.png" alt="Dashboard on a phone" width="48%"></a>
      <a href="docs/images/ui-rtk-mobile.png"><img src="docs/images/ui-rtk-mobile.png" alt="RTK page on a phone" width="48%"></a>
      <p align="left"><b>On a phone.</b> The dashboard and the RTK page at 390 px, with the bottom tab bar.</p>
    </td>
  </tr>
  <tr>
    <td colspan="2" align="center" valign="top">
      <a href="docs/images/ui-settings.png"><img src="docs/images/ui-settings.png" alt="Settings page" width="50%"></a>
      <p><b>Settings.</b> Station metadata for RINEX, receiver, base position and RTCM output, saved to the <code>.env</code>.</p>
    </td>
  </tr>
</table>

[`docs/ui.md`](docs/ui.md) walks through every page.

## Architecture

One asyncio process per host. Receiver bytes are split into frames and published on an
in-process bus. Each consumer subscribes with its own queue and runs under restart supervision.
RTKLIB runs as a subprocess inside background jobs.

```mermaid
flowchart TB
  RX["ZED-F9P or INS<br/>USB / serial"] -->|bytes| SRC["Source + router<br/>UBX, RTCM3, NMEA, vendor"]
  CTRL["Receiver controller<br/>profile, verify, reconnect"] -.->|config| RX
  SRC --> BUS(("Bus"))
  BUS --> LOG["Raw logger<br/>hourly .ubx"]
  BUS --> CAST["NTRIP caster<br/>base role"]
  BUS --> ROV["Rover services<br/>NTRIP client, NMEA/JSON, points"]
  BUS --> WEB["State + web<br/>REST, /ws, UI"]
  CAST -->|RTCM3| NROV["NTRIP rovers"]
  WEB --> JOBS["Jobs<br/>RINEX export, PPK via RTKLIB"]
  UI["Browser"] <--> WEB
  ROS["ROS 2 bridge"] <--> WEB
```

A base runs the caster and the base-mode manager; a rover runs the NTRIP client, outputs and
survey points instead. An INS driver fills the same receiver state as the F9P, so nothing
downstream changes. The full component diagram is in
[`docs/architecture.md`](docs/architecture.md).

## Supported hardware

| Unit | Firmware / protocol | Role | `ROVER_DRIVER` | Verification status |
|---|---|---|---|---|
| u-blox ZED-F9P | HPG 1.13 (PROTVER 27.12) | base | n/a | Verified live on hardware: profile apply and verify, survey-in, fixed site, 1005 check, NTRIP caster, raw logs |
| u-blox ZED-F9P | HPG 1.13 | rover | `ublox` (default) | Verified on replay (NTRIP client, correction age, NMEA/JSON, sessions, points). The live RTK-fixed check needs a second receiver and is pending |
| u-blox ZED-F9P | HPG 1.51 | base, rover | `ublox` | Supported by design through the capability probe; RINEX export converts L5 when the receiver reports it. Not yet run on 1.51 hardware |
| SBG Ellipse-D | sbgECom on Port A | rover (INS) | `sbg_ellipse` | Read-only stream verified on a real unit: framing, log layouts, raw GNSS to RINEX, daemon, API and UI. Configuration writes, RTCM input and RTK are unverified |
| VectorNav VN-200 | VN binary output 1 | rover (INS) | `vectornav` | Spec-based, built from the vendor documentation and not yet run on a unit. RTCM input is opt-in (`INS_VN_RTCM=1`) and unverified |

The full per-assumption matrix and the bench checklist are in [`docs/ins-drivers.md`](docs/ins-drivers.md).

## Setup

### Requirements

| Need | Details |
|---|---|
| **Host** | Linux, `amd64` or `arm64`: a Raspberry Pi with a 64-bit OS, an x86 box, or a Jetson. Images are published for `linux/amd64` and `linux/arm64` only. |
| **Receiver** | u-blox ZED-F9P on USB (HPG 1.13 or 1.51; `mtrtk doctor` recommends HPG 1.32 or later). It is found by `/dev/serial/by-id/*u-blox*` or by USB vendor ID `1546`. |
| **Container path** | Docker Engine with the Compose v2 plugin (`docker compose`). |
| **Native path** | Debian, Ubuntu or Raspberry Pi OS (64-bit) with systemd and `sudo`. Node.js 20 or later is needed to build the web UI. `install.sh` fetches or builds everything else. |
| **Network** | Tailscale, installed and logged in (`sudo tailscale up`). By default the caster and the UI bind to the `tailscale0` address only. They wait for that address to appear, re-bind if it changes, and never fall back to `0.0.0.0`. |
| **Disk** | At least `MIN_FREE_GB` (default 5 GB) free under `DATA_DIR`. Below that, the oldest raw logs not marked `keep` are deleted. |

### Quick start (Docker Compose)

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
cp .env.example .env
$EDITOR .env                       # at least NTRIP_PASSWORD, see below
docker compose up -d
docker compose logs -f             # one status line per second once the receiver is configured
```

Open `http://<tailscale-ip>:8080` (`tailscale ip -4` prints the address). Rovers connect to
`ntrip://rover:<NTRIP_PASSWORD>@<tailscale-ip>:2101/MTRK`.

Settings to review in `.env` before the first start:

| Variable | Default | Why it matters |
|---|---|---|
| `NTRIP_PASSWORD` | `change-me` | The rovers' password. Change it. The base refuses to start if the key is missing. An empty `NTRIP_PASSWORD=` means anonymous rovers, which is only safe on the tailnet. |
| `MTRTK_SOURCE` | `auto` | `auto`, or a fixed path such as `/dev/serial/by-id/usb-u-blox_...`. |
| `STATION_ID` | `MTRK` | Four upper-case letters or digits. Used in log file names and as the RINEX marker. |
| `BASE_MODE` | `survey-in` | `survey-in`, `fixed` (uses `ACTIVE_SITE`) or `off`. |
| `WEB_BIND` / `WEB_PASSWORD` | `tailscale` / empty | A bind other than `tailscale` needs a password; see the [`WEB_PASSWORD` rules](#web_password-rules). |
| `NTRIP_BIND` | `tailscale` | Also accepts `lan`, `all` or a literal IP address. |
| `DIALOUT_GID` | `20` | The host group that owns the serial device. 20 is right on Debian and Ubuntu; check yours with `stat -c %g /dev/ttyACM0`. |
| `MTRTK_RUN_AS_ROOT` | unset | Set to `1` only if no `DIALOUT_GID` gives access to the device. |

The container uses host networking. `/dev` is bind-mounted for hot-plug, and cgroup rules allow
only `ttyACM*` and `ttyUSB*`, so it is not `privileged`. The daemon runs as uid 1000 with every
capability dropped. Data lives in `./data`, and the healthcheck is `mtrtk healthcheck`.

> [!IMPORTANT]
> **Settings precedence under Docker.** Compose passes `./.env` to the container as environment
> variables, and environment variables outrank the `./data/.env` that the Settings page writes.
> A key that is set in `./.env` therefore keeps its `./.env` value. To change such a key, edit
> `./.env` and run `docker compose up -d`.

> [!NOTE]
> **Image tags.** Compose uses `ghcr.io/nekosaif/mtrtk:latest`, which follows tagged `vX.Y.Z`
> releases. Until `v0.1.0` is released, either build from the clone
> (`docker compose build && docker compose up -d`) or pin `:edge` in a
> `docker-compose.override.yml`. Pinning and updating are covered in [`docs/setup.md`](docs/setup.md).

### Native install (systemd)

Run the installer from the clone as the user who will operate mtrtk. Do not run it as root or
with `sudo`; it calls `sudo` itself where it needs to.

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
./install.sh --dry-run             # prints every command that would change something, runs none
./install.sh
journalctl -u mtrtk -f
```

<details>
<summary><b>What <code>install.sh</code> does, step by step, and its options</b></summary>

<br>

The script is idempotent: after a `git pull`, run `./install.sh` again.

1. Installs `uv` if it is missing, then creates `.venv` with `uv sync --frozen --no-dev`.
2. Builds RTKLIB demo5 `v2.5.1` (`convbin`, `rnx2rtkp`) into `/usr/local/bin` if those tools are missing. On a Pi this takes a few minutes. If the build fails, it falls back to Debian's `rtklib` package.
3. Builds the web UI if Node.js 20 or later is present. Without Node, the API and the NTRIP caster still run.
4. Sets up `.env`:
   - If there is no `.env`, it creates one from `.env.example` with `DATA_DIR=<clone>/data` and a random 20-character `NTRIP_PASSWORD`, and prints the password once.
   - If an existing `.env` still has the container path `DATA_DIR=/data`, it points it at `<clone>/data` and keeps a backup as `.env.bak-<UTC>`.
5. Installs `udev/99-mtrtk-ublox.rules` to `/etc/udev/rules.d/`. The rule keeps ModemManager away from u-blox ports and gives `ttyACM*` mode 0660 with group `dialout`.
6. Adds you to `dialout`. Your current shell gets the group only after you log in again; the service has it already.
7. Renders `systemd/mtrtk.service` into `/etc/systemd/system/mtrtk.service`, enables it and (re)starts it. The unit runs `<clone>/.venv/bin/mtrtk run` as your user with `MTRTK_ENV_FILE=<clone>/.env` and `Restart=always`.
8. Runs `mtrtk doctor`.

| Option | Effect |
|---|---|
| `--dry-run` | Print the commands, change nothing. |
| `--no-start` | Install and enable the service, but do not (re)start it. |
| `--no-web` | Skip the web UI build. |
| `--rtklib source\|apt\|skip` | How to get `convbin`/`rnx2rtkp` when they are missing (default `source`). |
| `--render-unit` / `--print-env` | Print the unit or the fresh `.env` that would be written, and exit. |

`./uninstall.sh` (also takes `--dry-run`) stops and disables the service and removes the unit
and the udev rule. It keeps `data/`, `.env`, `.venv`, your `dialout` membership and RTKLIB in
`/usr/local/bin`.

</details>

### From source

To run from a clone without systemd, create a `.env` with a data directory you can write to.
The template's `DATA_DIR=/data` is the container path.

```bash
uv sync
pnpm --dir web install && pnpm --dir web build:static   # pnpm 11; the UI the daemon serves
cp .env.example .env
sed -i "s|^DATA_DIR=.*|DATA_DIR=$PWD/data|" .env
$EDITOR .env                       # set NTRIP_PASSWORD (and ROLE for a rover)
uv run mtrtk doctor
uv run mtrtk base                  # or: mtrtk rover / mtrtk run (role from ROLE)
```

### Raspberry Pi and Jetson

- Use a 64-bit OS. There are no 32-bit (`armhf`) images, and `install.sh` targets Raspberry Pi OS 64-bit.
- A Pi has no real-time clock. Raw logs rotate and rover sessions are stamped on the receiver's UTC, so a wrong host clock does not misfile data. Still enable NTP: RINEX export names and event logs use host time, and `doctor` warns when the clock is not synchronised (`sudo timedatectl set-ntp true`).
- On a Pi, building RTKLIB from source takes a few minutes. Use `--rtklib apt` to install Debian's stock 2.4.3 instead.
- Jetson with JetPack 6 is Ubuntu 22.04, so `DIALOUT_GID=20` applies there too. The ROS 2 bridge profile (`ROS_DISTRO=humble`) targets JetPack 6.

### Exposure beyond Tailscale (optional)

Two compose profiles publish the station beyond the tailnet. Each starts the normal `mtrtk`
service plus a proxy that reaches it on loopback. Run `mtrtk doctor` afterwards: its `exposure`
check FAILs when a profile would publish the UI without a password.

<details>
<summary><b>Public IP with HTTPS (Caddy)</b></summary>

<br>

```bash
# .env
PUBLIC_DOMAIN=rtk.example.com      # required: the caddy container refuses to start without it
ACME_EMAIL=you@example.com         # optional Let's Encrypt / ZeroSSL contact
WEB_BIND=127.0.0.1                 # or lan; Caddy proxies to 127.0.0.1:WEB_PORT
WEB_PASSWORD=<a strong password>   # required: nothing else protects the UI here
NTRIP_BIND=lan                     # so a forwarded 2101 reaches the caster

docker compose --profile public up -d
```

- **Port forwarding:** forward TCP 443 to this host, plus TCP 80 for the redirect to HTTPS. Caddy does not proxy NTRIP, so forward TCP 2101 as well.
- **Certificate:** Caddy obtains and renews it itself and keeps it in the `caddy_data` volume.
- **Hardening in `docker/Caddyfile`:** HSTS is sent, the admin API is off, and responses are compressed.

</details>

<details>
<summary><b>Cloudflare Tunnel</b></summary>

<br>

```bash
# .env
TUNNEL_TOKEN=<token of a remotely managed tunnel>   # Zero Trust -> Networks -> Tunnels
WEB_BIND=127.0.0.1                                  # or lan
NTRIP_BIND=127.0.0.1                                # or lan
WEB_PASSWORD=<a strong password>
# TUNNEL_METRICS_PORT=20246                         # only if a host cloudflared holds 20241-20245

docker compose --profile cloudflare up -d
```

Set the tunnel's public hostnames in Zero Trust (use your own `WEB_PORT`/`NTRIP_PORT` if you
changed them):

| Hostname | Service |
|---|---|
| `rtk.<domain>` | `http://127.0.0.1:8080` |
| `ntrip.<domain>` | `http://127.0.0.1:2101` |

- **NTRIP v2 over HTTPS only.** The tunnel carries HTTP, not raw TCP, so v1 clients such as `str2str` and u-center cannot use it.
- **Cloudflare Access instead of a password.** Put an Access policy on the hostname and set `WEB_BIND=127.0.0.1` with `WEB_ALLOW_INSECURE=1`. Never combine `WEB_ALLOW_INSECURE=1` with `WEB_BIND=lan`.

</details>

Check either profile from outside your network:

```bash
scripts/check-exposure.sh https://rtk.<domain> https://ntrip.<domain>/MTRK rover <password>
```

#### `WEB_PASSWORD` rules

| `WEB_BIND` | `WEB_PASSWORD` empty |
|---|---|
| `tailscale` | Allowed. |
| anything else | Startup fails unless `WEB_ALLOW_INSECURE=1`. |
| `lan` + `WEB_ALLOW_INSECURE=1` | Starts; `doctor` warns. |
| `all`, or a set `PUBLIC_DOMAIN` | `doctor` FAILs, even with `WEB_ALLOW_INSECURE=1`. |
| a set `TUNNEL_TOKEN` | `doctor` FAILs unless `WEB_ALLOW_INSECURE=1`; then it warns that only Cloudflare Access protects the UI. |

Without a password the UI also refuses any `Host` name it was not given (DNS-rebinding protection): it answers IP addresses, `localhost`, this host's names, `PUBLIC_DOMAIN`, any `*.ts.net` MagicDNS name when `WEB_BIND=tailscale`, and the names in `WEB_ALLOWED_HOSTS` (comma-separated; a leading `.` allows a whole domain, `*` turns the check off). A LAN DNS name or a Cloudflare Access hostname goes in `WEB_ALLOWED_HOSTS`.

### Connecting rovers

```
ntrip://<NTRIP_USER>:<NTRIP_PASSWORD>@<base-tailscale-ip>:2101/<MOUNTPOINT>
ntrip://rover:<password>@100.x.y.z:2101/MTRK          # defaults
```

The caster serves NTRIP v1 and v2 on the same port, and the client picks the version. `GET /`
returns the sourcetable without authentication, so an app's "browse mountpoints" button works.

| Client | How |
|---|---|
| RTKLIB `str2str` (v1) | `str2str -in ntrip://rover:<pw>@<base-ip>:2101/MTRK -out serial://ttyUSB0:115200` |
| Phone and field apps (v2): SW Maps, Lefebure, Emlid | Host `<base-ip>`, port `2101`, mountpoint `MTRK`, user `rover`, your password. With the default `NTRIP_BIND=tailscale`, the phone must be on the tailnet. |
| An mtrtk rover | `ROLE=rover` and `NTRIP_URL=ntrip://rover:<pw>@<base-ip>:2101/MTRK` in the rover's `.env`, then `docker compose up -d` or `uv run mtrtk rover`. See [`docs/rover.md`](docs/rover.md). |

### Remote receiver over Tailscale

The receiver can be plugged into another machine on the tailnet: relay its serial port with
`socat` and point `MTRTK_SOURCE` at the local end.

<details>
<summary><b>Commands and caveats for a relayed receiver</b></summary>

<br>

```bash
# On the PC with the F9P (pick any free port; bind to its Tailscale address):
socat TCP-LISTEN:5001,bind=<remote-tailscale-ip>,reuseaddr FILE:/dev/ttyACM0,b115200,raw,echo=0

# On the mtrtk host: a pseudo-terminal that tunnels to it
mkdir -p ~/dev
socat PTY,link=$HOME/dev/f9p,raw,echo=0 TCP:<remote-host>:5001
```

```bash
# mtrtk host .env
MTRTK_SOURCE=/home/<user>/dev/f9p   # an absolute path; auto-detect only finds local USB devices
RECEIVER_ACK_TIMEOUT_S=5            # default 2.0, range 0.5-30; raise it for a relayed link
```

- **Native only.** This works with the native install or `uv run`, not in the container. The compose cgroup rules allow only `ttyACM*`/`ttyUSB*`, and `$HOME` is not mounted.
- **The baud setting is ignored on a pseudo-terminal.** The line speed is set by the far end's `b115200`.
- **Keep both `socat` processes running.** When the link drops, the PTY disappears and the daemon reconnects with backoff once it comes back. Run the two `socat` commands under a supervisor or a restart loop.
- **Stop ModemManager from grabbing the port on the remote PC.** Install `udev/99-mtrtk-ublox.rules` there. The local `doctor` reports ModemManager as "not relevant" for a non-USB source.
- **Stop gpsd from holding the receiver on the remote PC.** The Debian/Ubuntu `gpsd` package's udev rule hands every newly plugged u-blox to gpsd, so stopping it once does not last: `sudo systemctl stop gpsd.socket gpsd.service && sudo systemctl mask gpsd.socket gpsd.service`.
- **One client per forwarded port.** Leave `fork` off the `TCP-LISTEN`, and let only the daemon read the PTY: two programs talking to one receiver corrupt each other's replies. Stop the daemon before `doctor --probe` or u-center, or forward a second port.
- **A relayed link is slower.** Over a Tailscale DERP relay (`tailscale status` shows `relay`), answers can stall for seconds. The daemon retries probes and readbacks, and `RECEIVER_ACK_TIMEOUT_S=5` keeps configuration from timing out. A longer stall reconnects: once the receiver is configured, the daemon logs `reconnecting in 1s`, configures it again and carries on rather than exiting.

</details>

### Running without hardware (replay)

`mtrtk replay` plays a recorded `.ubx` file as if it were a live receiver, with the full UI and
API. Nothing is sent to the file, no position-mode manager runs, raw logs are not written unless
`REPLAY_LOG=1`, and the caster runs anonymously.

Use **`tests/fixtures/f9p_hpg113_base_30s.ubx`**. It carries NAV-EOE, which closes every epoch,
and the MON-*, survey-in and RTCM messages the base pages draw. The `f9p_hpg113_raw_10s.ubx` and
`f9p_hpg113_raw_60s.ubx` fixtures (and hourly logs written before NAV-EOE was logged) have no
NAV-EOE: the epoch ends are inferred from the NAV-* iTOW, so they replay too, with fewer panels.
The file is streamed, so a recording of any size plays every frame: one of the daemon's own
hourly logs (`DATA_DIR/ubx/YYYY/DDD/*.ubx`, 6-9 MB an hour at 1 Hz) or several hours joined with
`cat`.

```bash
# From source, no Tailscale and no .env needed (build the UI first, see "From source"):
DATA_DIR=/tmp/mtrtk-demo WEB_BIND=127.0.0.1 WEB_ALLOW_INSECURE=1 NTRIP_BIND=127.0.0.1 \
  uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --loop     # --speed 0 = as fast as possible
# then open http://127.0.0.1:8080
```

With Docker, copy the fixture into the data volume and point the source at it:

```bash
mkdir -p data && cp tests/fixtures/f9p_hpg113_base_30s.ubx data/
# .env: MTRTK_SOURCE=file:/data/f9p_hpg113_base_30s.ubx  and  REPLAY_LOOP=1
docker compose up -d
```

[`docs/rover.md`](docs/rover.md) shows a full base-plus-rover pair on the same fixture.

### `mtrtk doctor`

```bash
uv run mtrtk doctor                     # native, or .venv/bin/mtrtk doctor
docker compose exec mtrtk mtrtk doctor  # in the container (runs as the daemon's uid 1000)
mtrtk doctor --json                     # machine-readable
mtrtk doctor --probe                    # also polls the receiver's firmware (MON-VER); stop the daemon first
```

Each line is `[OK  ]`, `[WARN]`, `[FAIL]` or `[INFO]`, followed by a `fix:` hint when there is
one. The command exits 1 if any check FAILs; warnings alone exit 0.

<details>
<summary><b>Every check <code>doctor</code> runs</b></summary>

<br>

| Check | What it looks at |
|---|---|
| `config` | Whether `.env` validates. If the base's `NTRIP_PASSWORD` is missing, it still runs the remaining checks. |
| `python` | Python 3.12 or later. |
| `receiver` / `firmware` | The port exists and is readable and writable (USB serial, tty or pty), or the replay file exists. With `--probe`, the firmware version. |
| `ins_port` | INS rovers: `INS_PORT` exists and is readable and writable. |
| `ins_baud` | INS rovers: warns when `INS_BAUD` is too low for raw GNSS capture or a high `INS_OUTPUT_HZ`. |
| `ins_rtcm` | Ellipse rovers with `NTRIP_URL` and no `INS_RTCM_PORT`: warns that RTCM on the main port is unverified. |
| `modemmanager` | Whether ModemManager is running and whether the udev ignore rule is present. |
| `time_sync` | Whether the host clock is NTP-synchronised. |
| `tailscale` | A `tailscale0` IPv4 address, when a bind needs one. |
| `ports` | Whether `NTRIP_PORT`/`WEB_PORT` (and the rover's NMEA port) are free or held by mtrtk. |
| `rtklib` | `convbin` and `rnx2rtkp` on `PATH`. |
| `docker` | Docker version (informational). |
| `data_dir` | `DATA_DIR` is writable and has at least `MIN_FREE_GB` free. |
| `exposure` | Anything reachable beyond Tailscale without a password; Caddy or cloudflared unable to reach a tailnet-only bind. |

</details>

## Workflows

| Workflow | In short | Guide |
|---|---|---|
| **Base with a PPP site** | Survey-in, log 24 h, export RINEX for CSRS-PPP from the Site page, import the result and activate it. The 1005 check then confirms the broadcast position. A known position can skip PPP with `mtrtk sites add NAME --ecef X Y Z`. | [`docs/ppp-workflow.md`](docs/ppp-workflow.md), [`docs/base.md`](docs/base.md) |
| **Rover with NTRIP and NMEA** | `ROLE=rover` and `NTRIP_URL`, then feed QGIS through gpsd, SW Maps over TCP 10110, UDP listeners, a serial port or a pty. Collect averaged points on the Survey page. | [`docs/rover.md`](docs/rover.md) |
| **PPK against the base** | From the PPK page or `mtrtk ppk`: pick the rover data, a base source and the base position. The outputs are a track coloured by quality, a summary and geotagged camera events. | [`docs/ppk.md`](docs/ppk.md) |
| **ROS 2 bridge** | `ROS_DISTRO=humble docker compose --profile ros2 up -d`, then `ros2 topic echo /mtrtk/fix`. It publishes NavSatFix, velocity, time reference, RTK status, time marks, and IMU and heading on INS rovers. | [`docs/ros2.md`](docs/ros2.md) |
| **SBG Ellipse-D as the rover** | `ROVER_DRIVER=sbg_ellipse` and `INS_PORT`. Run `mtrtk ins info` and `mtrtk ins config --dry-run` before you apply anything. Its raw GNSS is logged as hourly `.ubx` for RINEX and PPK. | [`docs/ins-drivers.md`](docs/ins-drivers.md) |

The same steps from the command line:

```bash
# Base: a day of raw logs to RINEX, then the PPP result in as the active site
uv run mtrtk export --preset csrs-ppp --from 2026-09-19T00:00Z --to 2026-09-20T00:00Z --out /tmp/csrs
uv run mtrtk ppp-import result.zip --save-site roof-ppp --activate   # then set BASE_MODE=fixed in .env

# Rover: PPK a session against a remote mtrtk base
uv run mtrtk ppk --session 3 --base-url http://100.100.50.10:8080 --out ./ppk-2026-09-19
```

## Configuration

All configuration is environment variables. mtrtk reads them from `.env` in the working directory
(`/data/.env` in the Docker image), and real environment variables take precedence over the file.
Start from the annotated template, `cp .env.example .env`.

Each variable is a field of `Settings` in [`src/mtrtk/config.py`](src/mtrtk/config.py), with the
name in upper case. Booleans take `1`/`0`. For most optional keys an empty value (`KEY=`) means
"unset"; `NTRIP_PASSWORD` is the exception (see the [quick start](#quick-start-docker-compose)).
The Settings page writes changes back to `.env` through `PUT /api/config`. `BASE_MODE`,
`SVIN_MIN_DURATION_S`, `SVIN_ACC_LIMIT_M` and `ACTIVE_SITE` apply live. Every other key needs a
restart. At startup, a base must have `NTRIP_PASSWORD`, and the
[`WEB_PASSWORD` rules](#web_password-rules) are enforced.

<details>
<summary><b>Core and receiver</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `ROLE` | `base` | `base` or `rover` |
| `MTRTK_SOURCE` | `auto` | `auto` (find a u-blox receiver), a serial device path, or `file:<path>` to replay a capture |
| `BAUD` | `115200` | Receiver serial line speed |
| `DATA_DIR` | `/data` | Raw logs, exports, jobs and the SQLite database |
| `MTRTK_ENV_FILE` | `.env` | File the web UI rewrites when settings change (read-only over the API) |
| `STATION_ID` | `MTRK` | Exactly 4 characters `A-Z0-9`; names log files and the RINEX marker |
| `COUNTRY` | `BGD` | ISO 3166-1 alpha-3 code used in RINEX 3 file names |
| `MARKER_NAME` / `OBSERVER` / `AGENCY` | `MTRK` / `mtrtk` / `mtrtk` | RINEX header fields |
| `ANTENNA_TYPE` | `NONE` | IGS antenna code (`NONE` for an uncalibrated antenna) |
| `ANTENNA_HEIGHT_M` | `0.0` | ARP height above the mark, metres |
| `RECEIVER_STRICT` | `1` | `1`: a core config key the receiver rejects stops startup with exit 1. `0`: log a warning and keep running |
| `RECEIVER_ACK_TIMEOUT_S` | `2.0` | Wait for each receiver poll or config reply, 0.5–30 s. Raise it for a receiver reached over a slow tunnel |
| `REPLAY_SPEED` / `REPLAY_LOOP` / `REPLAY_LOG` | `1.0` / `0` / `0` | File-source pacing (`0` = as fast as possible), restart at EOF, and write raw logs while replaying |

</details>

<details>
<summary><b>Base station</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `BASE_MODE` | `survey-in` | `survey-in`, `fixed` (uses `ACTIVE_SITE`) or `off` |
| `SVIN_MIN_DURATION_S` | `300` | Minimum survey-in duration, 1–86400 s |
| `SVIN_ACC_LIMIT_M` | `2.0` | Survey-in accuracy target, >0–100 m |
| `ACTIVE_SITE` | unset | Name of the saved site used when `BASE_MODE=fixed` |
| `RTCM_MSM` | `7` | `7` (1077/1087/1097/1127) or `4` (1074/1084/1094/1124) |
| `RTCM_1230_RATE` | `5` | Seconds between RTCM 1230 GLONASS bias messages |
| `RTCM_STATION_ID` | `0` | RTCM reference station ID (DF003), 0–4095 |

</details>

<details>
<summary><b>NTRIP caster (base)</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `NTRIP_BIND` | `tailscale` | `tailscale`, `lan`, `all` or an IP address |
| `NTRIP_PORT` | `2101` | Caster port |
| `MOUNTPOINT` | `MTRK` | Mountpoint name |
| `NTRIP_USER` | `rover` | Caster username |
| `NTRIP_PASSWORD` | unset | Required for a base (empty means anonymous) |
| `NTRIP_MAX_CLIENTS` | `32` | Rovers served at once. Clients past the limit are refused |

</details>

<details>
<summary><b>Web UI and API</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `WEB_BIND` | `tailscale` | `tailscale`, `lan`, `all` or an IP address |
| `WEB_PORT` | `8080` | UI, API and WebSocket port |
| `WEB_PASSWORD` | unset | Login password; see the [rules](#web_password-rules) |
| `WEB_ALLOW_INSECURE` | `0` | `1` accepts an unauthenticated UI on a non-Tailscale bind |
| `WEB_ALLOWED_HOSTS` | empty | Extra host names a password-less UI answers to (`.domain` for a whole domain, `*` = off) |

</details>

<details>
<summary><b>Raw logging, retention and process</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `LOG_MESSAGES` | `RXM-RAWX,RXM-SFRBX,NAV-PVT,NAV-HPPOSLLH,NAV-SVIN,TIM-TM2,MON-VER,NAV-EOE` | UBX messages kept in the hourly raw logs |
| `MIN_FREE_GB` | `5.0` | Below this much free disk, the oldest raw logs are pruned and an alert is raised |
| `FSYNC_INTERVAL_S` | `10` | How often the raw-log writer fsyncs |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. `mtrtk -v` forces `DEBUG` |

</details>

<details>
<summary><b>Rover</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `ROVER_DRIVER` | `ublox` | `ublox` (ZED-F9P), `sbg_ellipse` or `vectornav` |
| `ROVER_NAV_HZ` | `5` | Navigation rate, 1–8 Hz |
| `ROVER_DYNMODEL` | `portable` | `portable`, `stationary`, `pedestrian`, `automotive`, `airborne1g`, `airborne2g` or `airborne4g` |
| `NTRIP_URL` | unset | Correction source, `ntrip://user:pass@host:2101/MOUNT` (`http://` also accepted) |
| `NTRIP_GGA_INTERVAL_S` | `10` | Seconds between GGA uploads to the caster, 0–3600 (`0` sends none) |
| `POINT_EPOCHS` | `30` | Default number of epochs averaged per survey point, 1–3600 |
| `POINT_FIXED_ONLY` | `1` | Count only RTK-fixed epochs toward a point |

</details>

<details>
<summary><b>NMEA and JSON outputs (rover)</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `NMEA_TCP_PORT` | `10110` | NMEA TCP server, which has no password. `-1` turns it off |
| `NMEA_TCP_BIND` | `lan` | `lan`/`all` = every interface, `tailscale`, or an IP address |
| `NMEA_TCP_MAX_CLIENTS` | `16` | Further connections are closed immediately |
| `NMEA_SENTENCES` | `GGA,RMC,GST,GSA,GSV,VTG,ZDA` | Any of `GGA,RMC,GST,GSA,GSV,VTG,ZDA,HDT,PASHR` (HDT/PASHR need attitude). An unknown name is a startup error |
| `NMEA_SLOW_INTERVAL_S` | `1.0` | Period of the GSA/GSV/ZDA bursts, seconds |
| `NMEA_UDP_TARGETS` | empty | Comma-separated `host:port` list to send NMEA over UDP |
| `NMEA_SERIAL` / `NMEA_SERIAL_BAUD` | unset / `115200` | Serial port to mirror NMEA to, and its own line speed (not `BAUD`) |
| `JSON_UDP_PORT` | unset | UDP port on `127.0.0.1` for a JSON state feed. Empty = off |

</details>

<details>
<summary><b>PPK</b></summary>

<br>

PPK has no settings of its own. `mtrtk ppk` and the PPK page use `DATA_DIR`, `STATION_ID` and
`MIN_FREE_GB` from above. On the PPK page, a set `NTRIP_URL` prefills the remote base as
`http://<host>:8080`. The CLI has no such default and always needs `--base`, `--base-url` or
`--base-logs`. `mtrtk ppk --base-password` also reads `MTRTK_BASE_PASSWORD`. See
[`docs/ppk.md`](docs/ppk.md).

</details>

<details>
<summary><b>INS rovers (<code>ROVER_DRIVER=sbg_ellipse</code> or <code>vectornav</code>)</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `INS_PORT` | unset | The unit's serial device. Required, because there is no auto-detect (unless replaying a `file:` source) |
| `INS_BAUD` | `115200` | The unit's port rate. mtrtk never changes it, so set it in the vendor tool first |
| `INS_RTCM_PORT` / `INS_RTCM_BAUD` | unset / `INS_BAUD` | SBG only: a separate device carrying RTCM to Port B, and its rate |
| `INS_OUTPUT_HZ` | `10` | Navigation output rate, 1–200 Hz |
| `INS_APPLY_CONFIG` | `0` | `1` writes the vendor config subset on connect and saves it to flash |
| `INS_RAW_GNSS` | `1` | Capture the unit's raw GNSS stream for PPK |
| `INS_LEVER_ARM_GNSS1` / `INS_LEVER_ARM_GNSS2` | unset | IMU-to-antenna lever arm, `x,y,z` metres (X forward, Y right, Z down) |
| `INS_IMU_LEVER_ARM` | unset | SBG only: IMU lever arm, `x,y,z` metres |
| `INS_IMU_AXIS` | `xyz` | SBG axis mapping. `xyz` leaves it as is |
| `INS_MOTION_PROFILE` | not managed | SBG: `general`, `automotive`, `marine`, `airplane`, `helicopter`, `uav` or `pedestrian`. Applied only when set explicitly |
| `INS_INIT_POSITION` | unset | Initial filter position, `lat,lon,alt` (degrees, metres) |
| `INS_VN_RTCM` | `0` | VectorNav: forward RTCM to the VN-200 (unverified, so opt-in) |
| `INS_VN_SCENARIO` / `INS_VN_AHRS_AIDING` | unset | VectorNav register 67. Unset leaves the unit's value |
| `INS_VN_REF_ROTATION` | unset | VectorNav register 26: 9 comma-separated floats, row-major |
| `INS_VN_VPE` | unset | VectorNav register 35: `enable,headingMode,filteringMode,tuningMode` |

[`docs/ins-drivers.md`](docs/ins-drivers.md) covers wiring and how each key maps to the vendor
setting.

</details>

<details>
<summary><b>Alerts and exposure</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `ALERT_WEBHOOK_URL` | unset | Each alert is POSTed here as JSON: `level`, `kind`, `message`, `ts`, `host`, `role` |
| `PUBLIC_DOMAIN` | unset | Hostname of the web UI for the `public` (Caddy) compose profile, e.g. `rtk.example.com` |
| `TUNNEL_TOKEN` | unset | Cloudflare tunnel token for the `cloudflare` compose profile. `mtrtk doctor` reads it too |
| `ACME_EMAIL` | unset | Compose only: optional Let's Encrypt/ZeroSSL contact for Caddy |
| `TUNNEL_METRICS_PORT` | `20241` | Compose only: cloudflared's loopback metrics port |
| `DIALOUT_GID` | `20` | Compose only: host group that owns the serial devices, added to the container user |

How to use the two profiles is under [Exposure beyond Tailscale](#exposure-beyond-tailscale-optional).

</details>

## Command line

```text
mtrtk [-v] [--version] COMMAND [ARGS]...
```

`-v` / `--verbose` turns on debug logging and goes before the command (`mtrtk -v base`). Every
command accepts `-h` / `--help`.

| Command | What it does | Useful flags |
|---|---|---|
| `mtrtk run` | Run the daemon in the role given by `ROLE` | |
| `mtrtk base` / `mtrtk rover` | Run as a base station or a rover | |
| `mtrtk replay FILE` | Replay a recorded `.ubx` stream as a live receiver; no configuration is sent | `--speed` (default 1.0, 0 = max), `--loop` |
| `mtrtk record` | Record the raw receiver byte stream to a file | `--out FILE` (required), `--port`, `--baud`, `--seconds` (default 60) |
| `mtrtk doctor` | Check receiver access, host services, Tailscale, ports, RTKLIB, disk and exposure. Exits 1 on any FAIL | `--json`, `--probe` |
| `mtrtk healthcheck` | Exit 0 when the local `/healthz` answers (the container healthcheck) | |
| `mtrtk sites list\|add\|activate\|delete` | Manage saved base sites. A running base applies a newly activated site within 10 s | `add NAME --ecef X Y Z` or `--llh LAT LON H` |
| `mtrtk export` | Export a raw-log window as RINEX | `--from`, `--to`, `--out DIR`, `--preset` |
| `mtrtk ppp-import FILE` | Read a PPP result (CSRS-PPP `.sum`/`.pos`/`.zip`, AUSPOS SINEX, OPUS) | `--save-site NAME`, `--activate` |
| `mtrtk ppk` | Post-process rover raw data against a base with RTKLIB | `--out DIR`, a rover source, a base source |
| `mtrtk ins info\|config\|monitor` | Inspect, compare or configure an INS unit (daemon stopped) | `config --dry-run\|--apply` |
| `mtrtk backup` / `mtrtk restore ARCHIVE` | Archive and restore the database, sites and a masked `.env` | `backup --out FILE`, `restore --force` |

<details>
<summary><b>The rest of the flags</b></summary>

<br>

- **`mtrtk sites add NAME`:** `--sigma`, `--frame` (default `ITRF2020`), `--epoch`, `--source`, `--notes`. `mtrtk sites list` marks the active site with `*`.
- **`mtrtk export`:** `--from` and `--to` are ISO-8601 with a timezone, and `--from`, `--to` and `--out` are all required. `--preset csrs-ppp|auspos|opus|generic` (default `csrs-ppp`), `--overwrite`. Generic only: `--interval`, `--hatanaka/--no-hatanaka`, `--gzip/--no-gzip`.
- **`mtrtk ppp-import`:** `--prefer-frame itrf|nad83` picks the frame from an OPUS report.
- **`mtrtk ppk`:** rover from `--rover FILE`, `--session ID` or `--from/--to`. Base from `--base FILE` (+ `--base-nav`), `--base-url URL` (+ `--base-password`) or `--base-logs`. Base position from `--site NAME` or `--base-xyz X Y Z`. Also `--no-events`, `--qzss` and `--set key=value` (repeatable rnx2rtkp overrides).
- **`mtrtk ins`:** these commands open `INS_PORT` exclusively, so run them with the daemon stopped. `config --apply` saves to flash only with `INS_APPLY_CONFIG=1`. `monitor --seconds N` (0 = until Ctrl-C).
- **`mtrtk backup`:** safe while the daemon runs; `--with-secrets` keeps the `.env` unmasked. **`mtrtk restore`:** stop the daemon first; `--force` overwrites the database and keeps a copy.
- **`mtrtk doctor --probe`:** polls the receiver's firmware, so stop the daemon first.

</details>

## HTTP API

The daemon serves a REST API and a WebSocket (`/ws`) on the same port as the UI, with
interactive docs at `/api/docs`. Auth is active only when `WEB_PASSWORD` is set: `POST
/api/login` returns a token and sets a cookie. Liveness is `GET /healthz`, which returns
`{"status":"ok","role":"base","connected":true,"passive":false}`. Besides `/api/login`,
`/healthz` is the only API route that needs no auth. [`docs/api.md`](docs/api.md) documents every
route, status code and WebSocket message.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/setup.md`](docs/setup.md) | Install, pinning image tags and updating, in Docker and natively |
| [`docs/hardware.md`](docs/hardware.md) | Receivers, antennas, cabling, host boards and storage |
| [`docs/exposure.md`](docs/exposure.md) | Tailscale, public IP with Caddy TLS, Cloudflare Tunnel, remote receivers |
| [`docs/firmware.md`](docs/firmware.md) | ZED-F9P firmware versions and upgrading |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Symptoms, causes and fixes |
| [`docs/acceptance.md`](docs/acceptance.md) | Fresh-host acceptance checklist (what is verified, what needs a real Pi) |
| [`docs/base.md`](docs/base.md) | Base station: survey-in, fixed sites, the 1005 check, the caster |
| [`docs/ppp-workflow.md`](docs/ppp-workflow.md) | From a 24 h log to a PPP-surveyed fixed site |
| [`docs/rover.md`](docs/rover.md) | Rover role: NTRIP client, NMEA/JSON outputs, sessions and points |
| [`docs/ppk.md`](docs/ppk.md) | Post-processing with RTKLIB and camera events |
| [`docs/ins-drivers.md`](docs/ins-drivers.md) | SBG Ellipse-D and VectorNav VN-200: wiring, configuration, verification matrix |
| [`docs/ros2.md`](docs/ros2.md) | The ROS 2 bridge, topics, parameters and native builds |
| [`docs/ui.md`](docs/ui.md) | The web UI, page by page |
| [`docs/api.md`](docs/api.md) | REST routes, status codes and the WebSocket protocol |
| [`docs/architecture.md`](docs/architecture.md) | The full component diagram |
| [`docs/superpowers/specs/2026-09-18-mtrtk-design.md`](docs/superpowers/specs/2026-09-18-mtrtk-design.md) | The design specification |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Commit conventions, checks and the release process |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |

## Development

You need Python 3.12 with [uv](https://docs.astral.sh/uv/), Node 22 with pnpm 11, and RTKLIB
(`convbin`, `rnx2rtkp`) for the export and PPK tests. A receiver is optional: the committed UBX
fixtures replay through the whole daemon and UI.

```bash
uv sync
pnpm --dir web install
WEB_BIND=127.0.0.1 WEB_ALLOW_INSECURE=1 NTRIP_BIND=127.0.0.1 DATA_DIR=/tmp/mtrtk-dev \
  uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --speed 10 --loop
pnpm --dir web dev          # UI on :5173, proxies /api, /healthz and /ws to :8080
```

CI runs these checks:

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -q
pnpm --dir web lint && pnpm --dir web build && pnpm --dir web test
```

- `pnpm --dir web build:static` builds the SPA and copies it into `src/mtrtk/web/static`, which the daemon serves. The Docker image builds the UI in its own stage.
- Tests that need RTKLIB skip when it is missing. `MTRTK_REQUIRE_CONVBIN=1` makes them fail instead, and CI sets it.
- Tests in `tests/hardware/` talk to a live receiver and **reconfigure it**. They carry the `hardware` marker and are excluded by default (`-m 'not hardware'`). Run them with `MTRTK_TEST_PORT=/dev/ttyACM0 uv run pytest -m hardware tests/hardware`, and never against a receiver that is serving a station.
- After changing a backend route, regenerate the OpenAPI snapshot with `uv run python web/scripts/gen_openapi_snapshot.py`.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for commit conventions and the release process.

## Project status

mtrtk is pre-release: no `v0.1.0` yet. The base station is verified live on an F9P with HPG 1.13.
The rover, PPK and INS features are built and tested, but parts of them have only run on replays
or sample files. The list below says which.

<details>
<summary><b>Development phases</b></summary>

<br>

| Phase | Scope | State |
|---|---|---|
| 0–1 | Scaffold, receiver core, replay and record | Complete |
| 2 | Base daemon: raw logging and retention, NTRIP caster, survey-in and fixed sites with the 1005 check, history, alerts | Complete |
| 3 | Web API: FastAPI, WebSocket hub, `.env` write-back, jobs, optional login | Complete |
| 4 | Operator web UI | Complete |
| 5 | RINEX export and PPP import | Code complete; the real 24 h CSRS-PPP round trip is pending |
| 6 | F9P rover: NTRIP client, NMEA/JSON outputs, sessions, survey points | Complete; live RTK pending a second receiver |
| 7 | ROS 2 bridge (Humble, Jazzy) | Complete |
| 8 | PPK with RTKLIB, camera events, PPK page | Code complete; the fix-rate milestone is open (see below) |
| 9 | Exposure and hardening: `doctor`, backup and restore, native install, the Caddy and Cloudflare profiles, a non-root container, the release pipeline | Complete; the fresh-host run on a real Pi and the live Cloudflare check are pending ([checklist](docs/acceptance.md)) |
| 10 | INS drivers: SBG Ellipse-D, VectorNav VN-200 | Complete; SBG verified live, read-only |

</details>

### Known limitations

- **F9P rover live RTK is not field-tested.** The rover role is verified on replay, against a replayed base caster. `tests/hardware/test_live_rover.py` has not run on hardware, and an RTK fix needs a second receiver.
- **The PPP round trip is unconfirmed.** No 24 h export has been submitted to CSRS-PPP, AUSPOS or OPUS yet. The result importers are checked against the services' real published outputs (NRCan's CSRS-PPP samples, a Geoscience Australia AUSPOS SINEX, three OPUS reports), not yet against a result for this station. OPUS acceptance of the F9P's L2C observations is unknown. The antenna's ANTEX calibration has not been checked, so keep `ANTENNA_TYPE=NONE`.
- **The PPK fix-rate milestone is unmet.** A same-file zero-baseline run stays 100 % float at millimetre level, because rnx2rtkp never attempts to fix all-zero ambiguities. CI asserts fixed + float ≥ 99 % instead. Measuring a real fix rate needs a splitter capture or a real baseline.
- **HPG 1.51 has not met hardware.** Support comes from the capability probe; the only receiver tested so far runs HPG 1.13.
- **SBG Ellipse-D is verified read-only only.** Framing, logs, GPS1_RAW to RINEX and the live UI are verified. Configuration writes, RTCM input (Port A or Port B) and RTK on the unit are not.
- **VectorNav VN-200 is spec-only.** It is built from the vendor documentation and has never met a unit. Its RTCM input is opt-in and unverified, and its RawMeas capture (`.vnraw`) has no RINEX converter.
- **Tunnelled or relayed receiver links can stall.** On a receiver reached through a socat PTY over a Tailscale relay, multi-second stalls make ACK/poll timeouts trigger reconnects. A running daemon reconnects instead of exiting. Raise `RECEIVER_ACK_TIMEOUT_S` (default 2.0, for example 5) for such links.
- **A survey-in will not validate indoors.** It is a receiver limit, not a software one: put the antenna under open sky, or use a known fixed site.

## License

mtrtk is released under the [MIT License](LICENSE). Copyright (c) 2026 Mollah Md Saif.
