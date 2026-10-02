# Changelog

All notable changes to mtrtk are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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

### Changed

- `.env.example` no longer sets `INS_MOTION_PROFILE=general`: any value in `.env`, `general`
  included, makes the SBG driver write the motion profile on apply. A `.env` copied from the
  older template still has the line; delete it to leave the unit's own profile alone.
