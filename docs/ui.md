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
| `lan` | the primary LAN address | **required** (`WEB_PASSWORD`) |
| `all` | `0.0.0.0` | **required** |
| an IP address | that address | **required** |

Outside `tailscale` the daemon refuses to start without `WEB_PASSWORD`, unless you set
`WEB_ALLOW_INSECURE=1` — which is for a network you already trust, and for nothing else. The
Settings page shows both, and `mtrtk doctor` prints the address it expects to be reachable on.

**With a password set**, any 401 sends the app to `/login`: one box, one password, and a cookie
the browser keeps. Sign out from the foot of the rail (on a phone, from the Settings page). There
is no user list and no second factor — a single shared password, which is why it belongs behind
Tailscale or a tunnel with its own access control, not on the open internet.

**With no password**, `/login` still renders and simply says so; the rest of the app is open to
anyone who can reach the port.

## The frame

Every page sits in the same frame.

- **The rail** (left) lists the nine pages and holds the theme toggle and Sign out. At 1024 px
  and up it shows labels, between 640 and 1023 px it shrinks to icons (the labels stay in the
  accessible name, so a screen reader still hears them), and below 640 px it becomes a bottom tab
  bar — a thumb's reach on a phone in the field.
- **The tape** across the top carries the same six readings on every page: UTC clock, fix badge,
  satellites used/tracked, horizontal accuracy, RTCM output rate, rovers connected, and a live
  indicator on the right. When the link or the data goes quiet, the tape is the first thing to say
  so. Below about 400 px it scrolls sideways rather than wrapping.

## The pages

**Dashboard** — the glance. The hero is the coordinate readout in the display serif, with
ellipsoidal and MSL heights and the horizontal/vertical accuracy under it; the *Coordinate format*
selector beside it switches the whole app between decimal degrees, degrees-minutes-seconds, UTM
and ECEF. Next to it the sky plot with its brass elevation rings, then the map. Underneath: *Fix*
(fix type, carrier solution, satellites, PDOP, uptime), *Satellites by system*, *Position mode*
(survey-in progress or the active site), *Corrections* (output rate, message types, rovers, bytes
sent) and *Recent* — sparklines of horizontal accuracy, satellites used and mean C/N0 over the
epochs this tab has seen.

**Satellites** — three views of the same set, chosen with the Sky / Signals / Table tabs, plus
per-system filter chips and a *Used only* switch. Sky is the polar plot: filled discs are used in
the fix, hollow ones are tracked only, colour is the constellation. Signals is the C/N0 bar chart,
one bar per signal with dashed reference lines at 20 and 40 dB-Hz. Table is the same data sorted
and searchable. Hovering any mark writes the full reading (`E9 · 31° el · 98° az · 37 dB-Hz ·
used`) into the caption under the chart.

**Receiver** — the radio and the box. One *RF block* panel per front end (block 0 is L1, block 1
is L2/L5) with the jamming indicator, AGC count, noise, I/Q balance, antenna state and self-test,
each with a trend line. *Spectrum* draws MON-SPAN for every block; firmware that does not support
it says so instead of showing an empty frame. *Antenna & hardware*, *Firmware*, *Time* and
*Ports* complete the picture, and the page's actions apply the profile, poll a message or reset
the receiver (cold, warm or hot — each behind a confirmation, because a cold reset drops the fix).

**Corrections** — what the base is sending. *RTCM 3 output* lists every message type the receiver
is emitting with its count, rate and last-seen time; 1005 appearing here is the signal that the
station's own coordinate is being broadcast. *Stream* is the aggregate rate. *NTRIP caster* lists
the rovers connected right now — address, client string, user, how long — and *Recent
connections* keeps the history, including from earlier runs of the daemon.

**Site** — the base's position mode and the saved sites. *Position mode* switches between
survey-in, fixed and off and writes the choice to the receiver; *Survey-in* shows the two gates
(elapsed time and the accuracy σ) as bars that both have to fill, plus the observation count and
the mean position; *Verification* reports whether the broadcast 1005 matches the active site.
Below, the sites table with Add site, Enter PPP result, Freeze as site and Activate. On a source
with no base-mode manager — a replay, or the rover role — the whole mode section is disabled and
says why: sites can still be saved, and a live base picks the active one up at its next start.

