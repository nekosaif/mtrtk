# Rover

`ROLE=rover` turns a ZED-F9P on a Pi, laptop or Jetson into an RTK rover fed by your base over
Tailscale (or a public path: [exposure.md](exposure.md)). The same process and the same web UI as
the base: the receiver gets the rover profile
(`ROVER_NAV_HZ` navigation, `ROVER_DYNMODEL`, RXM-RTCM, NAV-RELPOSNED, TIM-TM2), the NTRIP client
pulls RTCM from the caster and writes it into the receiver's USB port, and every navigation epoch
goes out as NMEA and JSON to whatever consumes it. Raw UBX is logged hourly for PPK, exactly as on
the base.

## Minimal `.env`

```
ROLE=rover
NTRIP_URL=ntrip://rover:<password>@<base-tailscale-ip>:2101/MTRK
ROVER_NAV_HZ=5
ROVER_DYNMODEL=automotive        # portable | stationary | pedestrian | automotive | airborne1g | airborne2g | airborne4g
NMEA_TCP_PORT=10110
```

Start it with `uv run mtrtk rover` (or `ROLE=rover` and `docker compose up -d`). `NTRIP_URL` also
accepts `http://`, and with no scheme it is read as `ntrip://`; the user and the port are
optional (the port defaults to 2101). The client asks as NTRIP v2 and falls
back to v1, sends the rover's GGA every `NTRIP_GGA_INTERVAL_S` (10 s, at most 3600; 0 sends no GGA, for a
caster that does not want it) for VRS casters, and
reconnects with backoff when the stream goes quiet. A URL it cannot parse is logged (without the
password) and the rover runs on without corrections until a working one is set: the RTK page has
a field for it, which writes `NTRIP_URL` to `.env` and restarts the client in place
(`PUT /api/rover/ntrip`).

The status line gains the RTK part once corrections flow:

```
21:54:58 3D        None      sats 25/49 lat 23.8373031 lon 90.2625793 h -43.30 hAcc 1.02 rtcm 491 B/s rtk None age 0.0s base -
```

## Outputs

- **NMEA over TCP** (`NMEA_TCP_PORT`, default 10110; `-1` turns it off). Any NMEA TCP client
  connects to `<rover-ip>:10110`: SW Maps, OpenCPN, a tablet app. gpsd reads it with
  `gpsd -N tcp://<rover-ip>:10110`, and QGIS then takes it from gpsd (GPS Information panel →
  gpsd). NMEA clients cannot log in, so the stream has no password: anyone who can reach the
  port gets the rover's live position. `NMEA_TCP_BIND` decides who that is: `lan` (the default)
  or `all` listen on every interface, `tailscale` only on the tailnet (it waits for tailscale0 and
  never falls back; unlike the base's web UI and caster it keeps the address it started on, so
  restart the rover after its tailnet IP changes), or give an IP address. At most `NMEA_TCP_MAX_CLIENTS` (16) clients are
  served at once; a further connection is closed as it arrives.
- **NMEA over UDP**: `NMEA_UDP_TARGETS=host:port,host:port`. An entry that is not `host:port` is
  skipped with a warning.
- **NMEA on a serial device** (`NMEA_SERIAL=/dev/ttyUSB1`, line speed `NMEA_SERIAL_BAUD`) or on a
  **pseudo-terminal** (`NMEA_SERIAL=pty`; the slave is linked at `DATA_DIR/ttyMTRTK`) for software
  that insists on a serial port.
- **JSON over UDP** (`JSON_UDP_PORT=5555`, sent to `127.0.0.1`): one JSON object per epoch with
  time, position, accuracy, velocity, fix and carrier solution, RTK baseline, correction age and
  attitude when there is one. The ROS 2 bridge does not use it: it reads the WebSocket (see
  [ros2.md](ros2.md)), so the WebSocket must be reachable from it (`WEB_BIND`).
- Sentence set: `NMEA_SENTENCES=GGA,RMC,GST,GSA,GSV,VTG,ZDA` (the default). GSA, GSV and ZDA go
  out at most every `NMEA_SLOW_INTERVAL_S` (1 s); the others every epoch. `HDT` and `PASHR` can be
  added to the list. They are written on every epoch whose attitude has a heading, with or
  without a position yet (the others wait for a position). That means an INS driver
  (`ROVER_DRIVER=sbg_ellipse|vectornav`); an F9P alone never produces them. An unknown name is a
  startup error.

An INS unit (SBG Ellipse-D, VectorNav VN-200) can replace the F9P as the rover receiver: wiring,
`INS_*` configuration and what is verified are in [`ins-drivers.md`](ins-drivers.md).

`GET /api/rover` shows what is running: the NTRIP client's state, the RTK status, the outputs
(the NMEA TCP port and its client count, UDP targets, serial, JSON port, sentences), the open
session and the point being collected.

## Reading the RTK page

The goal is a correction age under 5 s and "RTK fixed". Float for minutes means weak signals or a
long baseline. "No corrections" means checking `NTRIP_URL`, the base and Tailscale, in that order
([troubleshooting.md](troubleshooting.md));
the NTRIP panel shows the client's last error. The *Corrections received* table is what the
receiver itself reports (UBX-RXM-RTCM) per message type: **Count** says the corrections arrive,
**Used** says the receiver accepts them. A stale or distant base gives counts with nothing used.
Under the age gauge, *Receiver reports corrections ≤ N s old* is the receiver's own coarse
NAV-PVT age bucket, next to mtrtk's age since the last injected frame.

### Rover alerts

As on the base (`docs/base.md`, *Alerts*), each is one event when it starts and one
`<kind>_cleared` when it ends, sent to `ALERT_WEBHOOK_URL` too.

