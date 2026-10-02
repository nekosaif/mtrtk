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
- Release pipeline (Phase 9): `release.yml` on a `vX.Y.Z` tag checks the tag against the
  package version and the changelog, runs the unit tests, smoke-tests and pushes multi-arch
  (amd64, arm64) images `ghcr.io/<owner>/mtrtk:X.Y.Z` and
  `ghcr.io/<owner>/mtrtk-ros2:{X.Y.Z-humble,X.Y.Z-jazzy}`, then moves `latest`, `humble` and
  `jazzy` once every image is up (and only for the newest release), and publishes a GitHub
  Release with this file's section as the notes and the docs as an asset. Every push to `main`
  publishes `ghcr.io/<owner>/mtrtk:edge`. `scripts/bump-version.py` sets every version source
  and `--notes` prints a release's section.

### Changed

- `ghcr.io/<owner>/mtrtk:latest`, the `docker-compose.yml` default, now means the newest release
  and changes only when a `vX.Y.Z` tag is released; it no longer follows `main`. `main` publishes
  `:edge` instead, and `:main` is no longer updated. Until the first release, `docker compose pull`
  keeps the last `:latest` built from `main`; point `image:` at `:edge` (in a
  `docker-compose.override.yml`) to track `main`.
- `.env.example` no longer sets `INS_MOTION_PROFILE=general`: any value in `.env`, `general`
  included, makes the SBG driver write the motion profile on apply. A `.env` copied from the
  older template still has the line; delete it to leave the unit's own profile alone.
