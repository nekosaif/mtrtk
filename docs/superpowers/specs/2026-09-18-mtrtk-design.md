# mtrtk — Design Spec (approved 2026-09-18)

> Multi-role GNSS RTK/PPK/PPP toolkit: ZED-F9P base station + multi-receiver rovers. This is the design approved in the planning session; the implementation plan lives in `docs/superpowers/plans/`.

## Context

Empty repo at `/home/nekosaif/github/mtrtk` (not yet `git init`). User owns a SparkFun GPS-RTK-SMA (u-blox ZED-F9P) + SparkFun multi-band magnetic antenna and wants one repo covering:

1. **Base station**: logs raw UBX for PPK/PPP, serves RTCM3 corrections over an NTRIP caster (Tailscale by default, public IP or Cloudflare Tunnel optional), exports RINEX for CSRS-PPP / AUSPOS / OPUS, applies PPP results as a fixed position, and shows *everything* about the receiver in a polished web UI.
2. **Rover**: NTRIP client → RTK on the receiver, normalized outputs (NMEA, JSON/WS, ROS2), always-on raw logging for PPK, survey point collection, same web UI. Receivers: ZED-F9P first; SBG Ellipse-D and VectorNav VN-200 INS units last (spec-based, no hardware yet).
3. **PPK**: post-process rover + base logs with RTKLIB (`convbin` + `rnx2rtkp`), including camera events (TIM-TM2) for geotagging.

Use cases: UAV, ground rover, autonomous car, geo-surveying. Hosts: any Linux (Raspberry Pi ARM64, x86 PC, Jetson) via `git pull` + `.env` + Docker Compose.

### Verified environment (dev box, 2026-09-18)
- F9P on `/dev/ttyACM0` (USB 1546:01a9), user in `dialout`. **Firmware HPG 1.13, PROTVER 27.12** (old; user will upgrade to HPG 1.51 via u-center on Windows; code must run on both). Currently streaming NAV-PVT, NAV-SAT, NAV-SIG, RXM-RAWX, RXM-SFRBX.
- Python 3.12.3, `uv`, `pyubx2 1.3.6` (system), Node 24, `pnpm`, Docker 29, Tailscale 1.102 (this host `nekovivobook` 100.100.50.10), RTKLIB `convbin`/`rnx2rtkp`/`str2str` in `/usr/bin`, Ubuntu 24.04 x86_64.

## Confirmed decisions

| Area | Decision |
|---|---|
| Hosts | Any Linux: Pi (arm64), x86_64, Jetson. Multi-arch Docker (amd64+arm64, armv7 best effort) + native `install.sh` (uv + systemd) fallback |
| Receiver link | USB-C CDC (`/dev/serial/by-id/…u-blox…`, VID 1546 auto-detect) |
| Base site modes | Both: survey-in (portable) and FIXED from saved named sites (PPP-derived) |
| Deploy | Docker Compose, `network_mode: host`, `/dev` bind + `device_cgroup_rules` (hotplug-safe, non-privileged) |
| Raw logs | Hourly UTC-aligned UBX files, JSON sidecar, prune oldest when free disk < `MIN_FREE_GB`, `keep` flag exempt |
| RINEX presets | CSRS-PPP, AUSPOS, OPUS (GPS-only, RINEX 2.11), Generic RINEX 3.04 + Hatanaka + gzip |
| PPP apply | Web form + result-file upload (CSRS `.sum`, AUSPOS SINEX, OPUS text) → site → TMODE3 fixed |
| RTCM set | Default MSM7 1077/1087/1097/1127 + 1005 (1 Hz) + 1230 (5 s); `RTCM_MSM=4` switches to MSM4 |
| Caster | In-process asyncio NTRIP v1+v2, Basic auth configurable (default required, empty = anonymous), sourcetable, client stats, rover GGA → shown on base map |
| Web auth | Default bind Tailscale IP, no login. `WEB_PASSWORD` optional; mandatory when bound publicly |
| Exposure | Tailscale (default) / public IP (+ Caddy TLS sidecar) / Cloudflare Tunnel (`cloudflared` sidecar; NTRIP v2-over-HTTPS clients only) |
| Backend | Python 3.12, FastAPI, asyncio, pyserial-asyncio, pyubx2, pyrtcm, pynmeagps, aiosqlite, pydantic-settings, uv |
| Frontend | React + Vite + TS + Tailwind + shadcn/ui, MapLibre GL (OSM + Esri imagery, graceful offline), Recharts, custom SVG sky plot / canvas spectrum |
| History | SQLite: 1 s samples kept 24 h, 1 min aggregates kept 90 d |
| Alerts | In-UI event log + banners + optional `ALERT_WEBHOOK_URL` JSON POST (ntfy/Discord/Telegram-compatible) |
| Rover rate | 5 Hz nav + RAWX default (`ROVER_NAV_HZ`), base 1 Hz |
| Rover NMEA | Synthesized from UBX/normalized state (receiver outputs UBX only) |
| Rover logging | Always on, hourly, + session start/stop markers |
| Survey | Full point collection (epoch averaging, fixed-only filter, codes/notes, CSV/GeoJSON/KML/GPX) |
| Coordinates | WGS84 DD + DMS, ECEF, UTM auto-zone; ellipsoidal + MSL |
| ROS2 | Separate rclpy bridge node, Humble + Jazzy Dockerfiles, custom `mtrtk_msgs` |
| INS drivers | SBG Ellipse-D (sbgECom over serial) and VectorNav VN-200: RTCM inject (if unit supports), pos/attitude/IMU out, raw GNSS capture, vendor config where protocol allows. **Last phase, spec-based** |
| PPK events | TIM-TM2 camera events → interpolated positions CSV |
| Replay mode | `MTRTK_SOURCE=file:…ubx` real-time paced fake receiver for dev/CI/demo |
| Receiver config | Applied every start via CFG-VALSET to RAM+BBR+Flash, verified via CFG-VALGET; PROTVER-gated |
| Phasing | Base → web UI → RINEX/PPP → F9P rover → ROS2 → PPK → INS drivers |
| License | MIT |

## Architecture

One asyncio daemon per host, one container, `ROLE=base|rover`.

