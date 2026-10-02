# Changelog

All notable changes to mtrtk are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/). `scripts/bump-version.py X.Y.Z` moves the Unreleased
entries under a dated `## [X.Y.Z]` heading; the release workflow publishes that section as the
GitHub Release notes (see `CONTRIBUTING.md`).

## [Unreleased]

### Added

- Base station (Phases 1-5): F9P auto-detect, reconnect and a verified `CFG-VALSET` profile
  (HPG 1.13 and 1.51); survey-in or FIXED from a saved site with the RTCM 1005 check; RTCM3 MSM7
  or MSM4 out through an in-process NTRIP caster (v1 and v2); hourly raw UBX logs on receiver
  time with sidecars and retention; RINEX export presets; PPP result import; SQLite history and
  alerts with an optional webhook.
- Web UI (Phases 1-5): Dashboard, Satellites, Receiver, Corrections, Site & Position, Logs,
  History, Events and Settings, live over one WebSocket.
- Rover role (Phase 6): `ROLE=rover` on an F9P with an NTRIP client (`NTRIP_URL`, GGA upload),
  RTK status from NAV-RELPOSNED, NMEA over TCP, UDP, serial or a pty, a JSON UDP feed, sessions
  and averaged survey points with CSV/GeoJSON/KML/GPX export, rover alerts, and the RTK and
  Survey pages. See `docs/rover.md`.
- ROS 2 bridge (Phase 7): the `mtrtk_bridge` node and `mtrtk_msgs`, NavSatFix, velocity, RTK
  status, time marks and NMEA topics from the daemon's WebSocket; Humble and Jazzy images, the
  `ros2` compose profile and a CI build that runs the image. See `docs/ros2.md`.
- PPK (Phase 8): `mtrtk ppk` and `POST /api/ppk` run RTKLIB rnx2rtkp on a session, a window or
  an upload against a local, remote or uploaded base, and write the track (CSV, GeoJSON, KML),
  camera events and a summary; the PPK page. See `docs/ppk.md`.
- INS drivers for SBG Ellipse-D and VectorNav VN-200 (spec-based; hardware validation pending).
  `ROVER_DRIVER=sbg_ellipse|vectornav` with the `INS_*` settings, read-only by default
  (`INS_APPLY_CONFIG=0`); `mtrtk ins info|config|monitor`; INS panels on the Receiver page, an
  IMU card on the Dashboard, INS alerts and doctor checks; NMEA `HDT`/`PASHR`; the Ellipse's raw
  GNSS re-framed into the hourly `.ubx` logs. See `docs/ins-drivers.md` for the
  verified/unverified matrix and the hardware validation checklist.
- INS replay: `MTRTK_SOURCE=file:<capture>` with an INS driver replays a recorded sbgECom or
  VectorNav stream (no `INS_PORT` needed), paced on host time at `INS_BAUD`
  (`FileReplaySource(pace="host")`); `tests/fixtures/ins/sbg_frames.bin` and `vn_frames.bin`.
- ROS 2 bridge: `/mtrtk/imu` and `/mtrtk/heading` publish on an INS rover (the bridge reads the
  attitude from the WebSocket `ins` topic).
- Exposure profiles (Phase 9): the `public` compose profile puts Caddy in front of the web UI
  with Let's Encrypt HTTPS on `PUBLIC_DOMAIN` (HSTS, optional `ACME_EMAIL`), and the
  `cloudflare` profile runs a remotely managed Cloudflare Tunnel from `TUNNEL_TOKEN`. Tailscale
  stays the default. `scripts/check-exposure.sh` checks `/healthz` and that NTRIP v2 delivers
  RTCM3 frames through the chosen path. See `docs/exposure.md`.
- Container hardening (Phase 9): the image runs as an unprivileged `mtrtk` user, behind an
  entrypoint that fixes the ownership of `/data` on first run (`MTRTK_RUN_AS_ROOT=1` opts
  out); compose drops every capability except the four the root entrypoint needs to chown
  `/data` and drop privileges (the daemon itself runs with none), sets `no-new-privileges`, gives
  the container its own `/dev/shm` and caps the json-file logs.
  `LOG_LEVEL` sets the daemon's log level.
- Native install (Phase 9): `install.sh` (with `--dry-run`) sets up uv, the virtualenv, RTKLIB
  demo5, the web UI, a `.env` for the native layout, the u-blox udev rule, `dialout` and
  `mtrtk.service`; `uninstall.sh` removes the unit and the rule and keeps the data and `.env`.
- `mtrtk doctor` host checks (Phase 9): ModemManager against the udev ignore rule, time sync,
  who holds the caster and web ports, docker, a writable data dir, what is exposed beyond
  Tailscale (Cloudflare Tunnel included) and, with `--probe`, the firmware age. `--json` prints
  the checks; the exit code is 1 only on a FAIL. `TUNNEL_TOKEN` is a Settings field, masked as
  a secret.
- Backup and restore (Phase 9): `mtrtk backup` writes an owner-only archive with a WAL-safe
  SQLite snapshot, the sites as JSON and the `.env` with secrets masked (`--with-secrets` keeps
  them). `mtrtk restore` checks the database, refuses while another process holds it, keeps the
  database it replaces and writes the archived `.env` beside the current one for a hand merge.
