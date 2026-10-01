# INS drivers: SBG Ellipse-D and VectorNav VN-200

`ROVER_DRIVER=sbg_ellipse` or `ROVER_DRIVER=vectornav` replaces the u-blox rover receiver with
an inertial navigation unit on `INS_PORT`. The rest of the rover is unchanged: the NTRIP client,
NMEA/JSON outputs, survey points, the web UI and the ROS 2 bridge all read the same
`ReceiverState`, which the INS driver fills from the unit's own binary protocol (sbgECom on the
Ellipse, VectorNav binary output on the VN-200).

**Status.** The SBG driver has been run read-only against a real Ellipse-D. Framing, the log
layouts, the raw GNSS path to RINEX and the daemon, API and UI on a live stream are verified;
nothing has ever been written to that unit, so configuration, RTCM input and RTK are still
unverified. The VectorNav driver is built from the vendor documentation and vnproglib and has
not met a VN-200. The [verification matrix](#verified--unverified-matrix) lists every assumption
and its state, and the [hardware validation checklist](#hardware-validation-checklist) is what to
run when a unit is on the bench.

## Which unit for what

| Unit | Best for | Position | Heading | Corrections | Raw GNSS for PPK |
|---|---|---|---|---|---|
| u-blox ZED-F9P (`ublox`) | RTK survey, the cheapest rover | RTK fixed, cm | none (single antenna, no IMU) | RTCM3 on USB, verified | UBX RXM-RAWX, hourly `.ubx` |
| SBG Ellipse-D (`sbg_ellipse`) | vehicles and payloads that need attitude | EKF (GNSS + IMU), RTK when fed RTCM | dual-antenna GNSS heading plus IMU; heading at standstill | RTCM3 on Port B (documented) or Port A (unverified) | internal u-blox stream re-framed into hourly `.ubx` |
| VectorNav VN-200 (`vectornav`) | INS on a single antenna | INS solution from its own GNSS | from motion only (no dual antenna) | no documented RTCM input; opt-in, unverified | RawMeas to an opaque `.vnraw` capture, no RINEX |

Use the F9P when position is all you need. Use the Ellipse-D when you need roll, pitch and a
heading that holds when the vehicle stops. The VN-200 gives attitude and a smoothed INS
position, but its heading only settles once the vehicle moves, and its RTK and PPK paths are not
proven.

## Wiring

### SBG Ellipse-D

- **Port A** carries sbgECom, the binary protocol mtrtk speaks. It is RS-232 or TTL depending on
  the cable and variant (see the label and the Ellipse hardware manual). Connect it through a
  USB-serial adapter (FTDI) and name the adapter by id, which survives replugging:
  `INS_PORT=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXX-if00-port0`. FTDI ids are generic,
  so mtrtk never auto-detects an INS: the port is always explicit.
- **Port A must be in sbgECom mode** (sbgCenter → Interfaces → Port A). The bench unit runs it at
  921600 baud, 8N1. mtrtk never changes the baud rate (see [Configuration](#configuration)).
- **Port B** is the documented auxiliary input for RTCM. Wire it to a second USB-serial adapter
  and set `INS_RTCM_PORT` to that device; mtrtk then writes corrections there and sets the unit's
  aiding assignment to take RTCM on Port B. Port B has its own line rate, set in sbgCenter (115200
  is common for an RTCM input); mtrtk opens `INS_RTCM_PORT` at `INS_RTCM_BAUD`, else at
  `INS_BAUD`, so set `INS_RTCM_BAUD` to Port B's rate. mtrtk reads Port B's rate on connect (it
  never writes it) and reports a mismatch as an error: RTCM sent at the wrong rate never decodes,
  and every write still succeeds at the host end. Without `INS_RTCM_PORT` corrections go onto
  Port A next to the sbgECom traffic. That path is undocumented and unverified
  (`sbg-rtcm-port-a`). Whether one cable can carry both is also unverified. With neither
  `NTRIP_URL` nor `INS_RTCM_PORT` set, mtrtk feeds no corrections and leaves the aiding
  assignment as it is, so an RTCM input the owner set up (a radio modem on Port B) keeps working.
- **Antennas.** The Ellipse-D is dual antenna: the primary antenna gives position, the secondary
  gives the GNSS heading together with it. Mount them along a rigid baseline (the bench unit
  measures 1.22 m) with a clear sky view.
- **Lever arms.** Measure from the IMU (the reference point marked on the housing) to each
  antenna's reference point (ARP), in metres, in the vehicle frame: X forward, Y right, Z down.
  `INS_LEVER_ARM_GNSS1="x,y,z"` is the primary antenna, `INS_LEVER_ARM_GNSS2="x,y,z"` the
  secondary. A given primary arm is sent as precise (not re-estimated by the unit); a given
  secondary arm switches the unit to `DUAL_PRECISE`, so only set it once it is measured to about
  ±1 cm. Unset arms are left as configured in sbgCenter.
- **Sync In** (camera pulses) produce EVENT_A..E logs and become time marks. Which Sync In lines
  a given Ellipse-D variant has is unverified (`sbg-sync-in`).
- **Power** from a supply that holds through the unit's reboot after a settings save (see
  [Troubleshooting](#troubleshooting)).

A unit on another machine works as well: any path that ends in a serial-like device does. The
bench unit was read through a `socat` tunnel (on the far end, for example
`socat TCP-LISTEN:5002,bind=<tailscale-ip>,reuseaddr FILE:/dev/ttyUSB0,b921600,raw,echo=0`, locally
`socat PTY,link=$HOME/dev/ellipse,raw,echo=0 TCP:<host>:5002`, then `INS_PORT=$HOME/dev/ellipse`).
On a pseudo-terminal the baud setting is ignored. Keep something reading the link: an unread
tunnel backs up, and the far end's serial buffer overflows (the framer recovers, with a few
resyncs).

### VectorNav VN-200

- **Serial.** The rugged VN-200 and the surface-mount module expose TX, RX and GND on one or two
  serial ports, at 3.3 V TTL or RS-232 depending on the variant (see the VN-200 user manual,
  UM004, for the connector pinout). Never connect RS-232 levels to a TTL port. Use serial port 1
  through a USB-serial adapter: `INS_PORT=/dev/serial/by-id/usb-FTDI_…`.
- **Binary output.** mtrtk configures binary output 1 on serial port 1 (async mode 1) and turns
  the ASCII async output off (register 6 = 0) once the binary stream reaches mtrtk's port.
- **GNSS antenna offset.** `INS_LEVER_ARM_GNSS1="x,y,z"` is written to register 57 (GNSS antenna
  A offset): the antenna position relative to the sensor, in metres, in the sensor's body frame
  (X forward, Y right, Z down when it is mounted aligned). Use `INS_VN_REF_ROTATION` (register 26)
  when the sensor is not mounted aligned with the vehicle.
- **SyncIn** for camera events: each rising edge raises the SyncIn count, and the driver dates a
  time mark (channel 0) from the frame time minus `TimeSyncIn`.

## Configuration

Every key, its default and what it maps to on each unit. Keys that do not apply to a vendor are
ignored by its driver; `INS_MOTION_PROFILE` on a VN-200 adds a note to the configuration report.

| Variable | Default | SBG Ellipse-D | VectorNav VN-200 |
|---|---|---|---|
| `ROVER_DRIVER` | `ublox` | `sbg_ellipse` | `vectornav` |
| `INS_PORT` | — (required) | Port A device | serial port 1 device |
| `INS_BAUD` | `115200` | host line rate; must match Port A (bench unit: 921600) | host line rate; must match register 5 |
| `INS_RTCM_PORT` | — | second device on Port B; aiding assignment `rtcmPort = PORT_B`. Without it, `PORT_A` when `NTRIP_URL` is set; with neither, the aiding assignment is not touched | not used |
| `INS_RTCM_BAUD` | `INS_BAUD` | line rate `INS_RTCM_PORT` is opened at; must match Port B (read on connect, a mismatch is an error) | not used |
| `INS_OUTPUT_HZ` | `10` | output mode (divider of the 200 Hz loop) of IMU_SHORT, EKF_EULER and EKF_NAV; one of 1, 2, 5, 10, 20, 25, 40, 50, 100, 200 | rate divisor of 800 Hz for binary output 1; a rate it cannot hit is rounded up to the next faster one, with a note |
| `INS_APPLY_CONFIG` | `0` | `1`: write and save the profile on connect | same |
| `INS_RAW_GNSS` | `1` | GPS1_RAW output on (re-framed to `.ubx`) | RawMeas extension in binary output 1 (`.vnraw` capture) |
| `INS_LEVER_ARM_GNSS1` | — | GNSS_1_INSTALLATION primary lever arm, precise | register 57 antenna offset |
| `INS_LEVER_ARM_GNSS2` | — | secondary lever arm and `DUAL_PRECISE` mode | not used |
| `INS_IMU_LEVER_ARM` | — | IMU_ALIGNMENT lever arm | not used |
| `INS_IMU_AXIS` | `xyz` | IMU_ALIGNMENT axes: `xyz` leaves them; `<x>,<y>` names where the IMU X and Y axes point, from forward, backward, left, right, up, down (`forward,right` is aligned) | not used (use `INS_VN_REF_ROTATION`) |
| `INS_MOTION_PROFILE` | `general` | MOTION_PROFILE, only when set explicitly: general, automotive, marine, airplane, helicopter, uav (rotary wing), pedestrian. Any value in `.env`, `general` included, is applied; delete the key to leave the unit's own profile alone (a `.env` copied from an older template has `INS_MOTION_PROFILE=general`) | not used: set `INS_VN_SCENARIO` |
| `INS_INIT_POSITION` | — | INIT_PARAMETERS: `lat,lon,alt` (degrees, metres) and today's date | not used |
| `INS_VN_RTCM` | `0` | — | `1` forwards RTCM to the unit (`vn-rtcm`) |
| `INS_VN_SCENARIO` / `INS_VN_AHRS_AIDING` | — | — | register 67 INS basic configuration (`vn-reg67-scenario`) |
| `INS_VN_REF_ROTATION` | — | — | register 26, nine comma-separated numbers, row-major |
| `INS_VN_VPE` | — | — | register 35 VPE basic control `enable,headingMode,filteringMode,tuningMode` |

Besides these keys, the SBG profile sets the Port A outputs mtrtk reads: STATUS and UTC_TIME at
1 Hz; IMU_SHORT, EKF_EULER and EKF_NAV at `INS_OUTPUT_HZ`; GPS1_POS, VEL, HDT, SAT and RAW,
RTCM_RAW and EVENT_A..E on new data. It turns EKF_QUAT, IMU_DATA, SHIP_MOTION and MAG off, and
the NMEA output classes on Port A. The VN profile sets binary output 1 to the Time, IMU, GPS
(with SatInfo, and RawMeas when `INS_RAW_GNSS=1`), Attitude and INS groups. When the unit
refuses that, the profile is retried without RawMeas and then without SatInfo, and the driver's
capabilities follow what the unit kept.

### Read-only first

`INS_APPLY_CONFIG=0` (the default) never changes the unit's settings. On every connect mtrtk
still sends queries (SBG GET commands, VN register reads) to read the identity and the current
value of each profile item. The Receiver page and `mtrtk ins info` show them next to what the
profile wants. Applying is a decision you take after reading that report:

```bash
uv run mtrtk ins info              # identity and the configuration table (state / item / current)
uv run mtrtk ins config --dry-run  # "would write <item>: <current> -> <wanted>" for each difference
uv run mtrtk ins config --apply    # write, read back, report applied / mismatched
uv run mtrtk ins monitor           # one line per epoch: UTC, INS mode, lat/lon, heading, fix
```

The `mtrtk ins` commands open `INS_PORT` themselves, so run them with the daemon stopped.
`monitor` never writes to the unit. The web equivalent is the Receiver page's INS configuration
panel ("Re-read configuration", and "Apply INS configuration" behind a confirm), or
`POST /api/receiver/profile`.

Every write is read back, and the read-back is the evidence, not the ACK. An item that reads back
different from what was written is `mismatched`, and then nothing is saved.

### What is saved, and when

- **SBG:** with `INS_APPLY_CONFIG=1`, when something was applied and nothing mismatched, mtrtk
  sends `SAVE_SETTINGS` once per process. **The Ellipse reboots on save**; the controller rides
  out the reboot or reconnects, and the next read finds everything unchanged.
- **VN-200:** under the same conditions, and once binary output 1 is seen streaming, mtrtk sends
  `$VNWNV` (write settings to flash; no reboot).
- **An explicit apply with `INS_APPLY_CONFIG=0`** (`mtrtk ins config --apply`, the UI's confirm)
  writes RAM only on either vendor: the changes last until the unit restarts. Whether every SBG
  setting takes effect before a save and reboot is unverified (`sbg-ram-apply`): the read-back
  shows the unit holds the value, not that it already uses it. mtrtk remembers such an apply
  (`DATA_DIR/ins/<vendor>-<serial>.ram-only`): once `INS_APPLY_CONFIG=1` and the whole profile
  reads back in place, the next connect saves it to flash once, although nothing then reads as
  changed.

### Baud rate

mtrtk never writes the baud rate: a wrong value would strand the link. Set it once in sbgCenter
(Ellipse Port A) or VectorNav Control Center (register 5), then set `INS_BAUD` to match. Use
460800 or more when `INS_OUTPUT_HZ > 50` or raw GNSS is on (`mtrtk doctor` warns otherwise).
Above 50 Hz on a slower SBG link, the over-rate outputs are held (not written) and nothing is
saved. When the unit's baud cannot be read, `INS_BAUD` stands in for it, so the check fails
closed. On VectorNav, the configuration report notes when binary output 1 at the chosen rate
would not fit `INS_BAUD`.

### Replaying a capture

With no unit on hand, `MTRTK_SOURCE=file:<capture>` replays a recorded sbgECom or VectorNav
binary stream through the same stack, and `INS_PORT` is not needed:

```bash
ROLE=rover ROVER_DRIVER=sbg_ellipse MTRTK_SOURCE=file:tests/fixtures/ins/sbg_frames.bin \
  REPLAY_LOOP=1 NMEA_SENTENCES=GGA,RMC,HDT,PASHR DATA_DIR=/tmp/mtrtk-replay mtrtk run
```

(`vectornav` with `tests/fixtures/ins/vn_frames.bin` works the same way, and so does
`tests/fixtures/ins/ellipse_d_live_1s.sbg`, one second read from the real unit with
`INS_BAUD=921600`; that unit was not aligned, so it shows status and IMU but no position.) A vendor
stream has no UBX time of week to pace on, so the replay is paced on host time: the file's bytes are
sent at the rate an 8N1 line at `INS_BAUD` delivers them, `REPLAY_SPEED` times faster (0 = as fast
as possible). A capture recorded on a link that was not saturated therefore plays faster than it was
recorded. A replay is passive, as a u-blox one: nothing is configured on connect, `INS_RTCM_PORT` is
not opened, raw captures are written only with `REPLAY_LOG=1`, and `/healthz` reports `passive`.

## What you get

`ReceiverState` sections each driver fills:

| Section | SBG Ellipse-D | VectorNav VN-200 |
|---|---|---|
| `position`, `accuracy`, `velocity` | EKF_NAV (HAE and MSL, 1σ, NED); not copied while the EKF says invalid (`position.invalid_llh`) | INS group (HAE, NED, 1σ) |
| `fix` | `fix_type` from the EKF (3 position valid, 2 velocity only, else 0); carrier solution, DGNSS flag and satellites from GPS1_POS | `fix_type` from InsStatus; carrier solution from GPS Fix 7/8 (`vn-fix-rtk`) |
| `dops` | — | GPS DOP |
| `time` | UTC_TIME, carried to each epoch's device time stamp; leap seconds from its GPS time of week | Time group (UTC, GPS week and time of week, TimeStatus) |
| `sats`, `sat_summary` | GPS1_SAT, per signal | SatInfo (`vn-satinfo`) |
| `attitude` | EKF_EULER (source `sbg-ekf`), else the GPS1_HDT dual-antenna heading (`sbg-gnss-hdt`); at most 10 Hz | Attitude group (`vn-ins`) |
| `imu` | IMU_SHORT, at most 10 Hz | IMU group, at most 10 Hz |
| `ins` | EKF mode, STATUS health and aiding flags, GNSS fix type | InsStatus mode and error flags, GNSS fix |
| `rtk` | GPS1_POS carrier solution, base id, correction age; GPS1_HDT heading and antenna baseline; RTCM_RAW echo count | carrier solution from GPS Fix |
| `time_marks` | EVENT_A..E, one mark per edge | SyncIn count (channel 0) |

What lights up downstream:

- **NMEA:** `HDT` and `PASHR` (add them to `NMEA_SENTENCES`) are written on every epoch that has
  a heading, also before the EKF has a position: a dual-antenna Ellipse-D's GPS1_HDT heading is
  valid while the filter is still aligning, and goes out then. GGA/RMC and the other position
  sentences carry the INS position and wait for it.
- **Web UI:** the Receiver page swaps RF and spectrum for the INS panels (unit, filter, IMU,
  lever arms, configuration); the Dashboard gets an IMU card, and the RTK page a corrections-path
  notice. Alerts: `ins_not_aligned`, `ins_gnss_lost`, `ins_config_mismatch`, `imu_error`.
- **WebSocket:** topic `ins` carries `{ins, imu, attitude}` with each (decimated) epoch, plus the
  configuration reports.
- **ROS 2:** the bridge subscribes to the `ins` topic and publishes `/mtrtk/imu` (orientation
  only) and `/mtrtk/heading` on each epoch whose attitude has a heading (see `docs/ros2.md`).

On the RTK page and the tape, an Ellipse-D's "baseline" and "bearing" are the dual-antenna
baseline (about 1.2 m) and heading from GPS1_HDT, not the distance and bearing to the base.

## Raw GNSS and PPK

- **SBG:** GPS1_RAW is the internal u-blox receiver's own UBX stream, cut into chunks without
  regard to its framing. The driver re-frames it and hands the UBX frames to the normal raw
  logger: hourly `DATA_DIR/ubx/YYYY/DDD/{STATION}_{YYYYMMDD}_{HH}.ubx`, named by the unit's UTC,
  with the usual sidecar. RINEX export, PPP and `mtrtk ppk` work on these files unchanged.
  Verified on the real unit: RXM-RAWX, RXM-SFRBX and SEC-SIG, dual frequency (GPS L1C/L2C,
  GLONASS L1/L2, Galileo E1/E5b, BeiDou B1I/B2I), converted by `convbin`. If a unit's stream is
  not UBX, after 8 kB without a valid frame the driver reports
  `GPS1_RAW does not look like UBX` once and writes the bytes to an opaque hourly
  `DATA_DIR/ins/YYYY/DDD/{STATION}_{YYYYMMDD}_{HH}.sbgraw` instead.
- **Camera events with SBG:** Sync In marks become live `time_marks` (UI, API, ROS time marks),
  but they come from EVENT logs, not from the receiver, so they are not in the `.ubx` file.
  `mtrtk ppk` reads its events from TIM-TM2 in the UBX log, so its `events.csv` will be empty
  for an Ellipse.
- **VN-200:** RawMeas goes to an opaque capture, `DATA_DIR/ins/YYYY/DDD/{STATION}_{YYYYMMDD}_{HH}.vnraw`
  with a JSON sidecar. **RINEX conversion is not implemented.** The format, for whoever writes
  the converter (all little-endian, layout from the manual, `vn-rawmeas-layout`):

  ```
  record   = "VNRM" (4 bytes) | length u16 | RawMeas field (length bytes)
  RawMeas  = tow f64 (s) | week u16 | numMeas u8 | reserved u8 | numMeas × measurement
  measurement (28 bytes) = sys u8 | svId u8 | freq u8 | chan u8 | slot i8 | cno u8 |
                           flags u16 | pseudorange f64 (m) | carrier phase f64 (cycles) |
                           doppler f32 (Hz)
  ```

  One record per binary output frame that carried RawMeas, in arrival order. Files rotate on the
  unit's UTC (TimeStatus UTC valid); before the first valid UTC they are named by the host clock.

## Verified / unverified matrix

Every assumption that only a real unit can settle is tagged in the code (the drivers and
`src/mtrtk/config.py`) as `VERIFY(<tag>)` and has a row in one of the tables below.
`tests/unit/test_verify_markers.py` fails when a tag has no row, and when a row of the
Unverified table has no marker in the code. Rows of the other two tables carry no marker: they
were verified on the unit, or come from the primary source and hold for a whole module.

States: **verified-live** (seen on the real Ellipse-D, read-only, on the date given),
**primary source** (taken from sbgECom 5.8.935-stable or vnproglib 1.2 / the VN-200 manual, not
yet seen on a unit), **unverified** (an assumption until hardware says otherwise).

### Verified on the real Ellipse-D

All on 2026-10-01 (UTC), read-only, on the bench unit (dual antenna, internal u-blox receiver,
Port A at 921600). The unit was never aligned during these sessions (EKF in Vertical gyro mode),
and nothing was written to it.

| Tag | What was confirmed | How |
|---|---|---|
| `sbg-framing` | sbgECom frame, CRC-16/KERMIT, class and id tables | 6,548 frames in 20 s with 0 CRC failures; steady runs through the tunnel the same (a backlog drained after the link sat unread shows a few, from the link) |
| `sbg-log-layouts` | STATUS (27 B), UTC_TIME (33 B), IMU_SHORT (32 B), EKF_EULER (40 B), EKF_NAV (72 B), GPS1_POS (62 B), GPS1_VEL (44 B), GPS1_HDT (32 B), GPS1_SAT, GPS1_RAW: every field parsed, optional tails as listed | 0 parse failures; EKF_NAV undulation equal to GPS1_POS |
| `sbg-gps1-raw-ubx` | GPS1_RAW is UBX (RXM-RAWX, RXM-SFRBX, SEC-SIG), split across chunks | re-framed into hourly `.ubx` by the daemon's raw logger; `convbin` produced dual-frequency RINEX |
| `sbg-gyro-scale` | IMU_SHORT uses the standard gyro scale (status bit 10 clear) | thousands of frames per run, 0 high-range; accel z −9.80 m/s² |
| `sbg-utc-status` | UTC_TIME status bits; GPS−UTC = 18 s from its GPS time of week | status 0xA7 decoded as clock valid, UTC initialised; time of week − 18 s matches the UTC time |
| `sbg-signal-ids` | constellation enum (1 GPS, 2 GLONASS, 3 Galileo, 4 BeiDou, 6 SBAS) and signal ids | ids seen live match sbgEComDefsGnss.h and map to the UI's names |
| `sbg-ekf-status` | EKF status decoding: mode, attitude valid, heading invalid | mode 1 with yaw σ 180°; the GPS1_HDT heading stands in |
| `sbg-timestamp` | the u32 device time stamp wraps (unit up longer than 4,295 s), and a real gap is caught as a jump | a 322.8 s tunnel gap was detected and the anchor dropped |
| `sbg-daemon-live` | daemon on a live stream: API `driver`/`ins` blocks, INS panels, `ins_not_aligned` after 60 s, status line | throwaway daemon with a port wrapper that refuses writes (0 writes) |

### From the primary source, not yet seen on a unit

| Tag | Assumption | Where | How to verify | If wrong |
|---|---|---|---|---|
| `sbg-version-packing` | INFO layout and sbgVersion packing (basic and software schemes) | `sbg/logs.py` `decode_version` | `mtrtk ins info`: model, serial and firmware read sensibly (compare with sbgCenter) | firmware shows odd numbers; the raw value is kept next to it |
| `sbg-commands-ack` | command payloads (output, installation, alignment, aiding, init, motion profile, UART) and ACK matching by id | `sbg/commands.py`, `sbg/config.py` | `mtrtk ins config --dry-run`, then `--apply`; every item `applied` | items `mismatched` or `error`; nothing is saved |
| `sbg-extended-frames` | extended (paged) frames are marked by class bit 7 and are dropped | `sbg/framer.py` | none expected: no log mtrtk enables is that large | a log mtrtk does not read is skipped |
| `vn-binary-layout` | binary output group bits, field sizes and the frame CRC | `vectornav/fields.py`, `framer.py` | `mtrtk ins monitor` shows sensible epochs | frames fail CRC or parse wrong values |
| `vn-registers` | register numbers, `$VNRRG`/`$VNWRG` formats, error codes, the ASCII checksum | `vectornav/registers.py` | `mtrtk ins info` reads model, serial and firmware | configure reports errors; reads fail |

### Unverified until hardware

| Tag | Assumption | Where | How to verify | Fallback if wrong |
|---|---|---|---|---|
| `sbg-rtcm-port-a` | the Ellipse takes RTCM on Port A next to sbgECom | `sbg/driver.py`, `sbg/config.py` | NTRIP on, no `INS_RTCM_PORT`: RTCM_RAW echo counts rise and GPS1_POS reaches RTK | the UI shows "RTCM path unverified"; wire Port B and set `INS_RTCM_PORT` |
| `sbg-ram-apply` | a SET takes effect before `SAVE_SETTINGS` and the reboot | `sbg/config.py` | change the motion profile with `INS_APPLY_CONFIG=0` and watch the EKF behaviour change before any save | treat a forced apply as staged until saved: set `INS_APPLY_CONFIG=1` and let it save |
| `sbg-rtk-pos-type` | GPS1_POS reports RTK float/fixed as type 6/7 with a correction age | `sbg/adapter.py` | with corrections, type reaches 7 and the age stays under 5 s | the RTK page stays "None" with corrections flowing |
| `sbg-ekf-nav-valid` | EKF_NAV mapping with a valid, aligned solution | `sbg/adapter.py` | outdoors, moving: mode reaches Nav position and the position matches GPS1_POS | no position published while unaligned (by design) |
| `sbg-clock-outage` | the unit's clock leaves VALID during a GNSS outage | `sbg/adapter.py` | cover the antennas: UTC flagged not valid, time marks held | epochs dated from a clock the unit no longer vouches for |
| `sbg-held-drift` | 20 ppm bounds the device clock drift over a held time mark | `sbg/adapter.py` `HELD_DRIFT` | compare UTC_TIME `clk_sf_error_std` once its unit is known | a held mark's `acc_est_ns` is too optimistic or too wide |
| `sbg-event-offsets` | EVENT time offsets count from the log's own time stamp | `sbg/adapter.py` | pulse Sync In at a known rate: marks evenly spaced, matching the pulse source | marks off by up to one output period |
| `sbg-sync-in` | which Sync In lines (EVENT_A..E) the Ellipse-D has | `sbg/config.py` | enable all, pulse each input, see which channel counts | an absent channel is reported `unsupported`, harmless |
| `sbg-sbas-prn` | SBAS satellite id + 100 is the PRN | `sbg/adapter.py` | seen once (GAGAN id 27 = PRN 127); check another SBAS and QZSS | SBAS satellites show a wrong PRN in the sky plot |
| `vn-rtcm` | the VN-200 accepts RTCM on serial port 1 | `vectornav/driver.py` | `INS_VN_RTCM=1` with NTRIP: GPS Fix reaches 7 or 8 | stays opt-in; the UI says the VN-200 does not take corrections |
| `vn-fix-rtk` | GPS Fix 7/8 mean RTK float/fixed on a VN-200 | `vectornav/parse.py`, `adapter.py` | record the Fix values seen with RTCM forwarded | carrier solution never shown; position still correct |
| `vn-fix-sbas` | GPS Fix 4 means SBAS | `vectornav/parse.py` | record Fix with SBAS in view | a wrong fix name |
| `vn-satinfo` | SatInfo record layout (8 bytes), `sys` on the u-blox gnssId scale, flag bits | `vectornav/fields.py`, `parse.py` | Satellites page against VectorNav Control Center | wrong constellations or flags in the sky plot |
| `vn-rawmeas-layout` | RawMeas header (12 bytes) and record (28 bytes) layout, `sys` numbering | `vectornav/fields.py` | probe outcome in the configure report (`raw_meas`); decode one record by hand | the `.vnraw` capture is unreadable; position unaffected |
| `vn-reg75-ext` | register 75 encodes the GPS extension word after the field word with bit 15 set | `vectornav/registers.py` | `ins config --apply`: binary output 1 reads back with RawMeas | the unit refuses it; the probe drops RawMeas and retries |
| `vn-baud-error` | error code 12 means "output does not fit the baud rate" | `vectornav/config.py` | apply a too-fast output at a low baud and note the code | the probe stops early and keeps a wrong output set |
| `vn-reg67-scenario` | register 67 scenario values and field order | `vectornav/registers.py` | set `INS_VN_SCENARIO`, read back, compare with the manual | `mismatched` in the report; nothing saved |
| `vn-utc-year` | TimeUTC's year is a signed byte counted from 2000 | `vectornav/parse.py` | `time.utc` on the API matches the real date | wrong dates in `time.utc` and in the `.vnraw` file names |
| `vn-nan-uncertainty` | the unit sends NaN for an unknown uncertainty | `vectornav/adapter.py` | read the 1σ fields before alignment | NaN values become null (already handled) |

## Hardware validation checklist

Run this when a unit is on the bench, and record the results (date, firmware, what you saw)
in `docs/acceptance.md`. Start with a temporary `DATA_DIR` and `INS_APPLY_CONFIG=0`.

**Both units**

1. Connect, then `mtrtk doctor`: `ins_port` read/write OK, no baud warning for your settings.
2. `mtrtk ins info`: model, serial and firmware match the vendor tool (`sbg-version-packing`,
   `vn-registers`).
3. `mtrtk ins monitor` outdoors: the INS mode moves from aligning to tracking (SBG: Vertical gyro
   → AHRS → Nav velocity → Nav position; VN: Aligning → Tracking) once the vehicle moves.
4. `mtrtk ins config --dry-run`: review each `would write`. Then set `INS_APPLY_CONFIG=1`, run
   `mtrtk ins config --apply`, power-cycle the unit, and run `mtrtk ins info` again: every item
   is `unchanged` (the values persisted).
5. Run the rover daemon with NMEA `HDT` in `NMEA_SENTENCES`: a TCP client on `NMEA_TCP_PORT` sees
   `$GNHDT` and, with `PASHR`, `$PASHR`.
6. ROS 2 bridge: `/mtrtk/imu` and `/mtrtk/heading` publish once the unit reports a heading
   (`ros2 topic hz /mtrtk/imu` shows the epoch rate).

**SBG Ellipse-D**

7. NTRIP to a base: GPS1_POS type reaches 7 (RTK fixed), and the RTCM_RAW echo count
   (`rtk.rtcm_rx_total`) rises. Record it with and without `INS_RTCM_PORT`
   (`sbg-rtcm-port-a`, `sbg-rtk-pos-type`).
8. GPS1_RAW detected as UBX (`raw_gnss_format: ubx` on `/api/receiver`): an hourly `.ubx` file
   appears and `convbin` turns it into RINEX (re-check of `sbg-gps1-raw-ubx` on that unit).
9. Camera pulse on Sync In A: a time mark appears in the UI and on `/api/state`. Record the
   spacing (`sbg-event-offsets`, `sbg-sync-in`). `events.csv` from `mtrtk ppk` is expected
   empty (see [Raw GNSS and PPK](#raw-gnss-and-ppk)).
10. Cover the antennas for a minute: the UTC goes not valid and marks are held (`sbg-clock-outage`).

**VectorNav VN-200**

11. RawMeas probe: record `raw_meas` / `sat_info` in the configuration report and whether
    binary output 1 kept them (`vn-reg75-ext`, `vn-rawmeas-layout`).
12. With `INS_VN_RTCM=1` and NTRIP: record the GPS Fix values seen (`vn-rtcm`, `vn-fix-rtk`).
13. SyncIn pulse: a time mark appears, dated against the pulse source.
14. Satellites page against VectorNav Control Center (`vn-satinfo`), UTC date on the API
    (`vn-utc-year`).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Connected but no frames (`ins.stats.bytes_in` rises, `frames` flat on `/api/receiver`) | wrong baud, wrong port, or the port in another protocol mode | match `INS_BAUD` to the unit; Ellipse Port A must be in sbgECom mode; VN in binary output mode on serial port 1 |
| No bytes at all | wrong device, cable, or another process holds the port | `mtrtk doctor`, `fuser <port>`, check TX/RX and the voltage level |
| Frames, but with CRC failures and resyncs | a lossy cable or link (an unread tunnel overflowing), or a baud close but wrong | shorter cable, read the link continuously, re-check the baud |
| VN-200: frames arrive but the INS panels stay empty | binary output 1 is not set, or goes to the other serial port; only ASCII async output arrives | apply the profile (it sets binary output 1, then register 6 to 0 once binary streams), or set both in VectorNav Control Center |
| Configuration `mismatched` | the setting is locked, or the firmware clamps the value | read the current value in the report; set it in the vendor tool; nothing is saved while a mismatch stands |
| Items `pending` that never apply | `INS_APPLY_CONFIG=0` (read-only) | `mtrtk ins config --apply`, or set `INS_APPLY_CONFIG=1` |
| "outputs held" in the report | `INS_OUTPUT_HZ > 50` on a link below 460800 baud | raise the unit's baud in sbgCenter, then `INS_BAUD` |
| SBG reboots repeatedly after a save | the supply sags at the reboot | a stronger supply; settings are saved once per process, so a loop is power, not mtrtk |
| `GPS1_RAW does not look like UBX` | the internal receiver's stream is not UBX on this unit | the bytes go to the opaque `.sbgraw` capture; report the unit model and firmware |
| `ins_not_aligned` warning | the filter has not aligned within 60 s | move the vehicle; check lever arms and the antenna sky view |
| "RTCM path unverified on this unit" | corrections go to Ellipse Port A without an echo or RTK fix yet | wait for RTK, or wire Port B and set `INS_RTCM_PORT` |
| `ins_port` "no permission" in `mtrtk doctor` | the user is not in `dialout` | `sudo usermod -aG dialout $USER`, then log in again |

See also [`rover.md`](rover.md) for the rest of the rover role, [`ppk.md`](ppk.md) for
post-processing and [`ros2.md`](ros2.md) for the bridge.