```
Receiver (USB CDC) ─bytes─▶ Source ─▶ Demux ─▶ Bus (pub/sub, bounded queue per subscriber)
   ▲                       (serial |   (UBX /     ├─▶ RawLogger     hourly .ubx + .json → DATA_DIR/ubx/YYYY/DDD/
   │ RTCM in (rover)        replay)   RTCM3 /    ├─▶ NtripCaster   raw RTCM → clients (base)
   │ CFG-VALSET                       NMEA)      ├─▶ StateStore    parsed → ReceiverState/RoverState → diffs
   │                                             ├─▶ Sampler       1 s rows → SQLite, 1 min rollups
   │                                             ├─▶ WsHub         topics → browsers (throttled)
   │                                             └─▶ Alerts        rules → events + webhook
   └─ ReceiverController: open/reconnect/watchdog, apply+verify profile, TMODE3 ops, polls
NtripClient (rover) ─RTCM─▶ driver.inject_rtcm()
FastAPI REST + /ws + built SPA (same process). RTKLIB convbin/rnx2rtkp/rnx2crx via subprocess (jobs).
```

Key properties: live consumers drop-oldest on overflow; logger never drops (large queue, alert on sustained pressure); serial reconnect with backoff re-applies and re-verifies profile; hour rotation keyed on receiver UTC (NAV-PVT) not host clock; SQLite WAL; graceful shutdown finalizes sidecars; unfinished files recovered at startup.

Design points confirmed by independent Plan-agent review:
- **Own incremental framer + lazy parse** (not `UBXReader` on the live stream): RXM-RAWX / RXM-SFRBX frames go to the logger as raw bytes without full parsing (they dominate byte volume); only state-relevant classes are parsed. Caster receives raw RTCM frame slices, zero extra copies.
- **Core vs optional VALSET split + capability probe**: core keys must ACK or startup fails loudly; optional keys (MON-SPAN, MON-COMMS, NAV-TIMELS, etc.) are probed per firmware and skipped with an info event when NAK'd. This is the main 1.13 ↔ 1.51 coexistence mechanism.
- **ECEF is the canonical site datum**; frame + epoch stored as metadata; no datum transforms in v0.x.
- **1005-vs-site check** (decoded RTCM 1005 ECEF must equal active site within 0.1 mm) is the visible "base correctly configured" indicator in the UI.
- **Tailscale bind: retry, never fall back** to `0.0.0.0` if the Tailscale IP is not yet available (Tailscale may start after Docker).
- `daemon.py` is an explicit supervisor: wires bus, source, consumers per role, restarts failed consumer tasks, owns shutdown order (stop source → drain logger → close DB).

## Repo layout

```
mtrtk/
  README.md  LICENSE(MIT)  .env.example  docker-compose.yml  install.sh
  pyproject.toml  uv.lock
  src/mtrtk/
    cli.py                  # mtrtk base|rover|ppk|export|replay|record|config|doctor
    config.py               # pydantic-settings Settings ← .env / env
    core/   source.py demux.py bus.py state.py ubx_config.py receiver.py geo.py timeutil.py
    base/   ntrip_caster.py survey.py sites.py
    rover/  pipeline.py ntrip_client.py nmea_out.py points.py sessions.py
            drivers/ base.py ublox.py sbg_ellipse.py vectornav.py
    rawlog/ writer.py retention.py index.py
    rinex/  convbin.py splice.py presets.py export.py ppp_result.py
    ppk/    pipeline.py rtkconf.py pos.py events.py
    store/  db.py schema.sql sampler.py repo.py
    alerts.py  system.py  jobs.py  exposure.py
    web/    app.py ws.py deps.py api/{status,config,receiver,base,ntrip,logs,export,history,events,system,rover,points,ppk}.py
  web/                      # React+Vite+TS+Tailwind+shadcn; built → src/mtrtk/web/static via Docker stage
  ros2/ mtrtk_msgs/ mtrtk_bridge/ Dockerfile.humble Dockerfile.jazzy
  docker/ Dockerfile  Caddyfile  compose.cloudflare.yml  compose.public.yml  compose.ros2.yml
  docs/   setup.md hardware.md base.md ppp-workflow.md rover.md ppk.md ros2.md exposure.md ins-drivers.md firmware.md
  docs/superpowers/specs/2026-09-18-mtrtk-design.md   # spec committed in Phase 0 (from this plan)
  tests/  unit/ integration/ fixtures/*.ubx  (pytest; `-m hardware` for live F9P)
```

## Configuration (`.env.example`, all via pydantic-settings)

`ROLE`, `MTRTK_SOURCE` (`auto` | `/dev/serial/by-id/...` | `file:path`), `BAUD`, `DATA_DIR=/data`, `STATION_ID=MTRK`, `COUNTRY=BGD`, `MARKER_NAME`, `ANTENNA_TYPE=NONE`, `ANTENNA_HEIGHT_M`, `OBSERVER`, `AGENCY`;
`BASE_MODE=survey-in|fixed|off`, `SVIN_MIN_DURATION_S=300`, `SVIN_ACC_LIMIT_M=2.0`, `ACTIVE_SITE`, `RTCM_MSM=7`, `RTCM_1230_RATE=5`;
`NTRIP_BIND=tailscale|lan|all|<ip>`, `NTRIP_PORT=2101`, `MOUNTPOINT=MTRK`, `NTRIP_USER`, `NTRIP_PASSWORD`;
`WEB_BIND=tailscale|lan|all|<ip>`, `WEB_PORT=8080`, `WEB_PASSWORD`, `WEB_ALLOW_INSECURE=0`;
`LOG_MESSAGES` (default list), `MIN_FREE_GB=5`, `FSYNC_INTERVAL_S=10`;
`ROVER_DRIVER=ublox|sbg_ellipse|vectornav`, `ROVER_NAV_HZ=5`, `ROVER_DYNMODEL=portable|automotive|airborne1g|airborne2g|stationary`, `NTRIP_URL=ntrip://user:pass@host:2101/MTRK`, `NTRIP_GGA_INTERVAL_S=10`, `NMEA_TCP_PORT=10110`, `NMEA_UDP_TARGETS`, `NMEA_SERIAL`, `JSON_UDP_PORT`;
`ALERT_WEBHOOK_URL`, `ALERT_RULES` overrides; `TUNNEL_TOKEN` (cloudflare profile), `PUBLIC_DOMAIN` (caddy profile).

## Component design

