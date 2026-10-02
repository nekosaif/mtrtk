# Architecture

mtrtk runs as one asyncio process per host. Bytes from the receiver are split into frames and
published on an in-process pub/sub bus. Every consumer (raw logger, caster, state, sampler,
alerts, WebSocket, rover outputs) subscribes to the bus with its own queue and runs under restart
supervision. RTKLIB runs as a subprocess inside background jobs. The ROS 2 bridge is a separate
WebSocket client.

The README has a simplified version of this diagram. This one shows every component.

```mermaid
flowchart TB
  subgraph HW["Receivers"]
    F9P["u-blox ZED-F9P<br/>USB CDC"]
    INS["SBG Ellipse-D / VectorNav VN-200<br/>INS_PORT serial"]
  end

  subgraph D["mtrtk daemon: one asyncio process, ROLE=base or rover"]
    SRC["Source<br/>serial or file replay"]
    CTRL["ReceiverController<br/>capability probe, CFG-VALSET,<br/>VALGET verify, reconnect, watchdog"]
    RT["Router + framer<br/>UBX, RTCM3, NMEA, sbgECom, VN binary"]
    BUS(("Bus<br/>pub/sub"))
    STATE["StateStore<br/>ReceiverState"]
    LOG["RawLogWriter<br/>hourly .ubx + JSON sidecar"]
    RET["Retention<br/>MIN_FREE_GB"]
    CAST["NtripCaster v1 + v2<br/>base role"]
    BM["BaseModeManager<br/>survey-in or fixed site, 1005 check"]
    SAMP["Sampler<br/>1 s and 1 min rows"]
    ALR["AlertEngine"]
    SYS["SystemMonitor<br/>CPU, disk, temperature"]
    ROV["Rover services<br/>NTRIP client, NMEA and JSON out,<br/>sessions, survey points"]
    WEB["FastAPI REST + /ws<br/>+ React SPA"]
    JOBS["JobRunner<br/>RINEX export, PPK"]
  end

  DISK[("DATA_DIR<br/>ubx/YYYY/DDD, jobs/")]
  DB[("mtrtk.db<br/>SQLite WAL")]
  RTKLIB["RTKLIB<br/>convbin, rnx2rtkp"]
  HOOK["ALERT_WEBHOOK_URL"]
  NROV["NTRIP rovers"]
  UPCAST["Upstream NTRIP caster<br/>for example an mtrtk base"]
  APPS["NMEA / JSON consumers<br/>gpsd, QGIS, SW Maps"]
  UI["Browser"]
  ROS["mtrtk_bridge<br/>ROS 2 Humble / Jazzy"]
  TOPICS["ROS 2 topics<br/>/mtrtk/fix, vel, rtk_status, time_mark, imu, heading, nmea"]

  F9P -->|bytes| SRC
  INS -->|bytes| SRC
  SRC --> RT --> BUS
  CTRL -.->|"profile, TMODE3, polls, resets"| F9P
  BM --> CTRL
  BUS --> STATE
  BUS --> LOG
  BUS --> CAST
  BUS --> BM
  BUS --> SAMP
  BUS --> ALR
  BUS --> ROV
  STATE --> WEB
  BUS -->|"live topics"| WEB
  SYS --> BUS
  LOG --> DISK
  RET --> DISK
  SAMP --> DB
  ALR --> DB
  ALR -->|POST| HOOK
  CAST -->|RTCM3| NROV
  UPCAST -->|RTCM3| ROV
  ROV -.->|"inject RTCM"| F9P
  ROV -.->|"inject RTCM"| INS
  ROV --> APPS
  ROV --> DB
  WEB --> JOBS
  JOBS --> RTKLIB
  JOBS --> DISK
  UI <-->|"HTTP + WebSocket"| WEB
  ROS <-->|"WebSocket /ws"| WEB
  ROS --> TOPICS
```

- **Base role:** caster, base-mode manager, raw logger and retention. **Rover role:** NTRIP client, outputs and survey points instead of the caster. Both roles run the web API, sampler, alerts and system monitor.
- **INS rover:** the vendor driver opens `INS_PORT` and fills the same `ReceiverState` in place of the u-blox `ReceiverController` and profile. Nothing downstream changes.
- **Replay:** the live receiver's bus subscription drops the oldest frames when it backs up; a replay (`file:` source) never drops. A replay is passive: it sends no configuration and runs no base-mode manager, so survey-in, TMODE3 writes and the 1005 check need a live receiver. It writes no raw logs unless `REPLAY_LOG=1`.
- **Remote base:** PPK fetches a remote base's raw hours from that base's own API (`GET /api/logs/window`).

The design specification is in
[`superpowers/specs/2026-09-18-mtrtk-design.md`](superpowers/specs/2026-09-18-mtrtk-design.md).
