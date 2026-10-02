# mtrtk

Multi-role GNSS toolkit for the u-blox ZED-F9P. A **base station** that logs raw UBX, serves RTCM3
over its own NTRIP caster, exports RINEX for PPP services and sits on the result, with a web UI
that shows everything the receiver knows; a **rover** (F9P, or an SBG / VectorNav INS) that feeds
RTK positions to NMEA, JSON and ROS 2; and PPK with RTKLIB. One process, one container, one
`.env`, on any Linux host with a USB F9P: Raspberry Pi, x86 box, Jetson. MIT licensed.

## Features

- **Receiver.** USB auto-detect (`/dev/serial/by-id/…u-blox…`, VID 1546) or an explicit port,
  reconnect with backoff and a no-bytes watchdog. `CFG-VALSET` read back with `CFG-VALGET` (first
  apply of a start to RAM+BBR+Flash); core keys must ACK or startup fails, optional ones are probed
  per firmware, so one build runs on HPG 1.13 and 1.51. Live NAV-PVT/HPPOSLLH/HPPOSECEF/SAT/SIG/
  DOP/STATUS/CLOCK/TIMEUTC/SVIN and MON-HW/RF/COMMS/SPAN.
- **Base.** Survey-in, or FIXED from a saved, named site (ECEF, from PPP), verified against the
  receiver. RTCM3 MSM7 1077/1087/1097/1127 (or MSM4) + 1005 at 1 Hz + 1230 every 5 s. The **1005
  check** compares the broadcast 1005 with the active site to 0.5 mm. In-process NTRIP caster, v1
  and v2, Basic auth or anonymous, sourcetable, per-client stats, slow-client drop, rover GGA on
  the map; new clients get the cached 1005 and 1230 at once.
- **Raw logging.** Hourly UTC-aligned `.ubx` files keyed on *receiver* time under
  `DATA_DIR/ubx/YYYY/DDD/`, with JSON sidecars (counts, size, sha256, firmware, site, `keep`),
  finalized at startup after a crash; retention prunes the oldest non-`keep` hours.
- **RINEX and PPP.** Exports for CSRS-PPP, AUSPOS, OPUS and generic RINEX 3.04 (Hatanaka, gzip);
  CSRS `.sum`/`.pos`, AUSPOS SINEX and OPUS results import as a site.
- **Rover.** NTRIP client (v2, v1 fallback, GGA upload), RTK status, NMEA over TCP/UDP/serial/pty,
  a JSON UDP feed, sessions, averaged survey points with CSV/GeoJSON/KML/GPX export.
- **PPK.** RTKLIB `rnx2rtkp` against a local, remote or uploaded base: track (CSV, GeoJSON, KML),
  TIM-TM2 camera events for geotagging, a summary.
- **ROS 2 bridge.** `mtrtk_bridge` (rclpy) and `mtrtk_msgs` for Humble and Jazzy: `NavSatFix`, ENU
  velocity, time reference, RTK status, EXTINT time marks and NMEA on `/mtrtk/*`, plus `/mtrtk/imu`
  and `/mtrtk/heading` from an INS rover's attitude. A WebSocket client of the daemon
  (websocket-client), so the core stays ROS-free; `docker compose --profile ros2`.
- **History, alerts.** SQLite (WAL): 1 s samples 24 h, 1 minute aggregates 90 d. Alerts (receiver,
  fix, survey-in, jamming, antenna, disk, logger, temperature, RTK, corrections) in the UI and to
  an optional `ALERT_WEBHOOK_URL` (ntfy/Discord/Telegram compatible).
- **Web UI and API.** A React SPA the daemon serves itself (Dashboard, Satellites, Receiver,
  Corrections, Site & Position, Logs, History, Events, Settings, RTK, Survey, PPK), live over one
  WebSocket, dark and light, phone-sized. A REST API for all of it (`.env` write-back, `/api/docs`).
  Optional single-password login; a public web bind without a password is refused.
- **Operations.** Replay any `.ubx` as a fake receiver; `mtrtk doctor`; `mtrtk backup`/`restore`.
  Hardened Docker Compose (host networking, hotplug-safe `/dev`, non-root, no capabilities), the
  `public` (Caddy) and `cloudflare` (Tunnel) profiles, a native `install.sh`, multi-arch images.