### core
- `source.py`: `SerialSource` (pyserial-asyncio; auto-detect u-blox by-id; watchdog `no bytes > 5 s` → reopen; exponential backoff) and `FileReplaySource` (paces on UBX iTOW/NAV-PVT time, loops optional).
- `demux.py`: byte framer for UBX (`B5 62`, len, CK), RTCM3 (`D3`, 10-bit len, CRC24Q), NMEA (`$…\r\n`). Emits `Frame(proto, raw, ts_host, parsed?)`. UBX parsed via `pyubx2.UBXReader.parse`; RTCM header decoded via `pyrtcm` for type only (cheap).
- `bus.py`: topics + `subscribe(topics, maxsize, policy=drop_oldest|block)`.
- `state.py`: pydantic models `Position`, `Accuracy`, `Dops`, `TimeInfo`, `Satellite` (merged NAV-SAT + NAV-SIG signals), `RfBlock`, `Spectrum`, `SurveyIn`, `RtcmStats`, `NtripStats`, `RtkStatus`, `Attitude`, `SystemStats`, `ReceiverState`. Publishes `state.<section>` diffs. `state.epoch` carries a deep copy of `ReceiverState` taken at NAV-EOE; per-section `state.<section>` topics carry the live section objects and consumers must read them immediately.
- `ubx_config.py`: profiles as lists of `(key, value)` for `CFG-VALSET` with layers RAM|BBR|FLASH, chunked ≤64 keys, ACK checked, then `CFG-VALGET` verify. PROTVER gating table (e.g. MON-SPAN, NAV-TIMELS, MON-COMMS presence on 1.13 vs 1.32/1.51 — verified at runtime by polling; unsupported → skipped + info event).
  - Base: `CFG-RATE-MEAS=1000/NAV=1`, `CFG-NAVSPG-DYNMODEL=2`, USB out UBX+RTCM3X, NMEA off; `CFG-MSGOUT-UBX_{NAV_PVT,NAV_HPPOSLLH,NAV_HPPOSECEF,NAV_SAT,NAV_SIG,NAV_DOP,NAV_STATUS,NAV_CLOCK,NAV_TIMEUTC,NAV_TIMELS,NAV_SVIN,RXM_RAWX,RXM_SFRBX,MON_HW,MON_RF,MON_COMMS,MON_SPAN}_USB`; `CFG-MSGOUT-RTCM_3X_TYPE{1005,1077,1087,1097,1127}_USB=1`, `TYPE1230_USB=5`; `CFG-SIGNAL-*` GPS L1CA+L2C, GLO L1+L2, GAL E1+E5B, BDS B1+B2, QZSS L1CA+L2C, SBAS off; `CFG-TMODE-*` per mode.
  - Rover (ublox): TMODE off, `RATE-MEAS=200` (5 Hz), dynmodel per env, USB in RTCM3X on, + `NAV_RELPOSNED`, `RXM_RTCM`, `TIM_TM2`, no SVIN.
- `receiver.py`: lifecycle, `apply_profile()`, `set_tmode_survey_in()`, `set_tmode_fixed(site)` (ECEF cm + HP 0.1 mm parts, `FIXED_POS_ACC`), `poll(MON-VER)`, `reset(hot|warm|cold|factory)`.
- `geo.py`: LLH↔ECEF (WGS84), UTM auto-zone (pure-python formulas, no pyproj), DMS format, ENU deltas.

### base
- `ntrip_caster.py`: asyncio TCP server. Request parse: `GET /<mount>` (+ `Ntrip-Version: Ntrip/2.0` → HTTP/1.1 chunked `Content-Type: gnss/data`; else v1 `ICY 200 OK` raw), `GET /` → sourcetable (`STR;…` + `CAS`/`NET` lines, `ENDSOURCETABLE`), Basic auth → `401` + `WWW-Authenticate`, unknown mount → sourcetable. Per-client send queue (drop client if backlog > 256 KB). Read side parses `$GPGGA`/`$GNGGA` from clients → `NtripClient.position`. Stats published to bus; connect/disconnect rows in `ntrip_clients_log`.
- `survey.py`: tracks NAV-SVIN, exposes progress, "freeze as site".
- `sites.py`: CRUD, activate (writes TMODE3 fixed, then verifies). A broadcast RTCM 1005 matching the site within 0.0005 m per axis is what marks it verified, on its own; `fixType == 5` is the 30 s deadline that raises a mismatch when no 1005 has confirmed it by then.
- `exposure.py`: resolves `tailscale|lan|all` → IPs (tailscale via `tailscale0` interface or `tailscale ip` if available; falls back with warning). Refuses public web bind without password.

### rawlog
- `writer.py`: subscribes `raw.ubx` filtered by `LOG_MESSAGES`; file `DATA_DIR/ubx/YYYY/DDD/{STATION_ID}_{YYYYMMDD}_{HH}.ubx`; rotation on receiver-UTC hour; buffered writes, flush 1 s, fsync `FSYNC_INTERVAL_S`; sidecar JSON (start/end, counts by msg, size, sha256 on close, role, site, firmware, keep). Startup scan finalizes orphaned sidecars.
- `retention.py`: hourly; prune oldest non-keep when free < `MIN_FREE_GB`; also removes derived RINEX for pruned hours. `index.py`: availability per hour for UI timeline.

### rinex / PPP
- `splice.py`: select hourly files overlapping `[t0,t1)`, concatenate to temp, `convbin -ts/-te` to clip.
- `convbin.py`: wrapper `-r ubx -v <2.11|3.04> -od -os -oi -ot -ol -scan -hm/-hn/-ht/-ho/-hr/-ha/-hd -ti <interval> -y <exclude systems>`; produces `.obs/.nav(+.gnav etc.)`.
- `presets.py`: CSRS-PPP (3.04, 30 s, GPS+GLO+GAL), AUSPOS (3.04, 30 s, GPS+GLO), OPUS (2.11, GPS only, 30 s, antenna `NONE`), Generic (3.04, native, optional Hatanaka `rnx2crx` + gzip). RINEX 3 long names `XXXXMRCCC_R_YYYYDDDHHMM_01D_30S_MO.rnx`.
- `ppp_result.py`: parsers for CSRS-PPP `.sum` (estimated coordinates table: lat/lon/h + ECEF + sigmas, frame + epoch), AUSPOS SINEX (`SOLUTION/ESTIMATE` STAX/STAY/STAZ), OPUS text (`X:`/`Y:`/`Z:` lines). Robust regex + manual fallback. Stored with `frame`, `epoch`, `source`.
- `export.py` + `jobs.py`: background job runner with progress, results under `DATA_DIR/exports/<job>/`, downloadable.

