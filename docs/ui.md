# Web UI

The daemon serves a single-page app from the same port as its API. Open
`http://<bind-host>:8080/` — the Tailscale IP by default — and you get the whole base station:
where it is, how well it sees the sky, what the receiver's radio is doing, what corrections are
going out and to whom, the raw files on disk, and every setting in `.env`.

Nothing here is a separate service. `/api` and `/ws` are documented in [`docs/api.md`](api.md);
this page is about the screen in front of you.

## Reaching it

`WEB_BIND` decides which address the daemon listens on:

| `WEB_BIND` | Listens on | Password |
|---|---|---|
| `tailscale` (default) | the host's Tailscale IP | optional — the tailnet is the boundary |
| `lan` | every interface (`0.0.0.0`, same as `all`) | **required** (`WEB_PASSWORD`) |
| `all` | `0.0.0.0` | **required** |
| an IP address | that address | **required** |

Outside `tailscale` the daemon refuses to start without `WEB_PASSWORD`, unless you set
`WEB_ALLOW_INSECURE=1` — which is for a network you already trust, and for nothing else. The
Settings page shows both, and `mtrtk doctor`'s exposure line says what is reachable beyond
Tailscale without a password. `lan` and `all` differ only in what `mtrtk doctor` assumes about
reachability; to keep the UI off other interfaces (the tailnet, a public address), bind an IP
address.

**With a password set**, any 401 sends the app to `/login`: one box, one password, and a cookie
the browser keeps. Sign out from the foot of the rail (on a phone, from the Settings page). There
is no user list and no second factor — a single shared password, which is why it belongs behind
Tailscale or a tunnel with its own access control, not on the open internet.