## Quick start (Docker)

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
cp .env.example .env && $EDITOR .env      # ROLE, NTRIP_PASSWORD, STATION_ID at least
docker compose up -d
docker compose exec mtrtk mtrtk doctor
# UI: http://<tailscale-ip>:8080   NTRIP: ntrip://<user>:<pass>@<tailscale-ip>:2101/MTRK
```

Until v0.1.0 is released, `:latest` is an old pre-hardening image: pin `:edge` in a
`docker-compose.override.yml`, so `git pull` never conflicts ([docs/setup.md](docs/setup.md#updating)).
Native alternative (Debian, Ubuntu, Raspberry Pi OS with systemd): `./install.sh`. From a source checkout, set `DATA_DIR=./data` in
`.env` (`/data` is the container's path), then `uv sync`, `uv run mtrtk doctor`, `uv run mtrtk base`.

No hardware? `DATA_DIR=./data WEB_BIND=127.0.0.1 WEB_ALLOW_INSECURE=1 uv run mtrtk replay
tests/fixtures/f9p_hpg113_base_30s.ubx --speed 10 --loop` (the UI needs `pnpm --dir web build:static`).

## Configuration

Everything is environment variables, read from `.env` (`.env.example` is the annotated list;
[docs/setup.md](docs/setup.md) walks through it). `tailscale` binds `tailscale0` and retries until
Tailscale is up, never falling back to `0.0.0.0`. The ones to think about first:

| Variable | Default | What it does |
|---|---|---|
| `ROLE` | `base` | `base` or `rover` |
| `MTRTK_SOURCE` | `auto` | `auto`, a serial device path, or `file:<path>.ubx` to replay |
| `STATION_ID` / `COUNTRY` | `MTRK` / `BGD` | Name the log files and the RINEX files |
| `BASE_MODE` | `survey-in` | `survey-in`, `fixed` (with `ACTIVE_SITE`) or `off` |
| `NTRIP_BIND` / `NTRIP_PORT` / `MOUNTPOINT` | `tailscale` / `2101` / `MTRK` | Where rovers connect: `tailscale`, `lan`, `all` or an IP |
| `NTRIP_USER` / `NTRIP_PASSWORD` | `rover` / — | Caster auth; the base needs it set; empty = anonymous |
| `WEB_BIND` / `WEB_PORT` / `WEB_PASSWORD` | `tailscale` / `8080` / — | Where the UI listens, and its login; any other bind needs `WEB_PASSWORD` (or `WEB_ALLOW_INSECURE=1`) |
| `DATA_DIR` | `/data` | Raw logs, exports and the SQLite database (`./data` outside Docker) |
| `SVIN_MIN_DURATION_S` / `SVIN_ACC_LIMIT_M` | `300` / `2.0` | When a survey-in may finish |
| `MIN_FREE_GB` | `5.0` | Prune the oldest raw logs below this much free disk |
| `ALERT_WEBHOOK_URL` | — | POST alerts here as JSON |
| `NTRIP_URL` | — | Rover: the caster to take corrections from ([docs/rover.md](docs/rover.md)) |
| `NMEA_TCP_PORT` / `NMEA_TCP_BIND` | `10110` / `lan` | Rover: the NMEA TCP server, no password; `-1` turns it off |
| `ROVER_DRIVER` | `ublox` | Rover: `ublox`, `sbg_ellipse` or `vectornav` ([docs/ins-drivers.md](docs/ins-drivers.md)) |
| `INS_PORT` / `INS_BAUD` / `INS_APPLY_CONFIG` | — / `115200` / `0` | INS rover: its port, rate, and whether to write (and flash) the profile |
| `PUBLIC_DOMAIN` / `TUNNEL_TOKEN` | — | The `public` and `cloudflare` profiles ([docs/exposure.md](docs/exposure.md)) |

## Commands

```
mtrtk base | rover | run      # run the daemon as a base, a rover, or as ROLE says
mtrtk replay FILE             # replay a .ubx recording as a fake receiver (--speed, --loop)
mtrtk record --out F          # record the live receiver byte stream to a file
mtrtk doctor | healthcheck    # check the host (--probe, --json) | exit 0 when /healthz answers
mtrtk sites list|add|activate|delete
mtrtk export --preset P --from T --to T --out D    # RINEX for csrs-ppp, auspos, opus, generic
mtrtk ppp-import FILE [--save-site NAME --activate]
mtrtk ppk --rover F --base F --out D   # or --session/--from/--to, --base-url/--base-logs
mtrtk ins info|config|monitor # INS rover unit: identity, configuration (--dry-run/--apply), epochs
mtrtk backup --out F          # database, sites and (masked) .env; restore with: mtrtk restore F
```

## Documentation

| Page | What is in it |
|---|---|
| [setup.md](docs/setup.md) | Host prep, `.env`, Docker and native install, updating, backups, Pi and Jetson |
| [hardware.md](docs/hardware.md) | The F9P board, antenna placement, ARP, two receivers, RF interference |
| [base.md](docs/base.md) | The base station: rovers, survey-in, fixed sites, the 1005 check, files, alerts |
| [rover.md](docs/rover.md) | The rover: NTRIP client, NMEA/JSON outputs, the RTK page, survey points |
| [ppp-workflow.md](docs/ppp-workflow.md) | Centimetre base coordinates: RINEX export, CSRS-PPP/AUSPOS/OPUS, import |
| [ppk.md](docs/ppk.md) | Post-processing with RTKLIB, camera events, reading the result |
| [exposure.md](docs/exposure.md) | Tailscale, public IP + Caddy, Cloudflare Tunnel, remote receivers |
| [firmware.md](docs/firmware.md) | Checking and upgrading the F9P firmware, what differs per version |
| [ros2.md](docs/ros2.md) | The ROS 2 bridge: Docker and colcon, topics, parameters, tokens |
| [ins-drivers.md](docs/ins-drivers.md) | SBG Ellipse-D and VN-200: wiring, configuration, what is verified |
| [ui.md](docs/ui.md) | Every page of the web UI, live data, coordinates, the map |
| [api.md](docs/api.md) | Every route, the WebSocket protocol, authentication |
| [troubleshooting.md](docs/troubleshooting.md) | Symptom, cause and fix |
| [acceptance.md](docs/acceptance.md) | The release checklist: what was verified where, what is pending |

## Supported hardware

| Unit | Role | Driver | Status |
|---|---|---|---|
| u-blox ZED-F9P (HPG 1.13; 1.51 by design) | base, rover | `ublox` | verified: base on hardware, rover on replay |
| SBG Ellipse-D | rover (INS) | `sbg_ellipse` | spec-based; read-only stream verified on a real unit, configuration and RTK not yet |
| VectorNav VN-200 | rover (INS) | `vectornav` | spec-based, awaiting hardware |

## Architecture

```
Receiver (USB) ─▶ Source ─▶ Demux ─▶ Bus ─┬─▶ RawLogger     hourly .ubx + .json sidecars
   ▲            (serial |  (UBX /         ├─▶ NtripCaster   RTCM → rovers (base)
   │ RTCM in     replay)    RTCM3)        ├─▶ StateStore    → REST + WebSocket → web UI, ROS 2
   │ CFG-VALSET                           ├─▶ Sampler       SQLite history
   └─ ReceiverController                  └─▶ Alerts        events + webhook
NtripClient (rover) ─RTCM─▶ receiver    RTKLIB convbin/rnx2rtkp run as background jobs
```

## Status

Tagged: Phases **1** receiver core, replay, record · **2** base daemon · **3** web API · **4** web UI · **6** F9P
rover · **7** ROS 2 bridge · **10** INS drivers (spec-based). Code-complete: **5** RINEX and PPP import (gate
pending: a real CSRS-PPP round trip) · **8** PPK (the spec's "zero baseline ≥ 95 % fixed in CI" milestone is
open, pending a ruling) · **9** exposure and hardening ([acceptance](docs/acceptance.md) on a fresh Pi pending).

## License

MIT ([LICENSE](LICENSE)). Design: `docs/superpowers/specs/2026-09-18-mtrtk-design.md`; development: `CONTRIBUTING.md`.
