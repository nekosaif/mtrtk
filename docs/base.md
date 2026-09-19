# Base station

## What runs

`mtrtk base` opens the ZED-F9P, applies the base profile (1 Hz navigation, RXM-RAWX + RXM-SFRBX,
RTCM3 MSM7 + 1005 + 1230), puts the receiver into survey-in or onto a fixed site, logs the raw UBX
stream hourly under `DATA_DIR/ubx/YYYY/DDD/`, serves the RTCM to rovers over NTRIP on
`NTRIP_BIND:NTRIP_PORT/MOUNTPOINT`, samples history into `DATA_DIR/mtrtk.db` and raises alerts.

Everything is one process. Each job is supervised on its own: a raw logger that dies on a full disk
is restarted with backoff and the caster keeps serving, and `SIGINT`/`SIGTERM` gives every job up to
15 s to close its file and hang up its rovers before the process exits.

`mtrtk run` does the same thing driven by `ROLE` in `.env`; `mtrtk doctor` checks receiver access,
Tailscale, RTKLIB and disk before you start.

## Minimal `.env`

```
ROLE=base
MTRTK_SOURCE=auto                 # or /dev/serial/by-id/usb-u-blox_...
DATA_DIR=/data
NTRIP_PASSWORD=choose-a-password  # empty value = anonymous (tailnet only!)
BASE_MODE=survey-in               # survey-in | fixed | off
SVIN_MIN_DURATION_S=300
SVIN_ACC_LIMIT_M=2.0
```

The base role refuses to start with `NTRIP_PASSWORD` unset — an *empty* value is the explicit
"anonymous" choice, which is only safe when `NTRIP_BIND=tailscale`. `NTRIP_BIND=tailscale` waits for
`tailscale0` to have an address and never silently falls back to `0.0.0.0`; `lan`, `all` and a
literal IP are the other options. `.env.example` documents every key.

Start it with `docker compose up -d`, or `uv run mtrtk base`. The status line, once per second:

```
05:40:34 3D        None      sats 29/53 lat 23.8373293 lon 90.2625455 h -32.47 hAcc 1.69 rtcm 623 B/s svin 324s σ12.57m …
05:41:43 Time only None      sats 27/52 lat 23.8373286 lon 90.2625454 h -32.49 hAcc 0.03 rtcm 553 B/s
```

The `svin` tail is only there while a survey-in is running; `…` becomes `✓` when it validates. The
second line is the same base a minute later, sitting on a fixed site: a base on a valid fixed
position reports fix type `Time only` and an hAcc of its configured position accuracy. That is what
a correctly configured base looks like, not a fault.

## Rovers connect with

```
ntrip://rover:<password>@<tailscale-ip>:2101/MTRK
```

The username is `NTRIP_USER` (default `rover`), the mountpoint is `MOUNTPOINT` (default `MTRK`).
Both NTRIP versions are served on the same port and the URL is the same for both — the client
picks the version:

- **v1** (`str2str`, RTKLIB, u-center, older field apps) sends no `Ntrip-Version` header. The
  caster answers `ICY 200 OK` and then raw RTCM3 bytes.
  `str2str -in ntrip://rover:pw@100.100.50.10:2101/MTRK -out serial://ttyUSB0:115200`
- **v2** (SW Maps, Lefebure, Emlid, `gnssntripclient`) sends `Ntrip-Version: Ntrip/2.0`. The caster
  answers `HTTP/1.1 200 OK` with `Content-Type: gnss/data` and `Transfer-Encoding: chunked`, one
  RTCM frame per chunk. Apps that ask for host, port, mountpoint, user and password separately are
  v2 clients; give them `100.100.50.10`, `2101`, `MTRK`, `rover`, the password.

`GET /` returns the sourcetable (no authentication needed) in the requesting version's dialect, so
a rover app's "browse mountpoints" button works. An unknown mountpoint gets the sourcetable back on
v1 and `404` on v2; a wrong password gets `401` with a `Basic realm="mtrtk"` challenge.

Every connection is logged to the `ntrip_clients_log` table with its IP, user agent, NTRIP version,
bytes sent, dropped frames and the reason it ended. A rover that sends GGA has its last position
recorded there too. A client whose socket backs up by more than 256 KB for 10 s is hung up, so one
stalled rover cannot slow the others down, and `NTRIP_MAX_CLIENTS` (default 32) caps how many are
served at once — beyond it a v1 client gets `ERROR - Too Many Clients` and a v2 client a `503`.

## Survey-in