### rover
- `drivers/base.py`: `RoverDriver` protocol (capabilities, `frames()`, `inject_rtcm()`, `configure()`, `raw_log_filter`).
- `drivers/ublox.py`: wraps core demux/profile; maps UBX → `RoverState` (NAV-PVT, HPPOSLLH, RELPOSNED, RXM-RTCM, SAT/SIG, TIM-TM2).
- `drivers/sbg_ellipse.py` (last phase): sbgECom framer (`FF 5A`, msg id/class, len, payload, CRC16, `33`), logs EKF_NAV/EKF_EULER/EKF_QUAT/GPS1_POS/GPS1_VEL/GPS1_HDT/UTC_TIME/IMU_DATA/STATUS/GPS1_RAW/GPS1_SAT; RTCM inject on same port; settings subset via sbgECom commands (lever arms, alignment, output config, GNSS/RTCM port). Raw GPS1_RAW payload captured to hourly file; if UBX, RINEX-convertible.
- `drivers/vectornav.py` (last phase): VN binary (`FA` sync, groups) + ASCII (`$VN…`), registers via `$VNRRG/$VNWRG` for INS/GNSS/IMU config, raw GNSS output group if firmware supports, RTCM inject only if unit supports (verify against VN-200 docs; expected: not supported → capability off).
- `ntrip_client.py`: v2 (`Ntrip-Version: Ntrip/2.0`, chunked) with v1 fallback, Basic auth, GGA upload, backoff reconnect, correction age from last RTCM byte + `RXM-RTCM` stats.
- `nmea_out.py`: GGA (high-precision digits), RMC, GST, GSA (per system), GSV, VTG, ZDA, HDT + PASHR when attitude. Sinks: TCP server, UDP unicast/broadcast, serial or pty (`/dev/ttyMTRTK` symlink inside container).
- `points.py`: averaging job (N epochs, fixed-only filter, live σ), CRUD, exports. `sessions.py`: start/stop markers (DB + sidecar tags).

### ppk
- `pipeline.py`: inputs (rover files/session/window/upload; base local logs / remote base URL `GET /api/export/rinex?…` over Tailscale / uploaded RINEX or UBX; base coords from site / RINEX header / manual) → `convbin` both → `rnx2rtkp -k f9p_ppk.conf -o out.pos rover.obs base.obs rover.nav base.nav` → `.pos`.
- `rtkconf.py`: template: `pos1-posmode=kinematic`, `pos1-frequency=l1+l2`, `pos1-soltype=combined`, `pos1-elmask=15`, `pos1-dynamics=on`, `pos1-ionoopt=brdc`, `pos1-tropopt=saas`, `pos1-navsys=45` (+16 QZSS opt), `pos2-armode=fix-and-hold`, `pos2-gloarmode=on`, `pos2-bdsarmode=on`, `pos2-arthres=3`, `pos2-arminfix=10`, `pos2-maxage=30`, `ant2-postype=xyz` + `ant2-pos1/2/3`, `out-solformat=llh`, `out-timesys=utc`, `out-height=ellipsoidal`. Tunables exposed.
- `pos.py`: parse `.pos` → records (time, lat, lon, h, Q, ns, sd*, age, ratio) → CSV/GeoJSON/KML; summary (fix %, float %, gaps).
- `events.py`: TIM-TM2 rising edges → GPS time → interpolate position (linear; cubic option) → `events.csv` (n, time, lat, lon, h, Q, σ).

### store
`schema.sql`: `samples_1s`, `samples_1m` (avg/min/max), `sites`, `sessions`, `points`, `events`, `log_files`, `ntrip_clients_log`, `jobs` (export/ppk, params+result JSON). `sampler.py`: 1 s insert, per-minute rollup, prune (`samples_1s` 24 h, `samples_1m` 90 d, `events` 365 d, `ntrip_clients_log` 90 d — the last two are append-only and unbounded without a horizon). aiosqlite, WAL, migrations via numbered SQL files.

### alerts
Rules: receiver disconnected, fix lost / carrier solution dropped, survey-in stalled, no NTRIP clients for N min (base, optional), correction age > X (rover), jamming indicator high / antenna open-short, disk low, logger backpressure, host temp high. Event rows + WS `events` topic + webhook POST `{level, kind, message, ts, host, role}` (ntfy compatible; Discord/Telegram via their webhook JSON adapters).

### web API
REST (`/api`): `status`, `state` (snapshot), `config` GET/PUT (+ validation, restart required flags), `receiver/{profile,reset,poll}`, `base/mode`, `base/survey`, `base/sites` CRUD + `activate`, `base/ppp/import` (multipart), `ntrip/clients`, `logs` list + `logs/{name}` download + PATCH keep + DELETE, `logs/availability`, `export` POST → job, `jobs/{id}` + download, `history?metric&from&to&res`, `events` (+ack), `system`, `rover/ntrip`, `rover/sessions`, `rover/points` CRUD + `export?fmt=`, `ppk/jobs`, `healthz`.
WebSocket `/ws?topics=…`: `pvt`, `sats`, `rf`, `span`, `time`, `svin`, `rtcm`, `ntrip`, `rtk`, `attitude`, `events`, `system`, `jobs`. Snapshot on connect, then diffs; per-topic throttle.

### frontend (`web/`)
Pages: Dashboard, Satellites (sky plot SVG, CNO bars, per-signal, table), Receiver (HW/RF, jamming, AGC, spectrum canvas, firmware, comms, time), Corrections (base), Site & Position + PPP wizard (base), Logs & Export (availability timeline, export wizard, jobs), History (range picker charts), RTK (rover), Survey (rover), PPK, Events, Settings. Shared: `useLiveState()` WS hook with reconnect, coordinate formatter, role-adaptive nav, dark/light, responsive to tablet width. Map: MapLibre with OSM raster + Esri World Imagery toggle; offline → grid background, markers still drawn.

**Design system (decided and built in Phase 4; plan `docs/superpowers/plans/2026-09-19-phase4-frontend.md`, guide `docs/ui.md`).** A geodetic instrument, not a SaaS dashboard: the numbers are the design, one memorable element per page, structure encodes information, motion only where data moves.

