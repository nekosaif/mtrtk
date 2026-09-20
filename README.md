# mtrtk

Multi-role GNSS toolkit for the u-blox ZED-F9P: an RTK **base station** that logs raw UBX for
post-processing and serves RTCM3 corrections over its own NTRIP caster, with a web UI that shows
everything the receiver knows. Rover, RINEX/PPP and PPK roles are planned — see
[Status](#status).

One process, one container, one `.env`. Runs on any Linux host with a USB F9P: Raspberry Pi,
x86 box, Jetson. Design: `docs/superpowers/specs/2026-09-18-mtrtk-design.md`. MIT licensed.

## Features

**Receiver**
- USB auto-detect (`/dev/serial/by-id/…u-blox…`, VID 1546) or an explicit port; reconnect with
  backoff and a no-bytes watchdog, re-applying and re-verifying the profile every time.
- Configuration by `CFG-VALSET` to RAM+BBR+Flash, every key read back with `CFG-VALGET`.
  Core keys must ACK or startup fails loudly; optional keys are probed per firmware and skipped
  with an event when the receiver NAKs them — the same build runs on HPG 1.13 (PROTVER 27.12)
  and on 1.51.
- Live state from NAV-PVT/HPPOSLLH/HPPOSECEF/SAT/SIG/DOP/STATUS/CLOCK/TIMEUTC/SVIN and
  MON-HW/RF/COMMS/SPAN: position, accuracy, DOPs, per-signal C/N0, jamming and AGC, antenna
  status, spectrum, comms load, firmware.

**Base station**
- Survey-in (portable) or FIXED from a saved, named site; sites hold ECEF coordinates from a PPP
  solution and activate with a TMODE3 write that is verified against the receiver.
- RTCM3 out: MSM7 1077/1087/1097/1127 (or MSM4 via `RTCM_MSM=4`) + 1005 at 1 Hz + 1230 every 5 s.
- The **1005 check**: the decoded RTCM 1005 ECEF is compared against the active site to 0.1 mm —
  the single visible "this base is configured correctly" indicator.
- In-process NTRIP caster, v1 and v2, Basic auth (or anonymous), sourcetable, per-client stats,
  slow-client drop, and rover GGA positions plotted on the map. New clients get the cached 1005
  and 1230 immediately so RTK starts on the next epoch.

**Raw logging**
- Hourly UTC-aligned `.ubx` files keyed on *receiver* time, not the host clock, under
  `DATA_DIR/ubx/YYYY/DDD/`, each with a JSON sidecar (start/end, counts per message, size,
  sha256, firmware, site, `keep` flag). Orphaned sidecars are finalized at startup.
- Retention prunes the oldest non-`keep` files when free disk falls under `MIN_FREE_GB`.

**History, alerts, system**
- SQLite (WAL): 1 s samples kept 24 h, 1 minute aggregates kept 90 d.
- Alert rules — receiver gone, fix lost, survey-in stalled, jamming, antenna open/short, disk low,
  logger backpressure, host temperature — raise events in the UI and optionally POST to
  `ALERT_WEBHOOK_URL` (ntfy/Discord/Telegram compatible).

**Web UI and API**
- A React SPA the daemon serves itself: Dashboard, Satellites (sky plot, C/N0, per-signal table),
  Receiver (RF, jamming, AGC, spectrum, comms, time), Corrections, Site & Position, Logs,
  History, Events, Settings. Live over one WebSocket, dark and light, usable on a phone.
- REST API for everything the UI does — status, state, configuration with `.env` write-back,
  receiver and base-mode commands, caster clients, log download/keep/delete, history, events,
  background jobs — plus an interactive reference at `/api/docs`.
- Optional single-password login; binding the web UI publicly without a password is refused.

**Operations**
- Replay any recorded `.ubx` file as a fake receiver — full UI and API, no hardware.
- `mtrtk doctor` checks Python, receiver access, Tailscale, RTKLIB and free disk before you deploy.
- Docker Compose with host networking and hotplug-safe `/dev` access (no `privileged`).

## Quick start (Docker)

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
cp .env.example .env            # set NTRIP_PASSWORD at minimum
docker compose up -d
docker compose logs -f          # one status line per second once the receiver is configured
```

Then open `http://<tailscale-ip>:8080`.

## Quick start (without Docker)

```bash
uv sync
uv run mtrtk doctor             # python, serial access, tailscale, RTKLIB, disk
uv run mtrtk base
```

No hardware? Replay the committed fixture — the whole UI comes up on it:

```bash
uv run mtrtk replay tests/fixtures/f9p_hpg113_raw_10s.ubx --speed 10 --loop
```

## Configuration

Everything is environment variables, read from `.env` (see `.env.example` for the annotated
list). The ones you actually have to think about:

| Variable | Default | What it does |
|---|---|---|
| `ROLE` | `base` | `base` or `rover` |
| `MTRTK_SOURCE` | `auto` | `auto`, a serial device path, or `file:<path>.ubx` to replay |
| `DATA_DIR` | `/data` | Raw logs, exports and the SQLite database |
| `STATION_ID` | `MTRK` | Names the log files and the RINEX marker |
| `BASE_MODE` | `survey-in` | `survey-in`, `fixed` (with `ACTIVE_SITE`) or `off` |
| `SVIN_MIN_DURATION_S` / `SVIN_ACC_LIMIT_M` | `300` / `2.0` | When a survey-in is allowed to finish |
| `NTRIP_BIND` / `NTRIP_PORT` / `MOUNTPOINT` | `tailscale` / `2101` / `MTRK` | Where rovers connect |
| `NTRIP_USER` / `NTRIP_PASSWORD` | `rover` / — | Caster auth; empty password = anonymous |
| `WEB_BIND` / `WEB_PORT` / `WEB_PASSWORD` | `tailscale` / `8080` / — | Where the UI listens, and its login |
| `MIN_FREE_GB` | `5.0` | Prune oldest raw logs below this much free disk |
| `ALERT_WEBHOOK_URL` | — | POST alerts here as JSON |

`tailscale` binds the host's `tailscale0` address and retries until Tailscale is up — it never
silently falls back to `0.0.0.0`. `lan`, `all` and a literal IP also work.

## Commands

```
mtrtk base            # run as a base station
mtrtk rover           # run as a rover
mtrtk run             # run in whatever role ROLE says
mtrtk replay FILE     # replay a .ubx recording as a fake receiver (--speed, --loop)
mtrtk record --out F  # record the live receiver byte stream to a file
mtrtk doctor          # check python, serial access, tailscale, RTKLIB, disk
mtrtk healthcheck     # exit 0 when /healthz answers (this is the container healthcheck)
mtrtk sites list|add|activate|delete
```

## Documentation

- [`docs/base.md`](docs/base.md) — how the base station works: minimal `.env`, connecting rovers,
  survey-in, fixed sites, the 1005 check, files on disk, alerts.
- [`docs/ui.md`](docs/ui.md) — every page, live data and the stale rule, coordinate modes, the
  map, keyboard access, and what to do when something looks wrong.
- [`docs/api.md`](docs/api.md) — every route, the WebSocket protocol, authentication and status
  codes. Interactive version at `/api/docs` on a running daemon.

## Development

```bash
uv sync
uv run pytest tests/unit -q     # hardware tests are marked: -m hardware
uv run ruff check . && uv run mypy src
cd web && pnpm install && pnpm test && pnpm build
```

`pnpm build:static` copies the built SPA into `src/mtrtk/web/static`, which is what the daemon
serves; the Docker image builds it in its own stage. `-m hardware` tests need a real F9P and are
excluded by default.

## Status

Built and tagged: **Phase 1** receiver core, replay and record · **Phase 2** base daemon — raw
logging with retention, NTRIP caster, survey-in and fixed sites, SQLite history, alerts ·
**Phase 3** web API — FastAPI in-process, REST + WebSocket, `.env` write-back, jobs, optional
login · **Phase 4** web UI — the React SPA the daemon serves itself, nine pages plus login.

`ROLE=rover` today configures the receiver as a rover and logs its raw stream; the NTRIP client,
NMEA outputs and survey points come with the rover phase.

Planned, in order: RINEX export + PPP import (Phase 5), F9P rover (6), ROS2 bridge (7), PPK with
RTKLIB (8), public/Cloudflare exposure and hardening (9), SBG and VectorNav INS drivers (10).