With `BASE_MODE=survey-in` the daemon writes TMODE survey-in (`SVIN_MIN_DURATION_S`,
`SVIN_ACC_LIMIT_M`) to the receiver on every start. The survey ends when *both* the duration and the
accuracy limit are met — with a poor sky view the accuracy estimate can sit well above the limit for
tens of minutes, so a survey that never shows `✓` usually means the antenna, not the software.

MSM observations, and therefore usable RTK corrections, flow from the moment the profile is applied
(verified on HPG 1.13). RTCM **1005**, the station's own coordinate, is only broadcast once the
receiver holds a valid TMODE position — a valid survey-in or a fixed site.

**Indoors, a survey-in will not validate — and that is the receiver, not a bug.** Validity is
decided by the NAV-SVIN *mean accuracy* (`meanAcc`, the σ shown in the status line's `svin` tail),
not by the NAV-PVT `hAcc` the same line reports: under a roof `meanAcc` settles around 10 m and no
sane `SVIN_ACC_LIMIT_M` will ever be met, however long the survey runs. Put the antenna under open
sky, or skip the survey with `mtrtk sites add` + `mtrtk sites activate` and `BASE_MODE=fixed`.

Re-surveying needs a real change: on HPG 1.13 a CFG-VALSET that writes the *same* survey-in
parameters does not restart a survey that is already running, so a restart of the daemon alone
continues the old one. To start over, change an `SVIN_*` value, or set `BASE_MODE=off`, restart,
then set it back to `survey-in`.

Survey-in gives roughly 1–2 m of *absolute* accuracy. Rovers get centimetre-level *relative*
positions against it and inherit that same 1–2 m offset. For absolute coordinates, log 24 h, export
RINEX (Phase 5), submit it to CSRS-PPP and save the result as a site.

## Fixed sites

```bash
uv run mtrtk sites add roof --ecef 3980123.4567 123456.7890 4966789.0123 --sigma 0.01 \
    --source ppp --frame ITRF2020 --epoch 2026.7
uv run mtrtk sites add roof --llh 51.4778000 -0.0014000 45.123    # ECEF is preferred
uv run mtrtk sites list                                             # '*' marks the active site
uv run mtrtk sites activate roof
uv run mtrtk sites delete roof
```

`sites add` takes either `--ecef X Y Z` (metres, what a PPP report gives you) or
`--llh LAT LON H` (degrees and ellipsoidal height); `--sigma` is the 1-sigma per axis in metres,
whose 3D combination is stored as the site's `sigma_3d` and written to the receiver as the
fixed-position accuracy (0.01 m if `--sigma` is omitted). `sites delete` refuses the *active* site —
the base would go on broadcasting a position nothing could name or verify — so activate another
site first.

**What `activate` does, exactly:**

- Against a **running** daemon, `mtrtk sites activate NAME` switches the receiver to that fixed
  site within 10 s, **whatever `BASE_MODE` says** — the mode manager polls the active site every
  10 s and applies any row that was activated *after* it started. Activating a site while a
  survey-in is in progress therefore ends the survey and sits the base on the site. (The row that
  was already active at startup is the poll's baseline and is deliberately ignored, so a re-survey
  over a site surveyed earlier is not aborted ten seconds in.)
- On the **next start**, `BASE_MODE` governs: `survey-in` re-surveys and ignores the active row,
  `fixed` applies `ACTIVE_SITE` (or, if that is unset, whichever row is active). `BASE_MODE=fixed`
  with no site at all falls back to survey-in and says so on `base.mode`.

So for a permanent installation: activate the site *and* set `BASE_MODE=fixed`, otherwise the next
restart starts surveying again. TMODE writes go to RAM, BBR and flash, so they survive a power cycle
even if the daemon does not come back.

## The 1005 check

Once the receiver is on a fixed site, every broadcast RTCM 1005 is decoded and compared with the
site the daemon applied:

- All three ECEF axes within **0.5 mm** → `base.site_verified`, stored as event `site_verified`.
  In practice this arrives within a second of the switch.
- Any axis outside that → `base.site_mismatch` / event `site_mismatch`, with `dx`, `dy`, `dz`. That
  means the receiver is broadcasting something other than what you saved: a rejected configuration,
  a receiver still on an older position, or a mis-typed site.
- If no 1005 settles the question and the receiver has not reached `fixType == 5` ("Time only")
  **30 s** after the position was applied, the same `site_mismatch` event is raised with the
  observed fix type as its reason.

One event per edge, not per message: a site that goes bad after it was verified is reported, and one
that comes good again is reported too.

## Files and database

- `DATA_DIR/ubx/2026/262/MTRK_20260919_05.ubx` — one hour of raw UBX, named `STATION_YYYYMMDD_HH`
  from the *receiver's* UTC, filed by year and day-of-year. `LOG_MESSAGES` selects what goes in
  (RXM-RAWX, RXM-SFRBX, NAV-PVT, NAV-HPPOSLLH, NAV-SVIN, TIM-TM2, MON-VER by default).
- `…_05.json` — the sidecar beside it: per-message counts, byte count, sha256, firmware, the site
  name at the time the file was opened, `time_source`, `keep`, and `complete`. It is refreshed every
  60 s while the hour is open; `complete: true`, `end_utc` and `sha256` are written when it closes.
  An hour left open by a crash is finalised on the next start (`recovered: true`, and `end_utc:
  null` with `end_utc_source: "unknown"` — the host clock cannot stand in for receiver UTC).
- `DATA_DIR/mtrtk.db` — SQLite in WAL mode. Phase 2 fills `samples_1s` and `samples_1m` (position,
  accuracy, satellites, RTCM rate, system stats), `sites`, `events` and `ntrip_clients_log`. The
  `log_files` table exists and has its repository, but nothing writes to it yet: the raw logs are
  indexed by walking `DATA_DIR/ubx` and reading the sidecars, and Phase 3 is what mirrors them into
  the table. `sessions`, `points` and `jobs` are likewise for later phases. The CLI opens the same
  file, which is why `mtrtk sites …` works against a running daemon.
- Raw logging is on for a live receiver with no extra flag. Replaying a file writes no logs unless
  `REPLAY_LOG=1`.

Retention keeps `MIN_FREE_GB` free by deleting whole hours, oldest first. Two files are never
candidates: the newest hour (the one still being written) and any file whose sidecar says
`keep: true`.

## Alerts

`AlertEngine` writes to the `events` table and, if `ALERT_WEBHOOK_URL` is set, POSTs the same event
as `{"level","kind","message","ts","host","role"}`. A webhook URL is itself the credential for
ntfy, Discord and Slack, so a delivery failure is logged with its scheme and host only.

Most rules are **conditions**: one event when the condition starts, one `<kind>_cleared` when it
ends, and nothing in between however long it lasts.

| kind | raised | cleared |
| --- | --- | --- |
| `receiver_disconnected` | the link to the receiver drops | it reconnects |
| `receiver_error` | the receiver errors or refuses its profile | it is connected and configured again |
| `fix_lost` | a fix that existed stays below 3D for 10 s | the fix is 3D or better again |
| `jamming` | `jam_ind ≥ 200` or jamming state ≥ warning, held for 30 s | the next clean sample |
| `antenna_fault` | MON-RF antenna status short or open | any other antenna status |
| `disk_low` | free disk < `MIN_FREE_GB` | free ≥ 1.1 × `MIN_FREE_GB` |
| `disk_warning` | free disk < 1.5 × `MIN_FREE_GB` | free ≥ 1.5 × `MIN_FREE_GB` |
| `temperature_high` | host ≥ 80 °C for 60 s of consecutive samples | below 75 °C |
| `logger_backpressure` | the raw logger's queue reaches 2000 frames | it drains below 1000 |
| `sampler_failing` | the history sampler cannot write | it writes again |
| `site_mismatch` | the broadcast 1005 disagrees with the site, or no `fixType == 5` within 30 s | a 1005 verifies the site |

The two disk bands are deliberately independent. Retention holds free space just *above*
`MIN_FREE_GB`, which is precisely where a single shared threshold would leave `disk_low` raised for
ever; `disk_warning` is the earlier, wider band that still has headroom. Temperature, jamming and
fix loss all have to persist before anything is written, so a fan spinning up, a passing vehicle or
a one-epoch gap costs nothing. Back-pressure is a condition too, not a one-off: the raw logger says
when its queue has genuinely drained.

The rest are single records with no `_cleared`, deduplicated per kind for 5 minutes:
`log_pruned` (retention deleted an hour of raw log), `logger_error` (a raw-log write failed) and
`consumer_failed_<name>` (a supervised job crashed and was restarted, once per job name). Two more
are plain informational rows: `survey_in_valid` (with the mean accuracy and duration the survey
reached) and `site_verified`.

`mtrtk sites list` and the events table are the whole UI until Phase 3 adds the API.