- *Tokens and themes.* Every colour, font and radius is declared once in `web/src/index.css`: `:root` for dark (the default and what the design was drawn for) and `:root[data-theme="light"]` for light — a selected theme, not an automatic inversion. `@theme inline` maps them onto Tailwind v4 / shadcn variables, and nothing outside that file writes a literal colour. Palette: `--bg` deep navy `#0f1420` / `#f3f4f7`, `--panel` `#161d2e` / `#ffffff`, `--panel-2`, `--line`, `--ink` warm off-white `#f2eee6` / `#141a2b`, `--ink-2`, `--ink-3`, `--brass` `#e0b25a` / `#8a6a1f`, `--brass-2`. Type: Instrument Serif for the page title and the hero coordinate only, Instrument Sans everywhere else, `tabular-nums` on every number, sentence case, no uppercase labels. Radius 6 px, 1 px rules instead of shadows. The theme is a per-browser preference (`localStorage` `mtrtk:theme`) applied before React mounts. `web/src/lib/tokens.test.ts` is the contract: both themes declare the same tokens, `@theme inline` carries no literal colour, and every text/surface pair clears WCAG AA — computed from the stylesheet, so a palette edit that breaks contrast fails the suite.
- *Constellation palette.* Fixed categorical order, never cycled: GPS `#3987e5`/`#2a78d6`, GLONASS `#d95926`/`#eb6834`, Galileo `#199e70`/`#1baf7a`, BeiDou `#c98500`/`#eda100`, QZSS `#d55181`/`#e87ba4`, SBAS `#9085e9`/`#4a3aa7` (dark/light); IMES, NavIC and unknown fall back to `--ink-3`. Validated with the dataviz checker on both surfaces. Status colours (`good #0ca30c`, `warning #fab219`, `serious #ec835a`, `critical #d03b3b`) are fixed across both themes and always paired with an icon and a word; a *status word set as text* uses the parallel `--status-*-text` tokens, which carry the same hues to AA on the active surface (light darkens all four; dark lightens only critical).
- *Brass budget.* The accent is spent on the sky plot's elevation rings, the active nav item, the focus ring and primary/active controls. Everything else is ink on panel.
- *Chart conventions.* Hand-written SVG, no chart library. One y-axis per chart — several metrics means several charts, never a second axis. Every multi-series chart carries a legend; every chart has a text alternative (a `Table` tab or a `<details>` beneath it); every plotted chart answers the pointer with an in-panel readout and a crosshair rather than a floating tooltip, which also works on touch; every SVG is `role="img"` with a `<title>` (or `aria-label`) that states its range. Text is never set in a series colour.
- *Live data.* Exactly one WebSocket per tab feeding a zustand store every live panel reads, so nothing on a page is a second out of step with anything else on it. Snapshot on connect, then diffs. Reconnect is exponential 1 s → 30 s with a visible state in the tape; a socket that has carried nothing for 30 s is presumed dead and reopened; `stale` is separate from `disconnected` and greys the readings after 5 s without an epoch.
- *Responsive.* Rail with labels ≥1024 px, 64 px icon rail 640–1023 px (labels kept in the accessible name), bottom tab bar <640 px; content max-width 1440 px on a 12-column grid; no horizontal page scroll at 360 px — wide tables scroll inside their own panel.
- *Build path.* `pnpm --dir web build` → `web/dist`. For a local daemon, `pnpm --dir web build:static` copies it into `src/mtrtk/web/static/`, which the FastAPI app mounts (`/assets`, SPA fallback on 404, and a 503 `"UI not built"` when it is absent). The image does not depend on that: `docker/Dockerfile` has its own `web` stage (node:22-alpine → `pnpm build`) whose output the runtime stage copies, so a built image always serves a UI. CI runs `build`, `test` and `lint` for `web/`.

### ros2
`mtrtk_msgs/RtkStatus.msg` (carr_soln, fix_type, corr_age, baseline_len, n_sat_used, hdop, pdop, ntrip_connected…); `mtrtk_bridge` rclpy node: WS client → `/mtrtk/fix` NavSatFix, `/mtrtk/vel` TwistWithCovarianceStamped, `/mtrtk/imu` Imu + `/mtrtk/heading` (when attitude), `/mtrtk/time_reference` TimeReference, `/mtrtk/rtk_status`, `/mtrtk/nmea` nmea_msgs/Sentence. Params: `ws_url`, `frame_id`. Dockerfiles `ros:humble` / `ros:jazzy`.

### docker / deploy
- `docker/Dockerfile`: stage `web` (node:22-alpine, `pnpm build`) → stage `rtklib` (build RTKLIB demo5 `convbin`, `rnx2rtkp`, plus `rnx2crx`) → stage `runtime` (python:3.12-slim, uv sync, copy binaries + SPA). Multi-arch via buildx; GH Actions publishes `ghcr.io/<user>/mtrtk`.
- `docker-compose.yml`: `mtrtk` service, `network_mode: host`, `volumes: [./data:/data, /dev:/dev]`, `device_cgroup_rules: ["c 166:* rmw", "c 188:* rmw"]`, `env_file: .env`, `restart: unless-stopped`, healthcheck `/healthz`. Profiles: `public` (Caddy TLS reverse proxy for web on `PUBLIC_DOMAIN`), `cloudflare` (`cloudflared` with `TUNNEL_TOKEN`; docs for routing `rtk.<domain>`→web, `ntrip.<domain>`→caster, NTRIP v2 only), `ros2`.
- `install.sh`: native path (uv venv, systemd unit, RTKLIB from apt or source).
- `mtrtk doctor`: checks serial access, firmware, tailscale IP, disk, RTKLIB binaries.

## Implementation phases (each ends with a working, testable milestone)

**Phase 0 — Scaffold** `git init`, MIT, pyproject (uv), ruff/mypy/pytest, Vite app skeleton, Dockerfile + compose, `.env.example`, CI (lint, tests, multi-arch build), commit design spec to `docs/superpowers/specs/2026-09-18-mtrtk-design.md`, then run `superpowers:writing-plans` for the detailed task plan. *Milestone:* `docker compose build` succeeds on amd64+arm64.

