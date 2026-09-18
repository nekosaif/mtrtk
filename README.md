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

Replay a recording with no hardware: `uv run mtrtk replay tests/fixtures/f9p_hpg113_raw_10s.ubx --speed 10`.

## Status

Phase 1 (receiver core) complete: framing, live state, receiver configuration with firmware capability
probing, replay mode, Docker image. Next: raw logging + NTRIP caster (Phase 2), web API (Phase 3), UI (Phase 4).