| kind | raised | cleared |
| --- | --- | --- |
| `ntrip_disconnected` | the NTRIP client loses the caster (or cannot reach it) with an error | it connects again |
| `corrections_stale` | the receiver's correction age passes 10 s | the age is under 5 s again |
| `rtk_lost` | an RTK fixed solution that existed stays below fixed for 10 s | RTK fixed again |

Two one-off events, not conditions: `ntrip_unsupported` (info) when `NTRIP_URL` is set but the
driver takes no RTCM (a VectorNav without `INS_VN_RTCM=1`), and `ntrip_url_invalid` (warning)
when `NTRIP_URL` does not parse at startup. INS rovers add `ins_not_aligned`, `ins_gnss_lost`,
`ins_config_mismatch` and `imu_error` (`docs/ins-drivers.md`).

## Survey points

Start a session, name a point, collect N epochs (default `POINT_EPOCHS=30`, RTK fixed only while
`POINT_FIXED_ONLY=1`; both can be overridden per point, and the Survey page's form starts from
them). Epochs that do not qualify are counted as
skipped, not averaged. Points are averaged in ENU with per-axis standard deviations and exported
as CSV, GeoJSON, KML or GPX from the Survey page or from
`GET /api/rover/points/export?fmt=csv` (`&session_id=` for one session).

A session's start and end are stamped on the receiver's UTC, the clock the raw-log hours and the
points use, so its window selects the right raw logs for PPK even on a Pi whose own clock is
wrong (no RTC, no network). Only while the receiver has no valid time does the host clock stand
in; the daemon logs a warning when the two disagree by more than 5 s.

| Route | What it does |
|---|---|
| `GET /api/rover` | Overview: driver, NTRIP client, RTK, outputs, session, collection, and `collect_defaults` (`{"epochs", "fixed_only"}` from `POINT_EPOCHS` / `POINT_FIXED_ONLY`) |
| `PUT /api/rover/ntrip` | `{"url"}`: save `NTRIP_URL` and restart the client on it |
| `GET` / `POST /api/rover/sessions`, `POST /api/rover/sessions/stop` | List, open (closing the open one), close |
| `GET` / `POST` / `DELETE /api/rover/collect` | Collection status, start `{"name", "code", "note", "epochs", "fixed_only"}`, cancel |
| `GET /api/rover/points`, `DELETE /api/rover/points/{id}` | Stored points, newest first (`?session_id=` for one session); delete one |
| `PATCH /api/rover/points/{id}` | `{"name", "code", "note"}`: rename, recode, renote. A key left out or `null` keeps that field; `""` empties `code` or `note` |
| `GET /api/rover/points/export?fmt=csv\|geojson\|kml\|gpx` | Download |

## PPK

Raw UBX (RXM-RAWX/SFRBX, plus TIM-TM2 camera pulses on EXTINT) is logged hourly under
`DATA_DIR/ubx/`, exactly like the base. `mtrtk ppk` (Phase 8) post-processes a session or a time
window against a base's logs with RTKLIB.

## Testing without a second receiver

`tests/hardware/test_live_rover.py`
(`MTRTK_TEST_PORT=/dev/ttyACM0 uv run pytest -m hardware tests/hardware/test_live_rover.py -s`)
writes the rover profile to the receiver's RAM, BBR and flash and does not put the old one back,
so it runs only on the port `MTRTK_TEST_PORT` names (it skips without it). On a base receiver,
restart the base daemon afterwards so it re-applies the base profile. It proves the plumbing on a real F9P with recorded base corrections from an in-process caster: the
profile applies at 5 Hz, the receiver reports the injected RTCM in RXM-RTCM, the correction age
stays under 5 s and NMEA is served over TCP. The receiver counts the corrections but cannot use
them (stale), so the printed table shows `used == 0`. That is the expected result. A real fixed
solution needs a live base within about 20 km.

The whole rover role also runs on a replay, with no receiver at all. Replay the base fixture as a
base (its caster serves the recorded RTCM) and again as a rover pointed at it:

```bash
export WEB_BIND=127.0.0.1 WEB_ALLOW_INSECURE=1
DATA_DIR=/tmp/mtrtk-base NTRIP_BIND=127.0.0.1 NTRIP_PORT=2102 WEB_PORT=8081 \
  uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --loop &
DATA_DIR=/tmp/mtrtk-rover ROLE=rover MTRTK_SOURCE=file:tests/fixtures/f9p_hpg113_base_30s.ubx \
  REPLAY_LOOP=1 NTRIP_URL=ntrip://127.0.0.1:2102/MTRK WEB_PORT=8082 NMEA_TCP_PORT=10111 \
  uv run mtrtk rover &
nc 127.0.0.1 10111              # $GNGGA, $GNRMC, ... once per epoch; the UI is on :8082
```

A file source discards what is written to it, so the corrections never reach a receiver. The NTRIP
client, the correction age, the NMEA/JSON outputs, sessions and points all run for real. The base
fixture carries NAV-EOE, which closes each epoch; the `raw_*` fixtures lack it, and their epoch
ends are inferred from the NAV-* iTOW instead, so they drive the NMEA outputs too.

**Status: verified on replay; live check pending a second receiver.** On 2026-10-02 the replay
setup above (ephemeral ports, temporary `DATA_DIR`) showed the NTRIP client connected to the
replayed base's caster, the base listing the rover as a client with its GGA position, the
correction age at 0.06-1.0 s, `$GNGGA`/`$GNRMC`/`$GNGST`/`$GNVTG`/GSA/GSV on the NMEA TCP port,
one JSON object per epoch on UDP, and a 10-epoch point in a session exported as CSV and GeoJSON.
`test_live_rover.py` has not yet run against hardware: the only F9P was busy as the base, and RTK
fixed needs a second receiver or a live caster.