**Phase 1 — Core + replay + record** source/demux/bus/state/ubx_config/receiver, `mtrtk record` (writes fixture from live F9P), `mtrtk replay`. Record `tests/fixtures/f9p_base_120s.ubx` from the attached F9P. *Milestone:* replay prints live state; profile applies + verifies on real F9P; unit tests on fixtures pass.

**Phase 2 — Base daemon** rawlog writer/rotation/retention/index, NTRIP caster, survey-in + sites + TMODE3 fixed, alerts core, system stats, SQLite + sampler. *Milestone:* `str2str -in ntrip://…` and `gnssntripclient` receive RTCM over Tailscale; hourly files appear with sidecars; survey-in completes and 1005 flows.

**Phase 3 — Web backend** FastAPI app, REST routers, WS hub, jobs runner, config PUT, static SPA mount, healthz. *Milestone:* `curl /api/state`, `websocat /ws` show live data from replay.

**Phase 4 — Frontend (base pages)** design system (tokens, dark theme), Dashboard, Satellites, Receiver, Corrections, Site & Position, Logs & Export (list/download/keep), History, Events, Settings. *Milestone:* full base UI on replay + live F9P; Playwright smoke.

**Phase 5 — RINEX export + PPP import + wizard** splice, convbin wrapper, presets, Hatanaka/gzip, ppp_result parsers, PPP wizard UI, apply fixed. *Milestone:* 24 h export accepted by CSRS-PPP (user submits); result import → fixed mode verified.

**Phase 6 — F9P rover** driver interface + ublox driver, rover pipeline, NTRIP client, NMEA sinks, sessions, points + exports, RTK/Survey pages. *Milestone:* rover F9P (second unit or same unit in rover role vs recorded base stream) reaches RTK FIXED via caster over Tailscale; QGIS/gpsd reads NMEA TCP; points exported.

**Phase 7 — ROS2 bridge** msgs + node + Dockerfiles + compose profile. *Milestone:* `ros2 topic echo /mtrtk/fix` on Humble and Jazzy containers.

**Phase 8 — PPK** pipeline, rtkconf, pos parse/export, events interpolation, remote base fetch, PPK UI. *Milestone:* zero-baseline self-test in CI reaches ≥95 % fixed; real rover+base session produces track + events CSV.

**Phase 9 — Exposure + hardening + docs** public/cloudflare compose profiles (load `cloudflare` skill when implementing; verify NTRIP v2 streaming through tunnel), Caddy, password auth, `install.sh`, `doctor`, docs (setup, hardware/antenna placement, PPP workflow, PPK, ROS2, exposure, firmware upgrade), release pipeline. *Milestone:* fresh Pi: `git clone` → edit `.env` → `docker compose up -d` → RTK from a phone NTRIP client over Cloudflare domain.

**Phase 10 — INS drivers (spec-based)** sbgECom + VectorNav framers/parsers with hand-built protocol fixtures, RTCM inject, attitude → NMEA HDT/PASHR + ROS2 Imu, raw capture, vendor config subset, docs on what is unverified. *Milestone:* fixture-driven tests green; live validation deferred until hardware arrives.

## Testing strategy
- pytest unit: demux framing (incl. corrupted bytes), state mapping from fixtures, NMEA synth (round-trip with pynmeagps), geo conversions (known points), rotation with fake clock, retention, ppp_result parsers (sample files), rtkconf rendering, `.pos` parsing.
- Integration: caster with real clients (`pygnssutils.gnssntripclient`, raw socket v1/v2, auth), replay-driven end-to-end API/WS, convbin export on fixtures, zero-baseline PPK.
- `-m hardware`: live F9P profile apply/verify, survey-in start, MON-VER.
- Frontend: vitest + testing-library for formatters/sky plot/panels; Playwright smoke against replay.
- CI: lint, tests, multi-arch image build.

## Risks & mitigations
- **HPG 1.13**: some keys/messages missing (e.g., MON-SPAN/MON-COMMS uncertain, NAV-TIMELS). Runtime capability probe + gating; docs urge upgrade to 1.51. Unknown behavior differences flagged in events.
- **RTCM 1005 absent until survey-in valid** → rovers get MSM without base position (no RTK). UI shows clearly; alert; option to require valid position before caster serves.
- **RAWX while in TMODE3** fine on F9P; nav rate fixed at 1 Hz in base mode.
- **USB throughput**: ~10 KB/s at 1 Hz base; 5 Hz rover with all constellations ~40 KB/s. USB CDC is fine; Pi 3 CPU OK for pyubx2 parsing at these rates (profile early; parse-light path for RAWX/SFRBX = log raw without full parse).
- **Docker hotplug**: `/dev` bind + cgroup rules; by-id path stable across replug.
- **Tailscale IP inside container**: `network_mode: host` required; detect `tailscale0`; fallback warns.
- **Cloudflare Tunnel**: HTTP-only → NTRIP v2 TLS clients only; verify chunked streaming isn't buffered; UI auth via Cloudflare Access or `WEB_PASSWORD`.
- **PPP result formats** drift across service versions → tolerant parsers + manual form fallback; store frame/epoch.
- **SD wear** → fsync cadence, WAL, minute-level history rollups.
- **INS drivers unverified** (no hardware) → strict spec-driven parsers, fixtures, capability flags, clear docs.
- **RTKLIB demo5 build on arm64** in Docker → pinned source tag; fallback to Debian `rtklib` package if build fails.

