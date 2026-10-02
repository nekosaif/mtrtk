<div align="center">

# mtrtk

**An RTK base station, rover and post-processing toolkit for the u-blox ZED-F9P, with its own NTRIP caster and a live web UI.**

[![CI](https://github.com/nekosaif/mtrtk/actions/workflows/ci.yml/badge.svg)](https://github.com/nekosaif/mtrtk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-linux%2Famd64%20%7C%20linux%2Farm64-555.svg?logo=linux&logoColor=white)](docker/Dockerfile)

[Features](#features) ·
[Screenshots](#screenshots) ·
[Quick start](#quick-start-docker-compose) ·
[Workflows](#workflows) ·
[Configuration](#configuration) ·
[Docs](#documentation)

<br>

<img src="docs/images/ui-dashboard.png" alt="mtrtk dashboard on a base station: position, sky plot, map, fix, satellites by system, RTCM output and host health" width="100%">

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

- [Features](#features)
- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [Supported hardware](#supported-hardware)
- [Setup](#setup)
  - [Requirements](#requirements)
  - [Quick start (Docker Compose)](#quick-start-docker-compose)
  - [Native install (systemd)](#native-install-systemd)
  - [Raspberry Pi and Jetson](#raspberry-pi-and-jetson)
  - [Exposure beyond Tailscale](#exposure-beyond-tailscale-optional)
  - [Connecting rovers](#connecting-rovers)
  - [Remote receiver over Tailscale](#remote-receiver-over-tailscale)
  - [Running without hardware (replay)](#running-without-hardware-replay)
  - [`mtrtk doctor`](#mtrtk-doctor)
- [Workflows](#workflows)
- [Configuration](#configuration)
- [Command line](#command-line)
- [HTTP API](#http-api)
- [Documentation](#documentation)
- [Development](#development)
- [Project status](#project-status)
- [License](#license)

## Features

### Receiver (u-blox ZED-F9P)

- Finds the receiver on USB (VID 1546), or uses an explicit `MTRTK_SOURCE`, and looks for it again on every reconnect, so a re-enumerated `ttyACM*` is picked up.
- Reconnects with exponential backoff (1 to 30 s) and a no-data watchdog, then re-applies and re-verifies the profile.
- Sends the profile as `CFG-VALSET` in chunks of at most 64 keys and reads every key back with `CFG-VALGET`. The first configure writes RAM, BBR and flash; reconnects write RAM only.
- Core keys must be ACKed or startup fails. Optional features (MON-SPAN, MON-COMMS, NAV-TIMELS) are probed per firmware and skipped when the receiver NAKs them, which is how one build runs on both HPG 1.13 and 1.51.
- Live state from NAV-PVT/HPPOSLLH/HPPOSECEF/SAT/SIG/DOP/STATUS/CLOCK/TIMEUTC and MON-HW/RF, plus MON-SPAN/COMMS where the firmware has them. That covers position, accuracy, DOPs, per-signal C/N0, jamming and AGC, antenna status, spectrum and firmware.
- Hot, warm, cold and factory resets, message polls and profile re-apply from the UI or API.
- `mtrtk record` captures the live byte stream. `mtrtk replay FILE` runs the whole daemon, UI and API on a recording with no hardware attached.

### Base station

- Survey-in (`SVIN_MIN_DURATION_S`, `SVIN_ACC_LIMIT_M`) or FIXED on a named, saved ECEF site through a verified TMODE3 write. Survey-in can be restarted, and a valid one can be frozen as a site.
- RTCM3 output: MSM7 1077/1087/1097/1127 (MSM4 with `RTCM_MSM=4`), 1005 at 1 Hz and 1230 every 5 s.
- **1005 check:** each broadcast 1005 is decoded and compared with the active site. All three ECEF axes must agree within 0.5 mm, and the result is raised as `site_verified` or `site_mismatch`.
- In-process NTRIP caster, v1 and v2 on one port, with Basic auth or anonymous access, a sourcetable and up to `NTRIP_MAX_CLIENTS` (32) clients.
- A client whose socket backs up by 256 KB for 10 s is dropped. New clients get the cached 1005 and 1230 immediately.
- Every caster connection is logged to SQLite with the client's address, user agent, NTRIP version, bytes sent and the GGA position it reported.
- `mtrtk sites list|add|activate|delete` works against a running daemon. A running base switches to a newly activated site within 10 s.

### Raw logging, history and alerts

- Hourly `.ubx` files named and aligned on the receiver's UTC, not the host clock, under `DATA_DIR/ubx/YYYY/DDD/`.
- Each hour has a JSON sidecar: message counts, size, sha256, firmware, site and a `keep` flag. Hours left open by a crash are finalized at the next start.
- Retention deletes the oldest whole hours that are not marked `keep` when free disk falls below `MIN_FREE_GB`.
- SQLite in WAL mode: 1 s samples kept 24 h, 1 min rollups kept 90 days, caster connection log kept 90 days.
- Alert rules cover receiver gone or erroring, fix lost, jamming, antenna fault, disk low or warning, host temperature, logger backpressure, sampler failing and site mismatch, plus the rover and INS rules below.
- Alerts are written to the events table and, optionally, POSTed as JSON to `ALERT_WEBHOOK_URL` (works with ntfy, Discord and Slack).

### RINEX export and PPP

- Exports any UTC window of raw hours, up to 7 days, as RINEX through RTKLIB `convbin`. Presets: CSRS-PPP, AUSPOS, OPUS (RINEX 2.11, GPS only) and generic. Only generic lets you change the interval, Hatanaka and gzip.
- Runs as a background job (`POST /api/export`, `mtrtk export`) or as a synchronous zip download of up to 6 h. One export runs at a time, and an export is refused before it starts if it would leave less than half of `MIN_FREE_GB` free.
- Imports PPP results: a CSRS-PPP `.sum`/`.pos` (or the `.zip` they arrive in), an AUSPOS SINEX `.snx`, or an OPUS e-mail saved as `.txt`. You see a preview first, then save it as a site and optionally activate it (`mtrtk ppp-import`, or the Site page).

### Rover

- `ROLE=rover` on an F9P runs at `ROVER_NAV_HZ` (1 to 8 Hz, default 5) with a selectable dynamic model.
- The NTRIP client asks as v2 and falls back to v1, uploads GGA every `NTRIP_GGA_INTERVAL_S` (10 s by default), injects RTCM into the receiver and reconnects with its own jittered backoff (1 to 60 s).
- `NTRIP_URL` can be changed from the UI without a restart.
- RTK status from NAV-RELPOSNED and RXM-RTCM: carrier solution, baseline, correction age, and per-message-type count and used.
- NMEA over TCP (port 10110, no password), UDP, a serial device or a pty. Default sentences are GGA, RMC, GST, GSA, GSV, VTG and ZDA; HDT and PASHR are available with an INS.
- JSON over UDP, one object per epoch.
- Sessions stamped on receiver UTC. Survey points average N epochs in ENU (RTK fixed only by default) and export as CSV, GeoJSON, KML or GPX.
- Rover alerts: `ntrip_disconnected`, `corrections_stale`, `rtk_lost`.

### PPK

- `mtrtk ppk` and `POST /api/ppk` run RTKLIB `rnx2rtkp`.
- Rover data comes from a session, a UTC window or an uploaded UBX/RINEX file. Base data comes from another mtrtk base over HTTP, an uploaded file or this host's own logs.
- The base position is taken automatically from the site the base logged at, or from a saved site, or from ECEF X/Y/Z you give it.
- Outputs: `track.pos`, `.csv`, `.geojson` and `.kml` coloured by quality, `summary.json`, the exact `ppk.conf`, and the RINEX files the run used.
- Camera events: TIM-TM2 (EXTINT) pulses are interpolated onto the track and written to `events.csv` and `events.geojson` for geotagging.
- Prefers the RTKLIB demo5 build (v2.5.1, which the Docker image ships) and also works with stock RTKLIB 2.4.3.

### INS drivers

- `ROVER_DRIVER=sbg_ellipse` (sbgECom) or `vectornav` (VN binary output) replaces the F9P on `INS_PORT`. The NTRIP client, outputs, points, UI and ROS bridge work unchanged.
- Read-only by default: `INS_APPLY_CONFIG=0` only queries the unit. `mtrtk ins info`, `mtrtk ins config --dry-run|--apply` and `mtrtk ins monitor` inspect and configure it.
- Attitude and IMU data feed NMEA HDT/PASHR and the ROS `imu` and `heading` topics.
- The Ellipse-D's internal u-blox raw stream is re-framed into the hourly `.ubx` logs, so it can be exported to RINEX. VN-200 RawMeas is saved to an opaque `.vnraw` capture.
- Recorded INS captures can be replayed with `MTRTK_SOURCE=file:`.
- INS alerts: `ins_not_aligned`, `ins_gnss_lost`, `ins_config_mismatch`, `imu_error`.

### ROS 2 bridge

- `mtrtk_bridge` (rclpy) and `mtrtk_msgs` for Humble and Jazzy. The bridge is a WebSocket client of the daemon, so the daemon itself has no ROS dependency, and the bridge can run on another machine.
- Topics: `/mtrtk/fix` (NavSatFix), `vel` (ENU velocity), `time_reference`, `rtk_status`, `time_mark`, `imu` and `heading` (INS rovers only) and `nmea` (optional).
- Docker images through `docker compose --profile ros2`, or a native `colcon build`. The Fast DDS profile is UDP-only, so consumers in other containers get data.

### Web UI

- A React single-page app served by the daemon on the same port as the API, updated live over one WebSocket. It has dark and light themes and switches to a bottom tab bar on a phone.
- Pages on a base: Dashboard, Satellites, Receiver, Corrections, Site, Logs, History, PPK, Events, Settings.
- Pages on a rover: Dashboard, Satellites, Receiver, RTK, Survey, Logs, History, PPK, Events, Settings.
- Optional login.
- Satellites: sky plot, C/N0 bars and a signal table. Receiver: RF blocks, jamming and AGC, spectrum, and INS panels on an INS rover.
- Site: survey-in progress, verification, sites, and a step-by-step PPP flow. Logs: a 48 h availability strip, keep and delete, RINEX export and export jobs.
- History: 1 h to 90 d, one chart per metric. Settings: every `.env` field, with secrets masked.

### API and WebSocket

- FastAPI REST for everything the UI does: status, state, config with `.env` write-back, receiver commands, base mode and sites, PPP import, caster clients, logs, history, events, jobs, export, PPK and rover. Interactive docs are at `/api/docs`.
- `/ws` streams a snapshot, then live updates on 17 topics, for example `pvt`, `sats`, `rtcm`, `rf`, `ntrip`, `jobs`, `rtk`, `survey` and `ins`.
- `/healthz` needs no authentication. `mtrtk healthcheck` calls it and is the container healthcheck.
- Authentication uses a single `WEB_PASSWORD`. The HttpOnly cookie or `Authorization: Bearer` token is derived from the password and is not stored.

### Exposure

- Tailscale by default: `WEB_BIND` and `NTRIP_BIND` default to the `tailscale0` address. The daemon waits for that address and never falls back to `0.0.0.0`.
- Any other bind needs `WEB_PASSWORD`, unless you set `WEB_ALLOW_INSECURE=1`.
- `public` compose profile: Caddy in front of the web UI, with automatic HTTPS on `PUBLIC_DOMAIN` and HSTS. NTRIP is forwarded directly on TCP 2101.
- `cloudflare` compose profile: a remotely managed Cloudflare Tunnel from `TUNNEL_TOKEN`. Through the tunnel NTRIP works only for v2 over HTTPS.
- `scripts/check-exposure.sh` checks `/healthz` and that RTCM3 arrives over NTRIP v2 through the chosen path.

### Operations and hardening

- `mtrtk doctor` checks Python, receiver access, firmware age (with `--probe`), RTKLIB, Tailscale, ModemManager, time sync, port owners, Docker, the data directory and what is exposed. `--json` prints the checks as JSON, and the exit code is 1 only on a FAIL.
- `mtrtk backup` writes the database (a WAL-safe snapshot), the sites as JSON and the `.env` with secrets masked. `mtrtk restore` checks the database before it replaces anything.
- `install.sh` sets up a native install on Debian, Ubuntu or Raspberry Pi OS with systemd. It installs uv and a virtualenv, builds RTKLIB demo5, builds the web UI, writes a `.env`, installs the u-blox udev rule, adds you to `dialout` and installs `mtrtk.service`. `--dry-run` shows what it would do, and `uninstall.sh` reverses it.
- The container's daemon runs as uid 1000 behind an entrypoint that drops privileges. Compose sets `cap_drop: ALL` (adding back only what the entrypoint needs before it drops), `no-new-privileges` and a tmpfs `/tmp`, and caps the logs.
- Serial devices are reached through `/dev` plus cgroup rules for `ttyACM`/`ttyUSB`, so the container is hotplug-safe without `privileged`.
- Multi-arch images (amd64, arm64) on `ghcr.io/nekosaif/mtrtk`, tagged `latest`, `X.Y.Z` and `edge`.
- Every long-running job in the daemon is supervised and restarted with backoff if it fails. On SIGTERM each job gets 15 s to close its file and hang up its clients.

## Screenshots

All screenshots are of the real UI running on replayed receiver data from the repository's test
fixtures.

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
      <p><b>Corrections.</b> RTCM 3 MSM7 output per message, stream bitrate, the built-in NTRIP caster and connected rovers.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-site.png"><img src="docs/images/ui-site.png" alt="Site page" width="100%"></a>
      <p><b>Site.</b> Position mode, survey-in progress, verification, saved ECEF sites with per-axis sigma, a site map and the guided PPP workflow.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-logs.png"><img src="docs/images/ui-logs.png" alt="Logs page" width="100%"></a>
      <p><b>Logs.</b> A 48-hour availability strip, raw-window download, RINEX export (CSRS-PPP preset) with finished export jobs, and the hourly UBX file list.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-history.png"><img src="docs/images/ui-history.png" alt="History page" width="100%"></a>
      <p><b>History.</b> 24 h of horizontal and vertical accuracy and PDOP from real base data, at 1-minute rollups.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-rtk.png"><img src="docs/images/ui-rtk.png" alt="RTK page" width="100%"></a>
      <p><b>RTK (rover).</b> Solution and baseline, the NTRIP client connected to the base caster, correction age, fix timeline, camera time marks and NMEA/JSON outputs.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-survey.png"><img src="docs/images/ui-survey.png" alt="Survey page" width="100%"></a>
      <p><b>Survey (rover).</b> An open session, averaged point collection, collected points with sigma N/E/U, CSV/GeoJSON/KML/GPX export and a map.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-ppk.png"><img src="docs/images/ui-ppk.png" alt="PPK page" width="100%"></a>
      <p><b>PPK.</b> A new RTKLIB run: rover as session, window or upload; base as remote, upload or local logs; base position, camera events and elevation mask; and the jobs list.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-events.png"><img src="docs/images/ui-events.png" alt="Events page" width="100%"></a>
      <p><b>Events.</b> Info and warning alerts with acknowledge and a level filter.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-settings.png"><img src="docs/images/ui-settings.png" alt="Settings page" width="100%"></a>
      <p><b>Settings.</b> Station metadata for RINEX, receiver, base position and RTCM output, saved to the <code>.env</code>.</p>
    </td>
    <td width="50%" valign="top">
      <a href="docs/images/ui-dashboard-light.png"><img src="docs/images/ui-dashboard-light.png" alt="Dashboard in the light theme" width="100%"></a>
      <p><b>Light theme.</b> The dashboard with the light theme selected.</p>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <a href="docs/images/ui-site-light.png"><img src="docs/images/ui-site-light.png" alt="Site page in the light theme" width="100%"></a>
      <p><b>Site, light theme.</b></p>
    </td>
    <td width="50%" valign="top" align="center">
      <a href="docs/images/ui-dashboard-mobile.png"><img src="docs/images/ui-dashboard-mobile.png" alt="Dashboard on a phone" width="48%"></a>
      <a href="docs/images/ui-rtk-mobile.png"><img src="docs/images/ui-rtk-mobile.png" alt="RTK page on a phone" width="48%"></a>
      <p align="left"><b>On a phone.</b> The dashboard and the RTK page at 390 px, with the bottom tab bar.</p>
    </td>
  </tr>
</table>

## Architecture

One asyncio process per host. Bytes from the receiver are split into frames and published on an
in-process pub/sub bus. Every consumer (logger, caster, state, sampler, alerts, WebSocket, rover
outputs) subscribes to the bus on its own, with its own queue, and runs under restart
supervision. RTKLIB runs as a subprocess inside background jobs. The ROS 2 bridge is a separate
WebSocket client.

```mermaid
flowchart TB
  subgraph HW["Receivers"]
    F9P["u-blox ZED-F9P<br/>USB CDC"]
    INS["SBG Ellipse-D / VectorNav VN-200<br/>INS_PORT serial"]
  end

  subgraph D["mtrtk daemon: one asyncio process, ROLE=base or rover"]
    SRC["Source<br/>serial or file replay"]
    CTRL["ReceiverController<br/>capability probe, CFG-VALSET,<br/>VALGET verify, reconnect, watchdog"]
    RT["Router + framer<br/>UBX, RTCM3, NMEA, sbgECom, VN binary"]
    BUS(("Bus<br/>pub/sub"))
    STATE["StateStore<br/>ReceiverState"]
    LOG["RawLogWriter<br/>hourly .ubx + JSON sidecar"]
    RET["Retention<br/>MIN_FREE_GB"]
    CAST["NtripCaster v1 + v2<br/>base role"]
    BM["BaseModeManager<br/>survey-in or fixed site, 1005 check"]
    SAMP["Sampler<br/>1 s and 1 min rows"]
    ALR["AlertEngine"]
    SYS["SystemMonitor<br/>CPU, disk, temperature"]
    ROV["Rover services<br/>NTRIP client, NMEA and JSON out,<br/>sessions, survey points"]
    WEB["FastAPI REST + /ws<br/>+ React SPA"]
    JOBS["JobRunner<br/>RINEX export, PPK"]
  end

  DISK[("DATA_DIR<br/>ubx/YYYY/DDD, jobs/")]
  DB[("mtrtk.db<br/>SQLite WAL")]
  RTKLIB["RTKLIB<br/>convbin, rnx2rtkp"]
  HOOK["ALERT_WEBHOOK_URL"]
  NROV["NTRIP rovers"]
  UPCAST["Upstream NTRIP caster<br/>for example an mtrtk base"]
  APPS["NMEA / JSON consumers<br/>gpsd, QGIS, SW Maps"]
  UI["Browser"]
  ROS["mtrtk_bridge<br/>ROS 2 Humble / Jazzy"]
  TOPICS["ROS 2 topics<br/>/mtrtk/fix, vel, rtk_status, time_mark, imu, heading, nmea"]

  F9P -->|bytes| SRC
  INS -->|bytes| SRC
  SRC --> RT --> BUS
  CTRL -.->|"profile, TMODE3, polls, resets"| F9P
  BM --> CTRL
  BUS --> STATE
  BUS --> LOG
  BUS --> CAST
  BUS --> BM
  BUS --> SAMP
  BUS --> ALR
  BUS --> ROV
  STATE --> WEB
  BUS -->|"live topics"| WEB
  SYS --> BUS
  LOG --> DISK
  RET --> DISK
  SAMP --> DB
  ALR --> DB
  ALR -->|POST| HOOK
  CAST -->|RTCM3| NROV
  UPCAST -->|RTCM3| ROV
  ROV -.->|"inject RTCM"| F9P
  ROV -.->|"inject RTCM"| INS
  ROV --> APPS
  ROV --> DB
  WEB --> JOBS
  JOBS --> RTKLIB
  JOBS --> DISK
  UI <-->|"HTTP + WebSocket"| WEB
  ROS <-->|"WebSocket /ws"| WEB
  ROS --> TOPICS
```

- **Base role:** caster, base-mode manager, raw logger and retention. **Rover role:** NTRIP client, outputs and survey points instead of the caster. Both roles run the web API, sampler, alerts and system monitor.
- **INS rover:** the vendor driver opens `INS_PORT` and fills the same `ReceiverState` in place of the u-blox `ReceiverController` and profile. Nothing downstream changes.
- **Replay:** the live receiver's bus subscription drops the oldest frames when it backs up; a replay (`file:` source) never drops. A replay writes no raw logs unless `REPLAY_LOG=1`.
- **Remote base:** PPK fetches a remote base's raw hours from that base's own API (`GET /api/logs/window`).

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

| | |
|---|---|
| **Host** | Linux, `amd64` or `arm64`: a Raspberry Pi with a 64-bit OS, an x86 box, or a Jetson. Images are published for `linux/amd64` and `linux/arm64` only. |
| **Receiver** | u-blox ZED-F9P on USB (HPG 1.13 or 1.51; `mtrtk doctor` recommends HPG 1.32 or later). It is found by `/dev/serial/by-id/*u-blox*` or by USB vendor ID `1546`. |
| **Container path** | Docker Engine with the Compose v2 plugin (`docker compose`). |
| **Native path** | Debian, Ubuntu or Raspberry Pi OS (64-bit) with systemd and `sudo`. Node.js 20 or later is needed to build the web UI. `install.sh` fetches or builds everything else. |
| **Network** | Tailscale, installed and logged in (`sudo tailscale up`). By default the caster and the UI bind to the `tailscale0` address only. They wait for that address to appear and never fall back to `0.0.0.0`. |
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
| `WEB_BIND` / `WEB_PASSWORD` | `tailscale` / empty | Any bind other than `tailscale` needs `WEB_PASSWORD`, or startup fails. `WEB_ALLOW_INSECURE=1` overrides this. |
| `NTRIP_BIND` | `tailscale` | Also accepts `lan`, `all` or a literal IP address. |
| `DIALOUT_GID` | `20` | The host group that owns the serial device. 20 is right on Debian and Ubuntu; check yours with `stat -c %g /dev/ttyACM0`. |
| `MTRTK_RUN_AS_ROOT` | unset | Set to `1` only if no `DIALOUT_GID` gives access to the device. |

How the container runs:

- **Network:** host networking.
- **Devices:** `/dev` is bind-mounted for hot-plug, and the cgroup rules allow only `ttyACM*` and `ttyUSB*` (char majors 166 and 188). The container is not `privileged`.
- **User:** the daemon runs as uid 1000 with every capability dropped.
- **Data:** stored in `./data`. Settings changed in the web UI are written to `./data/.env`.
- **Health:** the healthcheck is `mtrtk healthcheck`, which runs `GET /healthz` against the daemon.

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

To run from source without systemd:

```bash
uv sync
pnpm --dir web install && pnpm --dir web build:static   # pnpm 11; the UI the daemon serves
uv run mtrtk doctor
uv run mtrtk base                  # or: mtrtk rover / mtrtk run (role from ROLE)
```

### Raspberry Pi and Jetson

- Use a 64-bit OS. There are no 32-bit (`armhf`) images, and `install.sh` targets Raspberry Pi OS 64-bit.
- A Pi has no real-time clock. Raw logs rotate and rover sessions are stamped on the receiver's UTC, so a wrong host clock does not misfile data. Still enable NTP: RINEX export names and event logs use host time, and `doctor` warns when the clock is not synchronised (`sudo timedatectl set-ntp true`).
- On a Pi, building RTKLIB from source takes a few minutes. Use `--rtklib apt` to install Debian's stock 2.4.3 instead.
- Jetson with JetPack 6 is Ubuntu 22.04, so `DIALOUT_GID=20` applies there too. The ROS 2 bridge profile (`ROS_DISTRO=humble`) targets JetPack 6.

### Exposure beyond Tailscale (optional)

Both profiles start the normal `mtrtk` service plus a proxy that reaches it on loopback. Run
`mtrtk doctor` afterwards: its `exposure` check FAILs when a profile would publish the UI without
a password.

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

Limits of the tunnel:

- **NTRIP v2 over HTTPS only.** The tunnel carries HTTP, not raw TCP, so v1 clients such as `str2str` and u-center cannot use it.
- **Cloudflare Access instead of a password.** Put an Access policy on the hostname and set `WEB_BIND=127.0.0.1` with `WEB_ALLOW_INSECURE=1`. Never combine `WEB_ALLOW_INSECURE=1` with `WEB_BIND=lan`.

</details>

Check either profile from outside your network:

```bash
scripts/check-exposure.sh https://rtk.<domain> https://ntrip.<domain>/MTRK rover <password>
```

**`WEB_PASSWORD` rules**

| `WEB_BIND` | `WEB_PASSWORD` empty |
|---|---|
| `tailscale` | Allowed. |
| anything else | Startup fails unless `WEB_ALLOW_INSECURE=1`. |
| `lan` + `WEB_ALLOW_INSECURE=1` | Starts; `doctor` warns. |
| `all`, or a set `PUBLIC_DOMAIN` | `doctor` FAILs, even with `WEB_ALLOW_INSECURE=1`. |
| a set `TUNNEL_TOKEN` | `doctor` FAILs unless `WEB_ALLOW_INSECURE=1`; then it warns that only Cloudflare Access protects the UI. |

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

The receiver can be plugged into another machine on the tailnet. Any path that ends in a
serial-like device works as `MTRTK_SOURCE`.

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

<details>
<summary><b>Caveats for a relayed receiver</b></summary>

<br>

- **Native only.** This works with the native install or `uv run`, not in the container. The compose cgroup rules allow only `ttyACM*`/`ttyUSB*`, and `$HOME` is not mounted.
- **The baud setting is ignored on a pseudo-terminal.** The line speed is set by the far end's `b115200`.
- **Keep both `socat` processes running.** When the link drops, the PTY disappears and the daemon reconnects with backoff once it comes back. Run the two `socat` commands under a supervisor or a restart loop.
- **Stop ModemManager from grabbing the port on the remote PC.** Install `udev/99-mtrtk-ublox.rules` there. The local `doctor` reports ModemManager as "not relevant" for a non-USB source.
- **A relayed link is slower.** Over a Tailscale DERP relay, answers can stall for seconds. The daemon retries probes and readbacks, and a higher `RECEIVER_ACK_TIMEOUT_S` keeps configuration from timing out.

</details>

### Running without hardware (replay)

`mtrtk replay` plays a recorded `.ubx` file as if it were a live receiver, with the full UI and
API. Nothing is sent to the file, raw logs are not written unless `REPLAY_LOG=1`, and the caster
runs anonymously.

Use **`tests/fixtures/f9p_hpg113_base_30s.ubx`**. It carries NAV-EOE, which closes every epoch.
The `f9p_hpg113_raw_10s.ubx` and `f9p_hpg113_raw_60s.ubx` fixtures have no NAV-EOE, so they
produce no epochs and the UI stays on "Waiting for data".

```bash
# From source, no Tailscale needed (build the UI first, see "Native install"):
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

Each walk-through below covers what is built today. The linked docs have the full detail.

### 1. Set up a base: survey-in, 24 h log, PPP, fixed site

The full procedure is in [`docs/ppp-workflow.md`](docs/ppp-workflow.md). The short version:

1. **Configure and start.** Put this in `.env`:
   ```
   ROLE=base
   NTRIP_PASSWORD=choose-a-password
   BASE_MODE=survey-in
   STATION_ID=MTRK          # 4 letters/digits, names the raw logs and the RINEX files
   COUNTRY=BGD              # ISO 3166 alpha-3, used in RINEX 3 file names
   ANTENNA_TYPE=NONE
   ANTENNA_HEIGHT_M=0       # 0 for a base site: the base broadcasts the antenna position
   ```
   Then run `docker compose up -d` (or `uv run mtrtk base`). Run `uv run mtrtk doctor` first to check receiver access, Tailscale, RTKLIB and disk.
2. **Survey-in.** The survey ends once both limits are met: `SVIN_MIN_DURATION_S` (default 300) and `SVIN_ACC_LIMIT_M` (default 2.0). When it validates, the status line's `svin` tail shows `✓`. RTCM MSM7 flows from the start. RTCM 1005 starts only once the receiver holds a valid position. Under a roof, a survey-in will not validate (mean accuracy settles around 10 m), so put the antenna under open sky.
3. **Log 24 h.** Raw UBX is written hourly to `DATA_DIR/ubx/YYYY/DDD/` whatever the base mode. The Site page's *Centimetre site from PPP* panel counts the hours on disk ("24 of 24 hours"). If the card is tight, mark the day's hours *keep* on the Logs page.
4. **Export RINEX.** In the UI: Site page, then *Export the last 24 h for CSRS-PPP*. Or from the CLI:
   ```bash
   uv run mtrtk export --preset csrs-ppp \
       --from "$(date -u -d '24 hours ago' +%Y-%m-%dT%H:00:00Z)" \
       --to   "$(date -u +%Y-%m-%dT%H:00:00Z)" --out /tmp/csrs
   ```
   This writes `MTRK00BGD_R_<YYYYDDDHHMM>_01D_30S_MO.crx.gz`, a mixed navigation file and `manifest.json`. Read the manifest's warnings before you upload. The other presets are `auspos`, `opus` and `generic`.
5. **Submit** the `.crx.gz` to CSRS-PPP with *Static* processing and the *ITRF* frame. The result arrives by e-mail.
6. **Import.** In the UI: Site page, then *Import PPP result*, then *Save and activate*. It accepts the `.sum`, the `.pos` or the e-mailed `.zip`. Activating also persists `BASE_MODE=fixed` and `ACTIVE_SITE` to `.env`. From the CLI:
   ```bash
   uv run mtrtk ppp-import result.zip --save-site roof-ppp --activate
   ```
   The CLI does not edit `.env`, so set `BASE_MODE=fixed` yourself. Otherwise the next start runs a survey-in again.
7. **Verify.** Within about a second, the Site page should read "RTCM 1005 matches the active site: every axis within 0.5 mm" and a `site_verified` event should be logged. A `site_mismatch` event means the receiver is broadcasting something other than the saved site.

You can skip the PPP step for a known position: `mtrtk sites add NAME --ecef X Y Z`, then
`mtrtk sites activate NAME`, with `BASE_MODE=fixed`. See [`docs/base.md`](docs/base.md).

### 2. A rover with NTRIP corrections and NMEA out

1. **Configure** in `.env`:
   ```
   ROLE=rover
   NTRIP_URL=ntrip://rover:<password>@<base-tailscale-ip>:2101/MTRK
   ROVER_NAV_HZ=5
   NMEA_TCP_PORT=10110
   ```
   Start it with `uv run mtrtk rover`, or `docker compose up -d` with `ROLE=rover`. The NTRIP client tries v2 and falls back to v1. It sends GGA every `NTRIP_GGA_INTERVAL_S` (10 s) and reconnects with backoff. You can also set or change the caster from the RTK page, which writes `NTRIP_URL` and restarts the client in place.
2. **Outputs.**

   | Consumer | How |
   |---|---|
   | QGIS | `gpsd -N tcp://<rover-ip>:10110`, then GPS Information panel, then gpsd |
   | SW Maps, OpenCPN, any NMEA TCP client | connect to `<rover-ip>:10110`. There is no password; `NMEA_TCP_BIND` (`lan`, `all`, `tailscale` or an IP) decides who can reach it |
   | UDP listeners | `NMEA_UDP_TARGETS=host:port,host:port` |
   | Serial-only software | `NMEA_SERIAL=/dev/ttyUSB1` or `NMEA_SERIAL=pty` (linked at `DATA_DIR/ttyMTRTK`) |
   | Scripts | `JSON_UDP_PORT=5555`: one JSON object per epoch to `127.0.0.1` |

   The default sentences are `GGA,RMC,GST,GSA,GSV,VTG,ZDA`.
3. **Check the RTK page.** Aim for a correction age under 5 s and "RTK fixed". In the *Corrections received* table, **Count** means corrections arrive and **Used** means the receiver accepts them.
4. **Collect points.** On the Survey page, start a session and collect a named point. It averages `POINT_EPOCHS` epochs (default 30), RTK fixed only while `POINT_FIXED_ONLY=1`. Points are averaged in ENU with per-axis standard deviations. Export them as CSV, GeoJSON, KML or GPX, from the page or with `GET /api/rover/points/export?fmt=csv`.

Raw UBX is logged hourly on a rover too, ready for PPK. See [`docs/rover.md`](docs/rover.md).

### 3. PPK a rover log against the base

**From the PPK page:** pick the rover source (a session, a UTC window up to 7 days, or an uploaded
UBX/RINEX up to 2 GB). Pick the base source (a remote mtrtk base over Tailscale, an upload, or
this host's logs). Pick the base position (automatic from the site the base logged, a saved site,
or manual ECEF). Choose the options (camera events, QZSS, elevation mask), then press *Run PPK*.
The result shows the track coloured by quality, a per-epoch quality strip, statistics, warnings,
the first 200 camera events, and every output file to download.

**From the CLI:**

```bash
mtrtk ppk --session 3 --base-url http://100.100.50.10:8080 --out ./ppk-2026-09-19
mtrtk ppk --from 2026-09-19T08:00Z --to 2026-09-19T09:30Z --base-logs --site roof --out ./ppk
mtrtk ppk --rover flight.ubx --base base.ubx --base-xyz -26748.172 5837156.618 2561801.261 --out ./ppk
```

`--set key=value` overrides an rnx2rtkp option (you can repeat it). `--no-events` skips camera
events, and `--qzss` includes QZSS.

**Outputs:** `track.pos`, `track.csv`, `track.geojson`, `track.kml`, `summary.json`, the
`ppk.conf` that ran, and the RINEX files used. Camera triggers come from TIM-TM2 (EXTINT) pulses
in the rover's UBX log: each pulse is interpolated between its neighbouring epochs and written to
`events.csv` and `events.geojson` with a `status` of `ok`, `gap_too_large` or `no_neighbours`.
Match the pulse `count` to the image order. All times are GPST. See [`docs/ppk.md`](docs/ppk.md).

### 4. ROS 2 bridge

```bash
ROS_DISTRO=humble docker compose --profile ros2 up -d      # or ROS_DISTRO=jazzy
ros2 topic echo /mtrtk/fix
```

The `mtrtk-ros2` service is a WebSocket client of the daemon and runs with host networking and a
UDP-only Fast DDS profile.

- **`MTRTK_WS_URL`** defaults to `ws://127.0.0.1:8080/ws`, which only reaches the daemon when `WEB_BIND` is `lan`, `all` or `127.0.0.1`. With the default `WEB_BIND=tailscale`, point it at `ws://<tailscale-ip>:8080/ws`.
- **`MTRTK_WS_TOKEN`** must be set when `WEB_PASSWORD` is set. Get the token from `POST /api/login`.
- **Bridge on another machine:** `docker compose --profile ros2 up -d --no-deps mtrtk-ros2`.
- **Stopping:** use `docker compose stop mtrtk-ros2`, not `--profile ros2 down`, which also stops the daemon.

| Topic | Type |
|---|---|
| `/mtrtk/fix` | `sensor_msgs/NavSatFix` (status −1/0/1/2; a NaN no-fix is published when there is no data) |
| `/mtrtk/vel` | `geometry_msgs/TwistWithCovarianceStamped` (ENU) |
| `/mtrtk/time_reference` | `sensor_msgs/TimeReference` (receiver UTC) |
| `/mtrtk/rtk_status` | `mtrtk_msgs/RtkStatus` |
| `/mtrtk/time_mark` | `mtrtk_msgs/TimeMark` (EXTINT camera pulses) |
| `/mtrtk/imu`, `/mtrtk/heading` | `sensor_msgs/Imu`, `std_msgs/Float64` (INS rover only) |
| `/mtrtk/nmea` | `nmea_msgs/Sentence` (when the `nmea_tcp` parameter is set) |

Native colcon builds, parameters and troubleshooting are in [`docs/ros2.md`](docs/ros2.md).

### 5. SBG Ellipse-D as the rover

```
ROLE=rover
ROVER_DRIVER=sbg_ellipse
INS_PORT=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXX-if00-port0   # Port A, sbgECom mode
INS_BAUD=921600          # must match Port A; the default is 115200, the bench unit runs 921600
INS_APPLY_CONFIG=0       # default: read-only, nothing is written to the unit
INS_RAW_GNSS=1           # default: GPS1_RAW logged as hourly .ubx
NMEA_SENTENCES=GGA,RMC,GST,GSA,GSV,VTG,ZDA,HDT,PASHR
```

- **Read-only first.** An INS is never auto-detected, so `INS_PORT` is always explicit, and mtrtk never writes the baud rate. With the daemon stopped, run `uv run mtrtk ins info` to see the identity and the current configuration next to the wanted profile, and `uv run mtrtk ins config --dry-run` to see what would change. Apply with `--apply`, or set `INS_APPLY_CONFIG=1`, only after reading that report. With `INS_APPLY_CONFIG=1` the unit saves its settings and reboots.
- **Raw GNSS.** GPS1_RAW is the internal u-blox receiver's UBX stream (RXM-RAWX, RXM-SFRBX, SEC-SIG). mtrtk re-frames it into the normal hourly `DATA_DIR/ubx/...` logs, so RINEX export, PPP and `mtrtk ppk` all work on it. Sync In pulses become live time marks but are not in the `.ubx`, so `events.csv` from PPK is empty for an Ellipse.
- **Downstream.** The Receiver page shows INS panels and the Dashboard gets an IMU card. `HDT`/`PASHR` go out whenever there is a heading, even before the filter has a position. ROS 2 publishes `/mtrtk/imu` and `/mtrtk/heading`.
- **No unit on hand?** Replay a capture: `ROLE=rover ROVER_DRIVER=sbg_ellipse MTRTK_SOURCE=file:tests/fixtures/ins/sbg_frames.bin REPLAY_LOOP=1 mtrtk run`.

Wiring, lever arms, the VN-200 driver and the verified/unverified matrix are in
[`docs/ins-drivers.md`](docs/ins-drivers.md).

## Configuration

All configuration is environment variables. mtrtk reads them from `.env` in the working directory
(`/data/.env` in the Docker image), and real environment variables take precedence over the file.
Start from the annotated template:

```bash
cp .env.example .env
```

Each variable is a field of `Settings` in [`src/mtrtk/config.py`](src/mtrtk/config.py), with the
name in upper case. Booleans take `1`/`0`. For most optional keys an empty value (`KEY=`) means
"unset". `NTRIP_PASSWORD` is the exception: `NTRIP_PASSWORD=` (empty) allows anonymous access.
The web UI's Settings page writes changes back to `.env` through `PUT /api/config`. `BASE_MODE`,
`SVIN_MIN_DURATION_S`, `SVIN_ACC_LIMIT_M` and `ACTIVE_SITE` apply live. Every other key needs a
restart.

Two cross-checks run at startup:

- `WEB_PASSWORD` is required whenever `WEB_BIND` is not `tailscale`, unless `WEB_ALLOW_INSECURE=1`.
- `NTRIP_PASSWORD` must be set (empty counts as set) for `ROLE=base`.

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
| `NTRIP_PASSWORD` | unset | Required for a base. An empty value means anonymous (keep that to the tailnet) |
| `NTRIP_MAX_CLIENTS` | `32` | Rovers served at once. Clients past the limit are refused |

`tailscale` binds the host's `tailscale0` address and retries until Tailscale is up. It never
falls back to `0.0.0.0`.

</details>

<details>
<summary><b>Web UI and API</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `WEB_BIND` | `tailscale` | `tailscale`, `lan`, `all` or an IP address |
| `WEB_PORT` | `8080` | UI, API and WebSocket port |
| `WEB_PASSWORD` | unset | Login password. Required unless `WEB_BIND=tailscale` or `WEB_ALLOW_INSECURE=1` |
| `WEB_ALLOW_INSECURE` | `0` | `1` accepts an unauthenticated UI on a non-Tailscale bind |

</details>

<details>
<summary><b>Raw logging, retention and process</b></summary>

<br>

| Variable | Default | Meaning |
|---|---|---|
| `LOG_MESSAGES` | `RXM-RAWX,RXM-SFRBX,NAV-PVT,NAV-HPPOSLLH,NAV-SVIN,TIM-TM2,MON-VER` | UBX messages kept in the hourly raw logs |
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
`MIN_FREE_GB` from above. When `NTRIP_URL` is set, its host becomes the default remote base
(`http://<host>:8080`). `mtrtk ppk --base-password` also reads `MTRTK_BASE_PASSWORD`. See
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

`docker compose --profile public up -d` puts Caddy (HTTPS) in front of the UI.
`docker compose --profile cloudflare up -d` runs a Cloudflare tunnel. Both need `WEB_BIND=lan` or
`127.0.0.1` plus `WEB_PASSWORD`. `.env.example` has the full exposure notes.

</details>

## Command line

```text
mtrtk [-v] [--version] COMMAND [ARGS]...
```

`-v` / `--verbose` turns on debug logging and goes before the command (`mtrtk -v base`). Every
command accepts `-h` / `--help`.

| Command | What it does |
|---|---|
| `mtrtk run` | Run the daemon in the role given by `ROLE` |
| `mtrtk base` / `mtrtk rover` | Run as a base station or a rover |
| `mtrtk replay FILE` | Replay a recorded `.ubx` stream as a live receiver; no configuration is sent |
| `mtrtk record` | Record the raw receiver byte stream to a file |
| `mtrtk doctor` | Check receiver access, host services, Tailscale, ports, RTKLIB, disk and exposure. Exits 1 on any FAIL |
| `mtrtk healthcheck` | Exit 0 when the local `/healthz` answers (the container healthcheck) |
| `mtrtk sites list\|add\|activate\|delete` | Manage saved base sites |
| `mtrtk export` | Export a raw-log window as RINEX |
| `mtrtk ppp-import FILE` | Read a PPP result (CSRS-PPP, AUSPOS SINEX, OPUS) |
| `mtrtk ppk` | Post-process rover raw data against a base with RTKLIB |
| `mtrtk ins info\|config\|monitor` | Inspect, compare or configure an INS unit |
| `mtrtk backup` / `mtrtk restore ARCHIVE` | Archive and restore the database, sites and `.env` |

<details>
<summary><b>Every command with its useful flags</b></summary>

<br>

| Command | What it does | Useful flags |
|---|---|---|
| `mtrtk run` | Run the daemon in the role given by `ROLE` | |
| `mtrtk base` | Run as a base station (`ROLE=base`) | |
| `mtrtk rover` | Run as a rover (`ROLE=rover`) | |
| `mtrtk replay FILE` | Replay a recorded `.ubx` stream as a live receiver; no configuration is sent | `--speed` (default 1.0, 0 = max), `--loop` |
| `mtrtk record` | Record the raw receiver byte stream to a file | `--out FILE` (required), `--port` (default `auto`), `--baud`, `--seconds` (default 60) |
| `mtrtk doctor` | Check receiver access, host services, Tailscale, ports, RTKLIB, disk and exposure. Exits 1 on any FAIL | `--json`, `--probe` (polls receiver firmware; stop the daemon first) |
| `mtrtk healthcheck` | Exit 0 when the local `/healthz` answers (the container healthcheck) | |
| `mtrtk sites list` | List saved sites; the active one is marked `*` | |
| `mtrtk sites add NAME` | Save a site | `--ecef X Y Z` or `--llh LAT LON H`, `--sigma`, `--frame` (default `ITRF2020`), `--epoch`, `--source`, `--notes` |
| `mtrtk sites activate NAME` | Make NAME the active fixed site (a running base applies it within 10 s) | |
| `mtrtk sites delete NAME` | Delete a saved site | |
| `mtrtk export` | Export a raw-log window as RINEX | `--from`, `--to` (ISO-8601 with timezone), `--out DIR` (all required), `--preset csrs-ppp\|auspos\|opus\|generic` (default `csrs-ppp`), `--overwrite`. Generic only: `--interval`, `--hatanaka/--no-hatanaka`, `--gzip/--no-gzip` |
| `mtrtk ppp-import FILE` | Read a PPP result (CSRS-PPP `.sum`/`.pos`/`.zip`, AUSPOS SINEX, OPUS) | `--save-site NAME`, `--activate`, `--prefer-frame itrf\|nad83` |
| `mtrtk ppk` | Post-process rover raw data against a base with RTKLIB | `--out DIR` (required). Rover: `--rover FILE`, `--session ID` or `--from/--to`. Base: `--base FILE` (+ `--base-nav`), `--base-url URL` (+ `--base-password`), or `--base-logs`. Position: `--site NAME` or `--base-xyz X Y Z`. Also `--no-events`, `--qzss`, `--set key=value` |
| `mtrtk ins info` | Read the INS unit's identity and configuration (queries only) | |
| `mtrtk ins config` | Compare the unit's configuration with the mtrtk profile, or apply it | `--dry-run`, `--apply` (saved to flash only with `INS_APPLY_CONFIG=1`) |
| `mtrtk ins monitor` | One line per navigation epoch: INS mode, position, heading, fix | `--seconds` (0 = until Ctrl-C) |
| `mtrtk backup` | Archive the database, sites and a masked `.env`; safe while the daemon runs | `--out FILE` (required), `--with-secrets` |
| `mtrtk restore ARCHIVE` | Restore a backup into `DATA_DIR` (stop the daemon first) | `--force` (overwrites the database; a copy is kept) |

The `mtrtk ins` commands open `INS_PORT` exclusively, so run them with the daemon stopped.

</details>

## HTTP API

The daemon serves a JSON API and a WebSocket on the same address as the UI.

- **Base URL:** `http://<WEB_BIND host>:<WEB_PORT>`, by default the Tailscale address on port `8080`.
- **Interactive docs:** `/api/docs` (Swagger UI) and `/api/openapi.json`. When a password is set, these require a login too.
- **Liveness:** `GET /healthz` returns `{"status": "ok", "role", "connected", "passive"}`. It is the only route that never needs auth.
- **Auth:** only active when `WEB_PASSWORD` is set. `POST /api/login {"password": "..."}` returns `{"token": ...}` and sets an `mtrtk_session` cookie that lasts 30 days. Non-browser clients send `Authorization: Bearer <token>`. `POST /api/logout` clears the cookie.

<details>
<summary><b>Route groups</b></summary>

<br>

| Group | Routes |
|---|---|
| Status | `GET /api/status`, `GET /api/state`, `GET /api/system` |
| Configuration | `GET`/`PUT /api/config`, `POST /api/restart` |
| Receiver | `GET /api/receiver`, `POST /api/receiver/{reapply,reset,poll,profile}` |
| Base | `/api/base/mode`, `/api/base/survey` (+ `restart`, `freeze`), `/api/base/sites`, `/api/base/ppp/import` |
| NTRIP caster | `GET /api/ntrip`, `/api/ntrip/clients`, `/api/ntrip/history` |
| Rover | `GET /api/rover`, `PUT /api/rover/ntrip`, `/api/rover/sessions`, `/api/rover/collect`, `/api/rover/points` (+ `export`) |
| Raw logs | `/api/logs`, `/api/logs/availability`, `/api/logs/window`, `/api/logs/{name}` |
| History and events | `GET /api/history`, `/api/history/metrics`, `GET /api/events`, `POST /api/events/{id}/ack` |
| RINEX export | `GET /api/export/presets`, `POST /api/export` (job), `GET /api/export/rinex` (zip, up to 6 h) |
| Jobs | `/api/jobs`, `/api/jobs/{id}`, `/api/jobs/{id}/files[/{name}]` |
| PPK | `GET /api/ppk/defaults`, `POST /api/ppk/upload`, `POST /api/ppk` |

</details>

**WebSocket.** Connect to `ws://<host>:8080/ws?topics=pvt,sats,...`, adding `&token=...` when the
client cannot send the cookie or a header. The first message is always a `snapshot` of the full
state. After that you get one `epoch` message per receiver epoch plus `update` messages for the
topics you subscribed to: `pvt`, `sats`, `rtcm`, `svin`, `rf`, `span`, `ntrip`, `events`,
`system`, `receiver`, `base`, `jobs`, `rawlog`, `daemon`, `rtk`, `survey`, `ins`. With no
`topics`, you get all of them. Up to 32 sockets can be open at once.

[`docs/api.md`](docs/api.md) documents every route, status code and WebSocket close code.

## Documentation

| Document | What it covers |
|---|---|
| [`docs/setup.md`](docs/setup.md) | Installing, pinning and updating, in Docker and natively |
| [`docs/hardware.md`](docs/hardware.md) | Receivers, antennas, cabling and host hardware |
| [`docs/exposure.md`](docs/exposure.md) | Tailscale, LAN, the Caddy and Cloudflare profiles, and their security trade-offs |
| [`docs/firmware.md`](docs/firmware.md) | ZED-F9P firmware versions and updating them |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Common problems and how to fix them |
| [`docs/base.md`](docs/base.md) | Base station: survey-in, fixed sites, the 1005 check, the caster |
| [`docs/ppp-workflow.md`](docs/ppp-workflow.md) | From a 24 h log to a PPP-surveyed fixed site |
| [`docs/rover.md`](docs/rover.md) | Rover role: NTRIP client, NMEA/JSON outputs, sessions and points |
| [`docs/ppk.md`](docs/ppk.md) | Post-processing with RTKLIB and camera events |
| [`docs/ins-drivers.md`](docs/ins-drivers.md) | SBG Ellipse-D and VectorNav VN-200: wiring, configuration, verification matrix |
| [`docs/ros2.md`](docs/ros2.md) | The ROS 2 bridge, topics, parameters and native builds |
| [`docs/ui.md`](docs/ui.md) | The web UI, page by page |
| [`docs/api.md`](docs/api.md) | REST routes, status codes and the WebSocket protocol |
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

| Phase | Scope | State | Tag |
|---|---|---|---|
| 0–1 | Scaffold, receiver core, replay and record | Complete | `v0.1.0-phase1` |
| 2 | Base daemon: raw logging and retention, NTRIP caster, survey-in and fixed sites with the 1005 check, history, alerts | Complete | `v0.2.0-phase2` |
| 3 | Web API: FastAPI, WebSocket hub, `.env` write-back, jobs, optional login | Complete | `v0.3.0-phase3` |
| 4 | Operator web UI | Complete | `v0.4.0-phase4` |
| 5 | RINEX export and PPP import | Code complete. Tested end to end on a real 1 h export; the real 24 h CSRS-PPP round trip is still pending | not tagged |
| 6 | F9P rover: NTRIP client, NMEA/JSON outputs, sessions, survey points | Complete. Verified on replay; live RTK pending a second receiver | `v0.6.0-phase6` |
| 7 | ROS 2 bridge (Humble, Jazzy) | Complete | `v0.7.0-phase7` |
| 8 | PPK with RTKLIB, camera events, PPK page | Code complete. The spec milestone "zero-baseline CI self-test ≥ 95 % fixed" is open (see below) | not tagged |
| 9 | Exposure and hardening | In progress. Merged: `doctor` host checks and `--json`, `backup`/`restore` and native install, the `public` (Caddy) and `cloudflare` compose profiles, a non-root container, the release pipeline. Pending: the documentation set and the fresh-host acceptance run | not tagged |
| 10 | INS drivers: SBG Ellipse-D, VectorNav VN-200 | Complete. SBG verified live, read-only; VN-200 built from the spec only | `v0.10.0-phase10` |

### Known limitations

- **F9P rover live RTK is not field-tested.** The rover role is verified on replay, against a replayed base caster. `tests/hardware/test_live_rover.py` has not run on hardware, and an RTK fix needs a second receiver.
- **The PPP round trip is unconfirmed.** No 24 h export has been submitted to CSRS-PPP, AUSPOS or OPUS yet. The result parsers were built from reconstructed sample files, not a real e-mailed result. OPUS acceptance of the F9P's L2C observations is unknown. The antenna's ANTEX calibration has not been checked, so keep `ANTENNA_TYPE=NONE`.
- **The PPK fix-rate milestone is unmet.** A same-file zero-baseline run stays 100 % float at millimetre level, because rnx2rtkp never attempts to fix all-zero ambiguities. CI asserts fixed + float ≥ 99 % instead. Measuring a real fix rate needs a splitter capture or a real baseline.
- **HPG 1.51 has not met hardware.** Support comes from the capability probe; the only receiver tested so far runs HPG 1.13.
- **SBG Ellipse-D is verified read-only only.** Framing, logs, GPS1_RAW to RINEX and the live UI are verified. Configuration writes, RTCM input (Port A or Port B) and RTK on the unit are not.
- **VectorNav VN-200 is spec-only.** It is built from the vendor documentation and has never met a unit. Its RTCM input is opt-in and unverified, and its RawMeas capture (`.vnraw`) has no RINEX converter.
- **Tunnelled or relayed receiver links can stall.** On a receiver reached through a socat PTY over a Tailscale relay, multi-second stalls make ACK/poll timeouts trigger reconnects. A running daemon reconnects instead of exiting. Raise `RECEIVER_ACK_TIMEOUT_S` (default 2.0, for example 5) for such links.
- **A survey-in will not validate indoors.** It is a receiver limit, not a software one: put the antenna under open sky, or use a known fixed site.

## License

mtrtk is released under the [MIT License](LICENSE). Copyright (c) 2026 Mollah Md Saif.
