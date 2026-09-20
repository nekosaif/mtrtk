# mtrtk

Multi-role GNSS toolkit for u-blox ZED-F9P: RTK base station (NTRIP caster, raw UBX logging,
RINEX export for PPP), rovers (NTRIP client, NMEA/ROS2 outputs, survey points) and PPK
post-processing. Design: `docs/superpowers/specs/2026-09-18-mtrtk-design.md`.

## Development

```bash
uv sync
uv run pytest
uv run mtrtk --help
```

## Quick start (base station)

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
cp .env.example .env            # set NTRIP_PASSWORD at minimum
docker compose up -d
docker compose logs -f          # one status line per second once the receiver is configured
```

Without Docker: `uv sync && uv run mtrtk doctor && uv run mtrtk base`.

How the base station works, how rovers connect and how to move from survey-in to a surveyed
site: `docs/base.md`.

Replay a recording with no hardware: `uv run mtrtk replay tests/fixtures/f9p_hpg113_raw_10s.ubx --speed 10`.

The daemon serves its own web UI, HTTP API and WebSocket on `WEB_BIND:8080` -
`http://<tailscale-ip>:8080` by default, with the interactive API reference at `/api/docs`. What
every page shows, how live data and the coordinate modes work, and what to do when something looks
wrong: `docs/ui.md`. Every route, the WebSocket protocol and the authentication flow: `docs/api.md`.
`mtrtk healthcheck` is the same `/healthz` request the container healthcheck makes.

## Status

Phase 4 (web UI) complete: dashboard, satellites, receiver, corrections, site, logs, history,
events, settings and login - a React SPA the daemon serves itself, live over one WebSocket, dark
and light, usable on a phone in the field (`docs/ui.md`). Phase 3 (web API) before it: FastAPI
served in-process by the daemon - status, state and host system, configuration with `.env`
write-back, receiver and base-mode commands, NTRIP caster and raw-log management, history and
events, background jobs, a WebSocket stream and an optional single-password login. Phase 2 (base
daemon): hourly raw logging + retention, NTRIP caster (v1/v2), survey-in / fixed sites with RTCM
1005 verification, SQLite history, alerts.
Next: RINEX export + PPP import (Phase 5).