### Open items to confirm during implementation (explicitly uncertain, from review)
1. MON-SPAN availability and `NAV-PVT.flags3.lastCorrectionAge` on PROTVER 27.12 → startup probe decides. → **Partially resolved 2026-09-19 (HPG 1.13):** MON-SPAN, MON-COMMS and NAV-TIMELS CFG keys are accepted and the messages stream; the capability probe uses CFG-VALGET key existence (HPG 1.13 does not answer MON-SPAN/MON-COMMS polls, only periodic output satisfies a waiter). `lastCorrectionAge` still unverified.
2. Whether `convbin -v 3.04` writes one mixed nav file (`-n`) or still splits `.gnav`/`.hnav` → handle both.
3. Exact RTKLIB demo5 tag to pin; semantics of `pos2-arthres1`, `pos2-rejionno`, `pos2-arlockcnt` at 5 Hz. → **Resolved:** RTKLIB demo5 pinned to `v2.5.1` (builds on debian bookworm with gfortran in the build stage). `pos2-*` semantics remain open for Phase 8.
4. OPUS acceptance of L2C-only (2L/2X) observations from F9P (no L2P) → document; may need `-od`/signal mapping.
5. CSRS-PPP current file-size/duration limits and exact `.sum` layout → build parser from a real result file (Phase 5 requires one real submission).
6. Whether F9P 1.13 emits MSM before TMODE is valid → affects "corrections inactive" UI wording and the optional gate. → **Resolved 2026-09-19:** HPG 1.13 emits RTCM MSM7 (1077/1087/1097/1127) and 1230 with `CFG_TMODE_MODE = 0`; 1005 requires a valid TMODE position. The "corrections inactive" UI wording should key off 1005 presence, not MSM.
7. `CFG-NAVSPG-SIGATTCOMP` and `CFG-HW-ANT_*` (antenna supervisor) behavior on the SparkFun board (no detect wiring) → probe; default off.
8. HPG 1.13 rejected no core key from the base or rover profiles (live VALSET + VALGET verify, 2026-09-19); `OPTIONAL_FEATURES` remains {MON-SPAN, MON-COMMS, NAV-TIMELS}, all supported on 1.13.

## Implementation reference (verified against pyubx2 configdb + local RTKLIB by review agent)

### CFG-VALSET key sets (pyubx2 spelling; ≤64 pairs per VALSET; one unknown key NAKs the whole set → core/optional split is mandatory)
- **Layers**: RAM|BBR|FLASH on explicit "apply profile" (user choice: persist to flash); RAM only on automatic reconnect (limits flash wear).
- **Common core**: `CFG_RATE_MEAS` (1000 base / 200 rover), `CFG_RATE_NAV=1`, `CFG_RATE_TIMEREF=1`; `CFG_USBOUTPROT_UBX=1`, `CFG_USBOUTPROT_NMEA=0`, `CFG_USBINPROT_UBX=1`, `CFG_USBINPROT_NMEA=0`; base `CFG_USBOUTPROT_RTCM3X=1`/`CFG_USBINPROT_RTCM3X=0`, rover inverse; `CFG_NAVSPG_DYNMODEL` (2 base; rover per env), `CFG_NAVSPG_INFIL_MINELEV=10`, `CFG_INFMSG_UBX_USB=0`; `CFG_ITFM_ENABLE=1`, `CFG_ITFM_ANTSETTING=2`; `CFG_MSGOUT_UBX_{NAV_PVT,NAV_SAT,NAV_SIG,NAV_DOP,NAV_STATUS,NAV_CLOCK,NAV_TIMEGPS,NAV_TIMEUTC,NAV_HPPOSLLH,NAV_HPPOSECEF,NAV_EOE,RXM_RAWX,RXM_SFRBX,MON_HW,MON_RF}_USB=1`, `NAV_TIMELS_USB=10`, `MON_COMMS_USB=5`.
- **Signals** (separate transactional VALSET, only when VALGET readback differs — change restarts GNSS engine): `CFG_SIGNAL_{GPS,GPS_L1CA,GPS_L2C,GLO,GLO_L1,GLO_L2,GAL,GAL_E1,GAL_E5B,BDS,BDS_B1,BDS_B2,QZSS,QZSS_L1CA,QZSS_L2C}_ENA=1`, `CFG_SIGNAL_SBAS_ENA=0`.
- **Base core**: `CFG_MSGOUT_UBX_NAV_SVIN_USB=1`; `CFG_MSGOUT_RTCM_3X_TYPE1005_USB=1`, MSM7 `TYPE1077/1087/1097/1127_USB=1` (MSM4 alt 1074/1084/1094/1124, never both), `TYPE1230_USB=5`, `TYPE4072_0_USB=0`, `TYPE4072_1_USB=0`; `CFG_RTCM_DF003_OUT=<station id>`; TMODE: `CFG_TMODE_MODE` 0/1/2, `CFG_TMODE_SVIN_MIN_DUR`, `CFG_TMODE_SVIN_ACC_LIMIT` (0.1 mm), `CFG_TMODE_POS_TYPE=0` (ECEF), `CFG_TMODE_ECEF_X/Y/Z` (cm) + `_HP` (0.1 mm, −99..99) via `pyubx2.ubxhelpers.val2sphp`, `CFG_TMODE_FIXED_POS_ACC` (0.1 mm).
- **Rover core**: `CFG_MSGOUT_UBX_{NAV_RELPOSNED,RXM_RTCM,TIM_TM2,NAV_VELNED}_USB=1`, `CFG_NAVHPG_DGNSSMODE=3` (RTK fixed).
- **Optional/probe** (own VALSET each; NAK → feature off, UI card hidden): `CFG_MSGOUT_UBX_MON_SPAN_USB=5`, `CFG_HW_ANT_CFG_*` (default off, board has no detect wiring), `CFG_NAVSPG_SIGATTCOMP`, `CFG_TP_ANT_CABLEDELAY`, `CFG_USB_SERIAL_NO_STR0..3` (two-receiver by-id uniqueness).
- **Never send on PROTVER 27.12**: L5/E5a/B1C/B2a/NavIC signal keys, `CFG_SIGNAL_PLAN`, all `NAV2_*`, `RXM_COR`, `MON_SYS`, `SEC_SIG`, `NAV_PL`, `NAV_TIMETRUSTED`, `CFG_NAVSPG_PL_ENA`, `CFG_GAL_OSNMA_*`, `CFG_RTCM_DF003_IN_FILTER`, `CFG_MSGOUT_RTCM_3X_TYPE1006_USB` (F9P never outputs 1006).
- **Capability probe** at each (re)connect: poll `MON-VER` → PROTVER → static table; empty-payload polls for MON-SPAN/MON-RF/MON-COMMS/NAV-SIG/RXM-RTCM (response vs NAK within 1 s); VALGET optional keys individually. Poll `SEC-UNIQID` for RINEX receiver serial.
- **ACK correlation**: single serial writer task; VALSET awaits `ACK-ACK`/`ACK-NAK` with payload `clsID=0x06,msgID=0x8A`, 2 s timeout, 3 retries; futures keyed (cls,id) FIFO. Rover RTCM-in is fire-and-forget on same writer.
- **Epoch assembly**: group NAV-* by `iTOW`, emit on `NAV-EOE` → drives sampler, WS snapshot, hour rotation. TMODE fixed ⇒ NAV-PVT `fixType=5` (time-only) — UI/NMEA must map it.