**Logs** — the raw UBX on disk. The availability strip covers the last 48 hours, one cell per
hour: brass is a complete hour, grey a partial one, empty means missing. Click an hour to load it
into the window form beside, which downloads every overlapping file concatenated (48-hour cap).
The files table gives size, RAWX epoch count and state, with per-file download, *keep* (exempt
from retention) and delete. *Jobs* is where exports will land in Phase 5.

**History** — the SQLite rollups. Pick a range (1 h / 6 h / 24 h / 7 d / 90 d) and any number of
metrics from the catalogue — position, satellites, RF, corrections, system. Each metric gets its
own chart, because one y-axis per chart is the only way the scales stay honest. Hovering a chart
reads out the timestamp and value; *Show as table* under each one gives the same numbers as text.

**Events** — what the daemon thought was worth recording: a lost fix, RF interference, a
survey-in finishing, a disk filling up. Filter by level, acknowledge a row to clear it from the
unacknowledged count.

**Settings** — every `.env` field, grouped (Station, Receiver, Base position, RTCM output, NTRIP
caster, Web UI, Raw logging, Alerts, Replay, Rover, Deployment). Only the fields you actually
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

## Keyboard and accessibility

- Every control is reachable with Tab in DOM order: rail, then the tape, then the page.
- Focus is always visible — a two-pixel brass ring, offset, never removed.
- Tab groups (the Satellites views) use the standard roving-tabindex pattern: one Tab stop for the
  group, then arrow keys between the tabs.
- Colour is never the only carrier. A status badge is a bordered pill with an icon *and* a word; a
  constellation has its colour in a legend *and* its name in the table.
- Every chart has a text alternative — a *Table* tab or a `<details>` under the chart — and every
  SVG carries a `role="img"` with a title that reads its range.
- `prefers-reduced-motion` is respected: the satellite discs stop gliding, everything else already
  only animates when data moves or you acted.
- The bottom tab bar keeps clear of the home indicator (`env(safe-area-inset-bottom)`).

## Development

```bash
# terminal 1 — a daemon with no hardware, on :8080
WEB_BIND=lan WEB_ALLOW_INSECURE=1 NTRIP_PASSWORD= \
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
into the Tailwind mapping, and every text/surface pair clears WCAG AA. Constellation colours are
fixed and never cycled — GPS blue, GLONASS orange, Galileo green, BeiDou yellow, QZSS magenta,
SBAS violet — and were checked for colour-vision deficiency on both surfaces.

## Troubleshooting

**`{"detail": "UI not built; run 'pnpm --dir web build' or use the Docker image"}`** — the daemon
is running but `src/mtrtk/web/static/index.html` does not exist. Either run
`pnpm --dir web build:static`, or use the image (`docker compose up -d`), which builds the SPA in
its own stage. The API and `/healthz` work either way; it is only the page that is missing.

**The tape says "0 rovers" but the Corrections page lists some.** The tape's count comes from the
live socket, which only learns about clients when the caster next reports a change; the
Corrections page asks `/api/ntrip/clients` directly. Reload, or trust the page — the caster is the
authority. (Tracked for the Phase 4 fix wave: seed the count from the connect snapshot.)

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
**Under Docker Compose, a plain restart is not enough**: `env_file:` values reach the process as
environment variables, which outrank the file, so `docker compose up -d` (a recreate) is what
applies them. On systemd, *Restart now* is enough.

**The map is a blank grid.** The host cannot reach the tile servers. Everything else on the panel
is live — the marker, the accuracy circle, the rovers — and the grid clears itself as soon as a
tile loads.

**The page will not load over anything but Tailscale.** That is the default `WEB_BIND=tailscale`.
Set `WEB_BIND=lan` (and a `WEB_PASSWORD`) in Settings or `.env`, then restart.