- Release pipeline (Phase 9): `release.yml` on a `vX.Y.Z` tag checks the tag against the
  package version and the changelog, runs the unit tests, smoke-tests and pushes multi-arch
  (amd64, arm64) images `ghcr.io/<owner>/mtrtk:X.Y.Z` and
  `ghcr.io/<owner>/mtrtk-ros2:{X.Y.Z-humble,X.Y.Z-jazzy}`, then moves `latest`, `humble` and
  `jazzy` once every image is up (and only for the newest release), and publishes a GitHub
  Release with this file's section as the notes and the docs as an asset. Every push to `main`
  publishes `ghcr.io/<owner>/mtrtk:edge`. `scripts/bump-version.py` sets every version source
  and `--notes` prints a release's section.

- `RECEIVER_ACK_TIMEOUT_S` (default 2, 0.5-30): how long each poll, `CFG-VALSET` and
  `CFG-VALGET` waits for the receiver, for a receiver reached over a slow tunnel (socat over a
  Tailscale relay). One missed answer to the optional-feature probe is asked again rather than
  hiding MON-COMMS; a probe that meets a link gone quiet fails at once and reconnects.
- `POST /api/ppk` takes `max_gap_s` (default 2): the widest gap between track epochs a camera
  event may be interpolated across.
- Web UI: a "Skip to content" link as the first Tab stop, and a Retry button on a map whose code
  failed to load.
- `WEB_ALLOWED_HOSTS`: the host names a UI without `WEB_PASSWORD` also answers to (see
  Security).

### Changed

- `RECEIVER_STRICT=1`: a profile refused at startup still exits 1, but once the receiver has been
  configured, a refused or unanswered reconnect is reported as `receiver.error` and retried with
  backoff (up to 30 s) instead of ending the process. A first start whose configuration readback
  gets no answers at all now says to check the link or raise `RECEIVER_ACK_TIMEOUT_S`, not to set
  `RECEIVER_STRICT=0`.
- `mtrtk doctor` is stricter about the internet: a `WEB_PASSWORD` under 16 characters FAILs once
  `PUBLIC_DOMAIN`, `TUNNEL_TOKEN` or a public address puts the login on the internet; Cloudflare
  Access mode on `WEB_BIND=lan` FAILs; `lan` on a host with a public address is judged as `all`;
  an anonymous caster with `PUBLIC_DOMAIN`, and the template's `NTRIP_PASSWORD=change-me` on an
  exposed caster, warn. A rover is no longer failed on the caster's `NTRIP_BIND`. `--probe` also
  spots a daemon that has not bound its ports yet.
- `scripts/check-exposure.sh` reads the NTRIP password from `NTRIP_PASSWORD` or asks for it; a
  4th argument still works, with a warning, since it shows up in `ps` and the shell history.
- Compose pins `caddy` (2.11.4) and `cloudflared` (2026.9.3) to their digests instead of floating
  tags; `install.sh` installs uv 0.9.30 and makes `.env` owner-only; the native unit sets
  `UMask=0027` and more sandboxing.

- `ghcr.io/<owner>/mtrtk:latest`, the `docker-compose.yml` default, now means the newest release
  and changes only when a `vX.Y.Z` tag is released; it no longer follows `main`. `main` publishes
  `:edge` instead, and `:main` is no longer updated. Until the first release, `docker compose pull`
  keeps the last `:latest` built from `main`; point `image:` at `:edge` (in a
  `docker-compose.override.yml`) to track `main`.
- `.env.example` no longer sets `INS_MOTION_PROFILE=general`: any value in `.env`, `general`
  included, makes the SBG driver write the motion profile on apply. A `.env` copied from the
  older template still has the line; delete it to leave the unit's own profile alone.
- With an INS driver, a `MTRTK_SOURCE=file:` line left in `.env` now replays that capture under
  `mtrtk run` (it used to be ignored); delete it to read the unit on `INS_PORT`. The
  `mtrtk ins` tools always use `INS_PORT`, and `ins info` / `ins config` exit 1 when the unit
  never answers.

### Security

- A UI without `WEB_PASSWORD` answers only to an IP address, localhost, the host's own names, its
  MagicDNS name, `PUBLIC_DOMAIN` and `WEB_ALLOWED_HOSTS`, and refuses a cross-site WebSocket:
  DNS rebinding could otherwise drive the whole API from a browser on the tailnet or the
  station. Behind Cloudflare Access, list the tunnel hostname in `WEB_ALLOWED_HOSTS`.
- Failed logins are throttled for the whole daemon (1 s each; after ten in a row, `429` until a
  bucket refills one attempt every 6 s).
- The login cookie is `Secure`, and HSTS is sent, whenever the request came over HTTPS (directly
  or `X-Forwarded-Proto` from Caddy or cloudflared). Every response carries `nosniff`,
  `frame-ancestors 'none'` and `no-referrer`, and no `Server` header.
- The masked backup also masks a secret on a BOM-led first line, `SECRET=value` inside comment
  prose and credential query parameters in URLs; a URL password with an `@` and a later `/`, `?`
  or `#` is masked whole in `GET /api/config`.
- `.env.*` copies (`install.sh`'s `.env.bak-<UTC>`) are git- and docker-ignored. CI actions are
  pinned to commit SHAs, `ci.yml` defaults to a read-only token, and a release tag must be on
  `main`.