### NTRIP protocol gotchas
- v1 detection = no `Ntrip-Version` header. RTKLIB clients (`str2str`, RTKNAVI) send `GET /m HTTP/1.0`, need literal `ICY 200 OK\r\n\r\n`, cannot dechunk → never chunk v1.
- v2 200: `HTTP/1.1 200 OK`, `Ntrip-Version: Ntrip/2.0`, `Content-Type: gnss/data`, `Transfer-Encoding: chunked`, `Cache-Control: no-store, no-cache, max-age=0`, `Connection: close`; one chunk per RTCM frame; `0\r\n\r\n` on close.
- Sourcetable: v1 `SOURCETABLE 200 OK` + `Content-Type: text/plain` + `Content-Length`; v2 `HTTP/1.1 200 OK` + `Content-Type: gnss/sourcetable`; ends `ENDSOURCETABLE\r\n`; STR has 19 `;` fields. Unknown mount: v1 → sourcetable, v2 → 404.
- Auth: Basic, `hmac.compare_digest`; 401 + `WWW-Authenticate: Basic realm="mtrtk"`; lenient on missing `User-Agent`; header limit 8 KB / 10 s.
- New client immediately gets cached last 1005 + 1230 so RTK starts next epoch. GGA intake per client; `SOURCE`/`POST` → `ERROR - Not Supported`. Slow client: 64-frame drop-oldest queue, disconnect if write buffer > 256 KB for 10 s.
- Client: try v2 (`HTTP/1.1`, `Host`, `Ntrip-Version`); accept `ICY 200 OK` raw or `HTTP/1.x 200` (+dechunk); `SOURCETABLE 200 OK` = mount missing (60 s backoff); 401 → 60 s backoff + alert; else one retry as pure v1 then 1→60 s jittered backoff; no-data timeout 10 s; incoming bytes re-framed with CRC check before serial write.

### convbin / RINEX
- Splice `[from − 1 h, to]` (extra hour for ephemeris), single run with `-ts/-te`.
- `convbin -r ubx -v 3.04 -od -os -oi -ot -ol -f 2 -ts … -te … [-ti 30] -ro "-TADJ=1.0" -hm MARKER -hn NUM -ht GEODETIC -ho "obs/agency" -hr "sn/u-blox ZED-F9P/HPG x.xx" -ha "sn/ANTTYPE" -hp X/Y/Z -hd h/e/n -o NAME.rnx -n NAME_MN.rnx spliced.ubx`. `-f 2` on 1.13 (L1/L2), `-f 3` once L5 on 1.51. `-TADJ=1.0` aligns receiver time tags to integer seconds (needed for `-ti 30`). OPUS adds `-y R -y E -y C -y J -y S -y I` and RINEX 2.11.
- Local `convbin` is stock 2.4.3 b34 and `rnx2crx` is absent → both built in Docker (RTKLIB demo5 pinned tag + RNXCMP 4.1.0).
- CSRS-PPP: static, ITRF20 @ obs epoch outside Canada, `.sum` sigmas are 95 % → ÷1.96 for 1σ; `ANT_TYPE=NONE` ⇒ result refers to ARP. AUSPOS: ≥1 h (2 h+ better), ≤7 days, GPS used. OPUS: GPS only, 15 min–48 h; F9P L2C (2L/2X) not L2W → acceptance must be verified.

### rnx2rtkp conf (demo5) additions beyond baseline
`pos2-arfilter=on`, `pos2-arthresmin=3`, `pos2-arthresmax=3`, `pos2-arthres1=0.1`, `pos2-varholdamb=0.1`, `pos2-gainholdamb=0.01`, `pos2-minfixsats=4`, `pos2-minholdsats=5`, `pos2-mindropsats=10`, `pos2-arlockcnt=5`, `pos2-aroutcnt=20`, `pos2-rejionno=1`, `pos2-rejgdop=30`, `pos2-slipthres=0.05`, `stats-eratio1/2=300`, `stats-errphase=0.003`, `stats-prnaccelh=3`, `stats-prnaccelv=1`, `out-timesys=gpst`, `out-outstat=residual`, `ant2-postype=xyz` + `ant2-pos1/2/3`, `ant2-maxaveep=1`. Consider `pos1-exclsats` for BDS GEO C01–C05. `gloarmode=on` valid only F9P↔F9P.

### Additional risks (from review)
- ModemManager grabs `ttyACM*` on some hosts → udev rule `ID_MM_DEVICE_IGNORE=1` in `install.sh`/docs.
- by-id symlink is generic (`u-blox_GNSS_receiver-if00`); two receivers on one host need `CFG_USB_SERIAL_NO_STR*`.
- `pyserial-asyncio` unmaintained → use `pyserial-asyncio-fast` behind a transport interface.
- Pi has no RTC: name/rotate by GPS time; alert when |host − GPS| > 1 s.
- SparkFun antenna has no ANTEX entry (PCO unknown, cm-level vertical bias); all results refer to ARP.
- Survey-in absolute accuracy ~1–2 m; rovers inherit that offset until PPP coordinates are applied — UI must say so.
- Measured USB rate now 4.1 kB/s; ~6 kB/s base with MSM7+MON; ~20 kB/s at 5 Hz rover. Watch MON-COMMS `txUsage`/`overrunErrs`.

## Verification (end-to-end)
1. `docker compose up` on this x86 box with the attached F9P: UI shows sats/position/RF; hourly UBX files with sidecars; `str2str -in ntrip://user:pass@100.100.50.10:2101/MTRK -out file://out.rtcm` receives 1005/1077/…; RTCM counters in UI match.
2. Survey-in → freeze site → fixed mode → `fixType=5` and 1005 present.
3. Export 24 h RINEX (CSRS preset) → user submits to CSRS-PPP → import `.sum` → site → fixed.
4. Rover role (replay of recorded rover stream or second F9P) → NTRIP client connected → RTK FIXED → NMEA TCP visible in QGIS; points collected/exported; ROS2 `ros2 topic echo /mtrtk/fix`.
5. PPK zero-baseline self-test and a real session → `.pos`, CSV, events CSV.
6. Fresh Raspberry Pi 64-bit: clone, `.env`, `docker compose up -d`, phone NTRIP client (SW Maps) gets RTK over Tailscale; optional Cloudflare domain path.