**With no password**, `/login` still renders and simply says so; the rest of the app is open to
anyone who can reach the port, by an IP address or by a name the daemon knows (localhost, the
host's name, its MagicDNS name, `PUBLIC_DOMAIN`, `WEB_ALLOWED_HOSTS`). Any other name gets a
400 that names `WEB_ALLOWED_HOSTS`: a guard against DNS rebinding.

## The frame

Every page sits in the same frame.

- **The rail** (left) lists the ten pages of the role (a rover has RTK and Survey where a base
  has Corrections and Site) and holds the theme toggle and Sign out. At 1024 px
  and up it shows labels, between 640 and 1023 px it shrinks to icons (the labels stay in the
  accessible name, so a screen reader still hears them), and below 640 px it becomes a bottom tab
  bar — a thumb's reach on a phone in the field.
- **The tape** across the top carries the same six readings on every page: UTC clock, fix badge,
  satellites used/tracked, horizontal accuracy, RTCM output rate, rovers connected, and a live
  indicator on the right. When the link or the data goes quiet, the tape is the first thing to say
  so. Whenever the readings do not fit — in practice on a phone — the strip scrolls sideways
  rather than wrapping, without drawing a scrollbar of its own. Only the fix badge is announced
  to a screen reader, so a lost fix is spoken and the ticking clock is not.

## The pages

**Dashboard** — the glance. The hero is the coordinate readout in the display serif, with
ellipsoidal and MSL heights and the horizontal/vertical accuracy under it; the *Coordinate format*
selector beside it switches the whole app between decimal degrees, degrees-minutes-seconds, UTM
and ECEF. Next to it the sky plot with its brass elevation rings, then the map. Underneath: *Fix*
(fix type, carrier solution, satellites, PDOP, uptime), *Satellites by system*, *Position mode*
(survey-in progress or the active site), *Corrections* (output rate, message types, rovers, bytes
sent), *Recent* — sparklines of horizontal accuracy, satellites used and mean C/N0 over the
epochs this tab has seen, each point at its own time so a gap shows as a gap — and *Host*: CPU,
load, memory, free disk, temperature and uptime of the machine the daemon runs on, the figures the
alert rules watch.

**Satellites** — three views of the same set, chosen with the Sky / Signals / Table tabs, plus
per-system filter chips and a *Used only* switch. Sky is the polar plot: filled discs are used in
the fix, hollow ones are tracked only, colour is the constellation. Signals is the C/N0 bar chart,
one bar per signal with dashed reference lines at 20 and 40 dB-Hz. Table is the same data, sortable
by any column and narrowed by the same chips. Pointing at any mark — or tapping it — writes the
full reading (`E9 · 31° el · 98° az · 37 dB-Hz · used`) into the caption under the chart.

**Receiver** — the radio and the box. One *RF block* panel per front end, in the order the
receiver reports them, with the jamming indicator, AGC count, noise, I/Q balance, antenna state and
self-test, each with a trend line. HPG 1.13 reports `block_id` 0 for *both* MON-RF blocks, so when
the ids repeat the panels are numbered by position (0, 1) instead; nothing in the message itself
says which band a block is on. *Spectrum* draws MON-SPAN for every block, bin *i* at
centre + span · (i − 128) / 256 as u-blox defines it; firmware that does not support it says so
instead of showing an empty frame. *Antenna & hardware*, *Firmware*, *Time* and *Ports* complete
the picture, and the page's actions re-apply the profile, poll a message or reset the receiver:
hot, warm, cold or factory, each behind a confirmation. Cold drops the fix, and cold and factory
ask for the word typed — factory clears BBR *and* flash, every setting the daemon wrote, and the
daemon re-persists its profile when the receiver comes back. A reset that sees no receiver within
90 s says so instead of waiting forever.

**Corrections** — what the base is sending. *RTCM 3 output* lists every message type the receiver
is emitting with its count, rate and last-seen time; 1005 appearing here is the signal that the
station's own coordinate is being broadcast. *Stream* is the aggregate rate. *NTRIP caster* is the
caster itself — where it listens, bind mode, mountpoint, authentication, clients against the limit
and how many callers were turned away. *Connected rovers* lists the rovers connected right now —
address, client string, user, how long — and *Recent connections* keeps the history, including
from earlier runs of the daemon. A rover still connected shows its live bytes sent and last
position on its open row, the same figures as *Connected rovers*; the final ones are written when
it disconnects. The tape, the Dashboard's rover count and this page read the same list.

**Site** — the base's position mode and the saved sites. *Position mode* switches between
survey-in, fixed and off and writes the choice to the receiver; *Survey-in* shows the two gates
(elapsed time and the accuracy σ) as bars that both have to fill, plus the observation count and
the mean position; *Verification* reports whether the broadcast 1005 matches the active site.
Below, the sites table with Add site, Freeze as site and Activate. On a source with no base-mode
manager — a replay, or the rover role — the whole mode section is disabled and says why: sites can
still be saved, and a live base picks the active one up at its next start.

*Centimetre site from PPP* walks the three steps. Step 1 counts the hours of raw data on disk in
the last 24 h and links to *Export the last 24 h for CSRS-PPP*, which opens Logs with the export
panel set up and scrolled into view. Step 2 lists the PPP services. Step 3 holds *Import PPP
result* and, for typing the numbers by hand, *Enter PPP result*. The import dialog reads a CSRS-PPP
`.sum` or `.pos` (or the `.zip` they arrive in), an AUSPOS SINEX `.snx`, or the OPUS e-mail saved
as `.txt`, with an OPUS frame choice (ITRF or NAD83; the other services give one frame and ignore
it). It only parses the file: before anything is saved it shows the source, frame @ epoch, X/Y/Z
each with its 1σ per ECEF axis (a dash where the file gives none), the geodetic position and the
parser's notes. The site is saved under the suggested, editable name; *Activate it* is on by
default — a running base switches to fixed mode on that site within 10 s, and the mode and site
are saved to `.env` — and the button says *Save and activate* while it is checked. A file that
cannot be read shows what went wrong, what to upload instead and the file's first lines; pick a
file again (the same one included) to retry. If the site was saved but activating it failed, the
dialog says so and offers *Retry activation* without saving the row twice.

On an INS rover (`ROVER_DRIVER=sbg_ellipse` or `vectornav`) the Receiver page shows the unit
instead of the u-blox panels: *INS unit* (identity and link), *INS filter* (mode, heading, the
GNSS fix the filter sees and, on a dual-antenna Ellipse-D, the antenna baseline), *IMU*, *Lever
arms* and *INS configuration*, the profile table with *Re-read configuration* and, behind a
confirm, *Apply INS configuration*. `docs/ins-drivers.md` explains the read-only-first flow.

**RTK** (rover role) — whether corrections flow and what they do. *Corrections path* says when
the driver cannot take RTCM or has not shown it uses it; *NTRIP client* shows the caster, the
connection, the correction age and the last error, and *Change caster* edits `NTRIP_URL`.
*Solution* is the carrier solution and the NAV-RELPOSNED baseline to the base (length, N/E/D,
bearing to the base, accuracy, reference station). *Corrections received* is UBX-RXM-RTCM per
message type, *Fix state, last 10 minutes* the carrier solution over time, *Camera time marks*
the EXTINT pulses, *Attitude* the heading on a receiver or INS that reports one, and *Outputs*
the NMEA and JSON streams with their client counts. `docs/rover.md` says how to read it.

**Survey** (rover role) — sessions and points. *Session* opens and closes the session whose
window PPK can later process. *Collect a point* averages N epochs (RTK fixed only, by default)
under a name and code, with live progress and σ, and starts from `POINT_EPOCHS` and
`POINT_FIXED_ONLY`. The points table renames, recodes and deletes, filters by session and
exports CSV, GeoJSON, KML or GPX; the map shows the points.

**PPK** — post-processing with RTKLIB. *New PPK run* picks the rover data (a session, a UTC
window of this host's raw logs, or an uploaded file), the base (another mtrtk's web address, an
uploaded file, or this host's own logs), the base position (automatic, a saved site, or ECEF
X/Y/Z) and a few options. *PPK jobs* lists the runs with live progress; selecting one shows its
result: fixed / float / single shares, mean σ, the GPST time span, gaps, warnings, the camera
events placed and every output file. A run deleted in another tab drops out of the list at once,
and its result closes if it was the one shown. `docs/ppk.md` covers the workflow.

**Logs** — the raw UBX on disk. The availability strip covers the last 48 hours, one cell per
hour: the strongest cells are complete hours (`--series-1`, pale blue-grey in dark, deep navy in
light), muted grey ones partial, an empty outline missing; each cell's name and title say which. Click an hour to load it
into the window form beside, which downloads every overlapping file concatenated (48-hour cap).
The files table gives size, RAWX epoch count and state, with per-file download, *keep* (exempt
from retention) and delete.

*Export RINEX* turns a UTC window of raw hours into RINEX for a PPP service. *Target* picks a preset
— CSRS-PPP, AUSPOS, OPUS or generic — and shows its description, what the service asks of the data
(minimum and maximum span, frequencies) and its fixed options (RINEX version, interval, Hatanaka
and gzip, GPS only for OPUS), with a link to the service. The window defaults to the last 24 whole
hours and is capped at 7 days; an hour clicked on the strip loads into both the raw window and the
export. Only *generic* takes an interval (empty is the native rate), Hatanaka and gzip. One export
runs at a time, and a refusal — another export running, no raw logs in the window, not enough free
space on the card — is shown verbatim. The Site page opens this panel with `?export=<preset>&hours=<n>`.

*Export jobs* lists the exports, newest first: a worded status, a progress bar and the current
step while one runs, the window it covers and how long ago it was started. A failed job says the
step it stopped at and its error. A finished job lists its result files — observations,
navigation, manifest, with sizes — as downloads, each checked with the daemon before the browser
saves it (a file that has gone shows why rather than saving an error page under the RINEX name),
and the export's warnings, for example a window the data covers only partly, or under an hour of
data for a PPP service. Delete sits behind a confirmation and is held while a job runs; deleting a
queued job cancels it. Progress arrives live on the `jobs` topic over the REST listing, which is
polled every 5 s and is the truth for which jobs exist. A deletion is published too
(`jobs.deleted`), so a job deleted from another tab or device, or by retention, drops out at once,
a queued one included; after a reconnect the list comes from REST until new updates arrive.

**History** — the SQLite rollups. Pick a range (1 h / 6 h / 24 h / 7 d / 90 d) and any number of
metrics from the catalogue — position, satellites, RF, corrections, system. Each metric gets its
own chart, because one y-axis per chart is the only way the scales stay honest. Pointing at a
chart — or dragging a finger sideways across it — reads out the timestamp and value; *Show as
table* under each one gives the same numbers as text.

**Events** — what the daemon thought was worth recording: a lost fix, RF interference, a
survey-in finishing, a disk filling up. Filter by level, acknowledge a row to clear it from the
unacknowledged count.

**Settings** — every `.env` field, grouped (Station, Receiver, Base position, RTCM output, NTRIP
caster, Web UI, Raw logging, Alerts, Replay, Rover, INS, Deployment). Only the fields you actually
change are sent. Secrets come back masked as `***`; leave the mask alone to keep the stored value.
Some keys apply live, the rest need a restart — see *Pending changes* below.

## Live data

One WebSocket per tab, whatever is on screen. It opens on load, receives a full snapshot, then
diffs; every live panel reads the same store, so nothing on a page can be a second out of step
with anything else on it.

- **Reconnect** is exponential, 1 s doubling to a 30 s ceiling, and the tape shows the attempt
  rather than freezing on the last good numbers. A socket that has carried nothing for 30 s is
  presumed dead and reopened.
- **Stale** is its own state: if no epoch arrives for 5 s while the socket is still open, the
  readings grey out. That distinguishes "the network dropped" from "the receiver stopped talking"
  — the second is an antenna or USB problem, not a browser one.
- **A password prompt mid-session** is handled: a socket that is refused before it ever opens
  makes the app check `/api/status`, and only a real 401 sends you to `/login`.

## Coordinates, time and units

The coordinate format — DD, DMS, UTM or ECEF — is chosen on the Dashboard and applies everywhere
coordinates are shown. It is a per-browser preference stored in `localStorage` under
`mtrtk:coordMode`; the base station has no say in it and it is not synced between devices. Storage
that is blocked or full is not an error: the choice simply lasts for that page.

Heights are given both ways, ellipsoidal and MSL. Times are UTC, with the local time in the
tooltip. Every number is set in tabular figures so a changing digit does not shift the ones beside
it.

## Theme

Dark is the default and what the design was drawn for; light and *System* are the other two
choices, cycled from the toggle at the foot of the rail (on a phone, from the Settings page). The
choice lives in `localStorage` under `mtrtk:theme` and is applied before React mounts, so a light
browser never flashes the dark palette. *System* follows `prefers-color-scheme` for as long as the
tab is open.

## The map

MapLibre with OpenStreetMap raster tiles and an Esri World Imagery alternative, the base position
as a marker with its accuracy circle, and connected rovers from their GGA. **A base station is
often on a network with no route out.** When tiles cannot load the frame falls back to a plain
grid and keeps drawing the markers — position and accuracy are still readable, just without the
ground underneath. It clears itself the moment a tile arrives.

The map's own code (MapLibre, about 1 MB) loads only when a page first draws a map. If that load
fails — a dropped connection, or a daemon upgrade while the tab was open, which removes the file
the old page points at — the frame says *The map could not load* with a **Retry** button, and the
rest of the page keeps working. A page that fails to render for any other reason shows *This page
could not be shown* inside the page area; the rail and the tape stay, so you can move on or reload.

## Keyboard and accessibility

- The first Tab stop is *Skip to content*, visible only while focused, which jumps past the rail
  and the tape to the page. After it every control is reachable with Tab in DOM order: rail, then
  the tape, then the page — on a phone too, where the rail is painted at the bottom as a tab bar.
- Focus is always visible — a two-pixel brass ring, offset, never removed, at least 3:1 against
  every surface in both themes.
- Tab groups (the Satellites views) use the standard roving-tabindex pattern: one Tab stop for the
  group, then arrow keys between the tabs.
- Colour is never the only carrier. A status badge is a bordered pill with an icon *and* a word; a
  constellation has its colour in a legend *and* its name in the table. Status marks (badge
  borders and icons, gauge fills, fix-timeline cells) clear 3:1 against every surface; a status
  *word* takes the darker text form of its colour. The constellation colours do not all clear 3:1
  on the light panel (see *Development*), which is why their legend and table are load-bearing.
- Charts answer any pointer: a mouse, a pen, or a finger — a tap reads a mark, a sideways drag
  scrubs a line chart while a vertical swipe still scrolls the page, and the reading stays after
  the finger lifts.
- Every chart has a text alternative — a *Table* tab or a `<details>` under the chart — and every
  SVG carries a `role="img"` with a title that reads its range.
- `prefers-reduced-motion` is respected: the satellite discs stop gliding, *Centre on the base*
  jumps instead of panning, and everything else already only animates when data moves or you
  acted.
- The bottom tab bar keeps clear of the home indicator (`env(safe-area-inset-bottom)`).

## Development

```bash
# terminal 1 — a daemon with no hardware, on :8080
DATA_DIR=./data WEB_BIND=lan WEB_ALLOW_INSECURE=1 NTRIP_PASSWORD= \
  uv run mtrtk replay tests/fixtures/f9p_hpg113_base_30s.ubx --loop

# terminal 2 — Vite on :5173, proxying /api, /healthz and /ws to the daemon
pnpm --dir web dev

pnpm --dir web test     # vitest
pnpm --dir web lint     # tsc -b --noEmit
pnpm --dir web build    # production bundle into web/dist
```

`pnpm --dir web build:static` does the build and copies `web/dist` into
`src/mtrtk/web/static/`, which is where the daemon looks for the SPA when you run it directly.
The Docker image does the same thing in its own `web` stage, so a built image always serves a UI.

Design tokens — colours, fonts, radii, the constellation palette — live once in
`web/src/index.css`, in `:root` and `:root[data-theme="light"]`. `web/src/lib/tokens.test.ts`
holds them to their contract: both themes declare the same tokens, no literal colour is written
into the Tailwind mapping, body text (`--ink`, `--ink-2`), status words and error text clear WCAG
AA on the surfaces they sit on, and status marks, chart series and the focus ring clear 3:1. Not
every pair is AA: `--ink-3` is a deliberately quiet tone for axis ticks, hints and greyed (stale)
figures, and is not held to 4.5:1. `web/src/lib/themeCoverage.test.ts` checks that every colour
utility a component writes has a token behind it, so a typo cannot silently emit nothing. Chart
series take their own `--series-*` palette — never brass, a status colour or a constellation
hue, each of which already means something. Constellation colours are fixed and never cycled —
GPS blue, GLONASS orange, Galileo green, BeiDou yellow, QZSS magenta, SBAS violet — and were
checked for colour-vision deficiency on both surfaces. In the light theme four of them fall under
the 3:1 non-text floor: BeiDou 2.17:1, QZSS 2.69:1 and Galileo 2.82:1 on the white panel (1.82,
2.26 and 2.36 on `--panel-2`), and GLONASS, 3.20:1 on white, is 2.68:1 on `--panel-2`. They are
kept anyway, because the palette is fixed: every chart that uses them also names each system in a
legend and a table, and that is what carries the meaning.

## Troubleshooting

**``{"detail": "UI not built; run `pnpm --dir web build:static` or use the Docker image"}``** — the daemon
is running but `src/mtrtk/web/static/index.html` does not exist. Either run
`pnpm --dir web build:static`, or use the image (`docker compose up -d`), which builds the SPA in
its own stage. The API and `/healthz` work either way; it is only the page that is missing.

**The survey-in never validates.** The gate is the survey's *own* mean accuracy — NAV-SVIN
`meanAcc`, the σ the Site page draws — not the fix's `hAcc` shown in the tape. Under a roof
`meanAcc` settles around 10 m and no sane `SVIN_ACC_LIMIT_M` is ever met, however long you wait.
Put the antenna under open sky, or skip the survey: save a site and set the mode to fixed. Note
also that on HPG 1.13, writing the same survey-in parameters again does not restart a running
survey — use *Restart survey-in*.

**No RTCM 1005 in the Corrections list.** Observations (MSM) flow as soon as the profile is
applied, but 1005 — the station coordinate — is only broadcast once the receiver holds a valid
time mode: a completed survey-in or a fixed site. Rovers cannot fix without it.

**Settings says changes are pending.** `pending` is the daemon's account of where `.env` and the
running process disagree; it stays until a restart settles it. Some keys (the base mode, the
active site, the survey-in gates) apply live — everything else needs the process to come back.
**Under Docker Compose, the UI cannot change a key the repository's `.env` sets**: `env_file:`
values reach the process as environment variables, which outrank `data/.env`, the file the UI
writes, and a recreate (`docker compose up -d`) reads the repository's `.env` again, not the UI's
file. Such a change stays pending for good: make it in the repository's `.env` and run
`docker compose up -d`, or delete the key there so the UI can manage it
([setup.md](setup.md#2-clone-and-configure)). A key the repository's `.env` leaves out applies
after *Restart now*. On systemd, *Restart now* is enough.

**The map is a blank grid.** The host cannot reach the tile servers. Everything else on the panel
is live — the marker, the accuracy circle, the rovers — and the grid clears itself as soon as a
tile loads.

**The page will not load over anything but Tailscale.** That is the default `WEB_BIND=tailscale`.
Set `WEB_BIND=lan` (and a `WEB_PASSWORD`) in Settings or `.env`, then restart.
