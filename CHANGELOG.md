# Changelog

All notable changes to mtrtk are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- INS drivers for SBG Ellipse-D and VectorNav VN-200 (spec-based; hardware validation pending).
  `ROVER_DRIVER=sbg_ellipse|vectornav` with the `INS_*` settings, read-only by default
  (`INS_APPLY_CONFIG=0`); `mtrtk ins info|config|monitor`; INS panels on the Receiver page, an
  IMU card on the Dashboard, INS alerts and doctor checks; NMEA `HDT`/`PASHR`; the Ellipse's raw
  GNSS re-framed into the hourly `.ubx` logs. See `docs/ins-drivers.md` for the
  verified/unverified matrix and the hardware validation checklist.
