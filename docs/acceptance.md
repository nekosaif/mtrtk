# Acceptance

The end-to-end checklist for an mtrtk release, from the spec's *Verification* section plus the
Phase 9 host checks. Each row says what to run and what must happen. The *Result* column records
the last run: the date (UTC), the host and, where a receiver was involved, its firmware.

- **verified-here**: run on the date given, on the host named, with the evidence in the notes
  under the table.
- **partial**: the parts that need no extra hardware were run; the rest is pending.
- **pending-user**: needs something the development box does not have: a Raspberry Pi, a
  Cloudflare domain and tunnel token, a phone NTRIP client, open sky, a second F9P.

When you run a pending row, replace its *Result* cell with your date, host and firmware, and add
what you saw (time to fix, any stall) under [Notes](#notes). A row that fails is a bug: open an
issue, or fix it and say which commit did.

## Checklist

| Step | Command / action | Expected | Result (date, host, firmware) |
|---|---|---|---|
| 1. Compose up on the dev box | `docker compose up -d` with the F9P attached; open the UI; `str2str -in ntrip://rover:<pw>@<tailscale-ip>:2101/MTRK -out file://out.rtcm` | UI shows satellites, position, RF; hourly `.ubx` files with `.json` sidecars in `data/ubx/`; str2str receives 1005, 1077, 1087, 1097, 1127, 1230; the UI's RTCM rate matches str2str's within 10 % | **partial**, 2026-10-02, dev box (Ubuntu 24.04 x86_64), HPG 1.13: **not a compose run**. A read-only check of the live base, which is the native daemon (`mtrtk base` under `systemd --user`) with its F9P on a remote host over a Tailscale serial link and loopback binds: hourly files, MSM and 1230 from str2str and the RTCM rate passed; 1005 is missing (survey-in has not converged indoors); the UI's satellites, position and RF were not looked at. The compose path, on a replay source, is in row 6. See [row 1](#row-1-regression-against-the-live-base) |
| 2. Survey-in, site, fixed | Survey-in under open sky, freeze it as a site, `BASE_MODE=fixed` with `ACTIVE_SITE` | NAV-PVT `fixType` 5 (time only); RTCM 1005 present and within 0.5 mm of the site on every ECEF axis (the 1005 check passes, `site_verified`) | **partial**, 2026-09-19, dev box, HPG 1.13: on a site from `sites activate` (Phase 2 live test) the fix went to *Time only*, 1005 came at 1 Hz (92 in 92 s) and `site_verified` fired. Survey-in validation **pending-user**: indoors NAV-SVIN meanAcc stays near 12.5 m |
| 3. PPP round trip | Export 24 h RINEX (`mtrtk export --preset csrs-ppp --from ... --to ... --out ...`), submit to CSRS-PPP, `mtrtk ppp-import <file>.sum --activate` | The site is created from the PPP result; the base runs fixed on it | **pending-user**: needs 24 h under open sky and a CSRS-PPP account (the Phase 5 gate). The import step no longer rests on reconstructed files: on 2026-10-02 the CSRS-PPP importer was fixed against NRCan's published sample results (`tests/fixtures/ppp/csrs_v3_*`), and the AUSPOS SINEX and OPUS importers were checked against genuine published outputs ([ppp-workflow.md](ppp-workflow.md)). A result for this station is still missing |
| 4. Rover | `ROLE=rover`, `NTRIP_URL` to the base; QGIS on `NMEA_TCP_PORT`; collect and export points; `docker compose --profile ros2 up -d` then `ros2 topic echo /mtrtk/fix` | RTK FIXED; NMEA visible in QGIS; points exported; NavSatFix messages on `/mtrtk/fix` | **pending-user** with a second F9P (Phases 6 and 7 were tested against replayed captures) |
| 5. PPK | PPK zero-baseline self-test, then a real base + rover session | `.pos`, CSV and events CSV; zero baseline at least 95 % fixed | **pending-user** and an open ruling: a same-file zero baseline is 100 % float (see [Open items](#open-items)) |
| 6. Fresh host | [Fresh-host procedure](#fresh-host-procedure): clone, `.env`, `docker compose --profile cloudflare up -d`, `mtrtk doctor`; phone RTK over Tailscale, then over the Cloudflare domain; browser on `https://rtk.<domain>` | `doctor` exits 0; FIXED on the phone both ways; dashboard live after login | **partial**, 2026-10-02, throwaway clone on the dev box with a replay source: clone, `.env`, compose config for every profile, image build, compose up, `doctor`, NTRIP v2 and `check-exposure.sh` on loopback. Pi, phone and Cloudflare **pending-user**. See [row 6](#row-6-fresh-host) |
| 7. Native install | `docker compose down`, then `./install.sh` on the same host | `mtrtk.service` active, `mtrtk doctor` exits 0, UI on port 8080 | **partial**, 2026-10-02, throwaway Ubuntu 24.04 container running systemd: install, service, UI and `doctor` exit 0 on a replay source. With the F9P and Tailscale on a real host **pending-user**. See [row 7](#row-7-native-install) |
| 8. Backup and restore | `mtrtk backup --out FILE` on host A; `mtrtk restore FILE` on host B before its first start | The sites are present on host B; the archived `.env` is masked and not applied | **verified-here**, 2026-10-02: dev-box source checkout to the native container, and image to image. See [row 8](#row-8-backup-and-restore) |
| 9. Receiver power cycle | Unplug the F9P (or cut its power) for 10 s while the daemon runs, plug it back | The daemon reconnects, re-applies the profile (RAM layer), raw logging resumes within 30 s, an event is logged | **pending-user**: the F9P used by the live base (on a remote host) is not at hand and is in use by that daemon (reconnect logic is covered by `tests/unit/test_receiver.py`) |
| 10. Host reboot | `sudo reboot` with `WEB_BIND=tailscale` | The container (or `mtrtk.service`) comes back by itself; while `tailscale0` has no address the daemon retries the bind every 5 s and listens nowhere; it never falls back to `0.0.0.0` | **partial**, 2026-10-02: bind retry with no `tailscale0`, then a bind within 5 s once it appears (image); `mtrtk.service` back after a container reboot (native). A real reboot of a Pi **pending-user**. See [row 10](#row-10-host-reboot) |

## Fresh-host procedure

On the fresh host (Raspberry Pi OS 64-bit, or Ubuntu if no Pi is at hand; say which in the
result):

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER && newgrp docker
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk && cp .env.example .env
$EDITOR .env    # ROLE=base NTRIP_PASSWORD=... STATION_ID=... WEB_PASSWORD=... TUNNEL_TOKEN=... WEB_BIND=lan NTRIP_BIND=lan
docker compose --profile cloudflare up -d
docker compose exec mtrtk mtrtk doctor
```

The tunnel's public hostnames are set in Cloudflare Zero Trust
([exposure.md](exposure.md#cloudflare-tunnel)): `rtk.<domain>` to `http://127.0.0.1:8080`,
`ntrip.<domain>` to `http://127.0.0.1:2101`.

1. Phone on mobile data with the Tailscale app on: SW Maps, NTRIP client to
   `<tailscale-ip>:2101`, mountpoint `MTRK`, user `rover` and `NTRIP_PASSWORD`. Wait for FIXED and
   note the time to fix.
2. Tailscale off: the same client to `ntrip.<domain>`, port 443, TLS on, mountpoint `MTRK`. Wait
   for FIXED; note the time to fix and any stall (this is the first test of the NTRIP v2 stream
   through Cloudflare's edge).
3. From any machine, this prints `OK` with the time to the first RTCM byte:

   ```bash
   scripts/check-exposure.sh https://rtk.<domain> https://ntrip.<domain>/MTRK rover
   ```

4. Browser: `https://rtk.<domain>`, log in with `WEB_PASSWORD`, the dashboard updates live
   (this also tests the WebSocket through the tunnel).

## Notes

Results from 2026-10-02 on the development box: Ubuntu 24.04.5 x86_64, Docker 29.8.1, compose
v5.5.1, image built from `dddf53f` (python 3.12 on Debian bookworm, RTKLIB demo5 v2.5.1). The
dev box has no receiver on its USB: the F9P used by the live base (HPG 1.13, on a remote host) is
reached over a Tailscale serial link and is in use by that daemon, so the image and native runs
used a replay of `tests/fixtures/f9p_hpg113_base_30s.ubx` (`MTRTK_SOURCE=file:...`, `REPLAY_LOOP=1`) and
never saw a serial device.

### Row 1: regression against the live base

This was not the compose path. The live base on the dev box is the native daemon from the main
checkout: `mtrtk base` run by a `systemd --user` unit, with `MTRTK_SOURCE` a virtual serial port
that reaches the F9P (HPG 1.13) on a remote host over a Tailscale link, `WEB_BIND` and
`NTRIP_BIND` on `127.0.0.1`, and no container. The check was read-only: HTTP requests, one
`str2str` pull and a listing of finished files. Compose with a replay source is covered by
[row 6](#row-6-fresh-host); compose with an attached F9P still needs a run.

`/healthz` answers `ok`; the hourly files `MTRK_20261002_03..06.ubx` with their `.json` sidecars
(`complete: true`, `time_source: receiver`) are in `data/ubx/2026/275/`; a 15 s `str2str` pull from
the caster got 1077, 1087, 1097 and 1127 (15 each) and 1230 (4) at about 4.3 kbit/s (about
540 B/s), against the UI's 580 B/s (4.6 kbit/s): within 10 %, the difference being the two
averaging windows. No 1005: the base is still in survey-in after 41 318 s with NAV-SVIN meanAcc
12.5 m against the 2.0 m limit (the antenna is indoors), and 1005 needs a valid TMODE position.
The UI's satellites, position and RF views were not looked at in this run.

### Row 6: fresh host

In a fresh `git clone`, `cp .env.example .env` (unedited), then `docker compose config -q` with no
profile and with each of `ros2`, `public` and `cloudflare`: all exit 0. The same with an edited
`.env` and all three profiles together: exit 0, services `mtrtk caddy cloudflared mtrtk-ros2`.

`docker build -f docker/Dockerfile` then `docker/smoke.sh` in the image: `smoke ok: x86_64, 2
epochs`. The image was started with the real `docker-compose.yml` plus this override, which only
renames it and keeps the dev box's receivers out of it (no `/dev`, no device rules), on high
loopback ports:

```yaml
# compose.acceptance.yml, used as:
#   docker compose -p mtrtk-acc -f docker-compose.yml -f compose.acceptance.yml up -d
services:
  mtrtk:
    image: mtrtk:acceptance
    pull_policy: never
    container_name: mtrtk-acc
    volumes: !override
      - ./data:/data
    device_cgroup_rules: !reset []
```

with `WEB_BIND=127.0.0.1 WEB_PORT=18087 NTRIP_BIND=127.0.0.1 NTRIP_PORT=12107 NMEA_TCP_PORT=-1`,
`WEB_PASSWORD` and `NTRIP_PASSWORD` set. Seen:

- `healthy` within the first interval; `/healthz` ok; `/api/status` 401 without a token, 200 after
  `POST /api/login`; a wrong password 401; the UI's `index.html` served.
- tini and the daemon run as uid/gid 1000 with groups 20 (`DIALOUT_GID`) and 1000, `CapEff` 0,
  `NoNewPrivs` 1; the container has `cap_drop: ALL`, `no-new-privileges`, a `/tmp` tmpfs and
  `restart: unless-stopped`; files it wrote in `./data` are owned by 1000.
- `docker compose exec mtrtk mtrtk doctor` exits 0 (WARN for the host clock, which cannot be read
  inside a container, and `[WARN] tailscale 100.100.50.10` with no fix line, although Tailscale
  was up and no bind used it; fixed in `87fbf1a`, after which doctor reports it as OK);
  `doctor --json` is valid JSON with the same verdicts.
- `str2str` over NTRIP v1 for 20 s: 1077, 1087, 1097, 1127 (21 each) and 1230 (5); no 1005, since
  the replayed capture was recorded during survey-in.
- `scripts/check-exposure.sh http://127.0.0.1:18087 http://127.0.0.1:12107/MTRK rover`: `OK: 18
  RTCM3 frames in 2070 bytes; first byte after 0.1 s`; with a wrong password it says `401` and
  exits 1.
- `docker compose down` stops it at once and the hour's sidecar is written.

Not run: `--profile public` (it binds the host's 80 and 443) and `--profile cloudflare` (no
tunnel token). Both are covered by `tests/unit/test_exposure_profiles.py` and by `compose config`.

On the dev box itself (source checkout, temporary `.env` and `DATA_DIR`), `mtrtk doctor --json`
reports the host as it is and exits 1, because no u-blox receiver is on its USB (the live base
reads its F9P over the network): `receiver` FAIL, `modemmanager` WARN (no udev rule installed),
everything else OK.

### Row 7: native install

A throwaway `ubuntu:24.04` container running systemd (an unprivileged container: `SYS_ADMIN` for
systemd's own cgroup, no host devices), a user `pi` with sudo, Node.js 22 from nodejs.org, a
`git clone` of the repository, then `./install.sh --dry-run` (exit 0) and `./install.sh`, which
took 1 min 32 s: uv installed, `.venv` synced, RTKLIB demo5 v2.5.1 built from source into
`/usr/local/bin`, the web UI built with pnpm 11, `.env` created with `DATA_DIR=/home/pi/mtrtk/data`
and a random `NTRIP_PASSWORD`, the udev rule installed, `mtrtk.service` enabled.

With no receiver and no Tailscale the service keeps restarting (`no u-blox receiver found`,
`Restart=always` every 5 s), `doctor` FAILs `receiver` and `tailscale`, and `install.sh` exits 1:
the right answer for a host with neither. (2026-10-03, F10: `auto` with no receiver no longer
exits; the service stays up, waits for `tailscale0` and scans USB. Not re-run.) After pointing
`.env` at the replay file with `WEB_BIND=lan`, `NTRIP_BIND=lan` and a `WEB_PASSWORD`:
`mtrtk.service` active, `mtrtk healthcheck` ok, the UI answers on 8080, `mtrtk doctor` exits 0
(WARN for Tailscale only).

On a real host this row still needs a run with the F9P plugged in and Tailscale up.

### Row 8: backup and restore

- Host A, the dev box's source checkout with a temporary `DATA_DIR`: two sites added with
  `mtrtk sites add`, `mtrtk backup --out mtrtk-backup.tar.gz`: a mode 0600 archive with
  `manifest.json`, `mtrtk.db`, `sites.json` and the `.env` with `NTRIP_PASSWORD=***`.
- Host B, the native container above: `systemctl stop mtrtk`, `mtrtk restore`, start: both sites
  listed, the archived `.env` left in `data/restored.env`, not applied. The notes leave out a
  step: host B had already run `mtrtk.service` on a replay source (row 7), so `data/mtrtk.db`
  existed, and `mtrtk restore` refuses that without `--force` (`<DATA_DIR>/mtrtk.db exists;
  pass --force to overwrite`). So the run either passed `--force` (which keeps `mtrtk.db.pre-restore-<UTC>`) or
  removed the database first, and which one was not recorded. The "before its first start" case
  itself is the image-to-image check below, on an empty data directory.
- A second empty `DATA_DIR` on host A: restore lists the one key that differs (`STATION_ID`); a
  second restore without `--force` refuses; with `--force` the old database is kept as
  `mtrtk.db.pre-restore-<UTC>`.
- Image to image: `docker compose exec mtrtk mtrtk backup --out /data/mtrtk-backup.tar.gz`, then
  `docker run ... restore /data/mtrtk-backup.tar.gz` and `sites list` against a new empty data
  directory: the site is there.

### Row 10: host reboot

- Image without `tailscale0` (its own network namespace), `WEB_BIND=tailscale`
  `NTRIP_BIND=tailscale`: `bind mode 'tailscale' not available yet ... retrying every 5s`, no
  listening socket at all, `mtrtk healthcheck` says `unhealthy: tailscale has no address yet` and
  exits 1. Adding a `tailscale0` interface with `100.101.1.2` to that namespace: within 5 s the
  caster and the web UI listen on `100.101.1.2:2101` and `:8080` only, and the healthcheck says
  `ok`.
- Native: `docker restart` of the systemd container: `mtrtk.service` active again at boot with no
  manual step, healthcheck ok.
- **pending-user** (F10, 2026-10-03): after a reboot of the Pi with a changed tailnet IP,
  `ss -ltn` shows the web UI and the caster on the new address within about 5 s and a
  `bind_changed` event appears; with the F9P unplugged and `MTRTK_SOURCE=auto`, the container
  stays healthy (UI served, `receiver_disconnected` raised) and picks the receiver up within a
  few seconds of it being plugged in.

## Open items

Carried from the plans; record the answer here when a run settles one.

| Item | Status |
|---|---|
| Cloudflare Tunnel: is the NTRIP v2 chunked stream buffered at the edge; does the WebSocket work | pending-user (row 6). Locally through a reverse proxy the first RTCM byte arrives after 0.1 s ([exposure.md](exposure.md#cloudflare-tunnel)) |
| Survey-in validation on the dev box | pending-user: needs the antenna under open sky (row 2) |
| Real CSRS-PPP `.sum` layout and limits | layout **settled 2026-10-02**: the CSRS-PPP `.sum`, `.pos` and `.zip` import as NRCan's published samples lay them out (`csrs_v3_*`), and the AUSPOS SINEX and OPUS reports as genuine published outputs do (`auspos_v3_str1.snx`, `epn_bkg_2025333.snx`, `opus_*.txt`; spec open items 4 and 5). Still pending-user (row 3): the service's upload limits and a result for this station |
| OPUS acceptance of F9P L2C | not tried: OPUS serves sites in the USA only ([ppp-workflow.md](ppp-workflow.md)). The report format the importer reads is confirmed against three real OPUS reports (2026-10-02) |
| Zero-baseline PPK at least 95 % fixed | open ruling: with the same file as rover and base every ambiguity is zero, rnx2rtkp never attempts a fix and the result is 100 % float at about 1 mm; the options are to amend the milestone or record an outdoor F9P + Ellipse-D splitter capture (row 5) |
| `NAV-PVT.flags3.lastCorrectionAge` on HPG 1.13 | unverified (rover row 4) |
| `CFG-HW-ANT_*` on the SparkFun board | probe only, default off |
| INS drivers (SBG Ellipse-D, VectorNav VN-200) | pending-user: the [hardware validation checklist](ins-drivers.md#hardware-validation-checklist) and its `# VERIFY` markers |
| The fd-level serial write path (`core/source.py` `_write_without_spinning`, hardening extras X4) on real ports | **pending-user, release gate**: tested on ptys only, and the live base runs the main checkout's daemon, so the old write path. Before release, run the branch's daemon on the live base F9P over its socat/Tailscale link and watch the startup `CFG-VALSET`/`CFG-VALGET`, the MON-VER polls, one reconnect, RTCM injection on a rover, and the Ellipse configure writes (`mtrtk ins config --apply`) |
| Map tiles carry the base position to OpenStreetMap and Esri (Phase 4 M11) | needs a decision: no "no basemap" preference exists; the tile requests reveal the area the map shows |
| `?token=` in the page URL is forwarded into the WebSocket URL (Phase 4) | needs a decision: still the manual bearer fallback (`web/src/app/App.tsx`, `web/src/lib/api.ts`); a token in a URL can end up in browser history and proxy logs |
| `Secure` login cookie (Phase 4) | fixed in the Phase 9 final wave: `Secure`, plus HSTS, whenever the request came over HTTPS, Caddy or the tunnel included |
| A bundle-size budget in CI (Phase 4, after F2) | needs a decision: `ci.yml` builds the SPA but sets no size limit |
| The rail's DOM order (Phase 4 E2 "fix rather than document") | needs a controller ruling: the hardening extras (X2) declined the reorder, citing desktop Tab order and the E3 skip link; nothing records the ruling being accepted or overruled |
| Hardening extras (X4) needs-a-decision list | needs decisions: RINEX preset systems vs the spec (CSRS GPS+GLO+GAL, AUSPOS GPS+GLO); pinning `CFG-TP-TIMEGRID_TP1` on the rover; cubic event interpolation; VectorNav output 1 (SatInfo/RawMeas) at `INS_BAUD=115200`; NMEA/JSON from an unvouched SBG UTC; the `NMEA_TCP_BIND=lan` default and PPK antenna heights; the live Ellipse slice's region-level privacy. User inputs: a real CSRS-PPP file, overlapping base and rover data, the zero-baseline ruling, UM004 for the VN-200 pinout, a push so the arm64/demo5/ROS CI legs run. Settled 2026-10-02: export errors that reach the API name paths relative to `DATA_DIR` (the CLI keeps the full path); an SBG Port B recovery publishes `receiver.recovered` and clears its `receiver_error`; a job deletion is published as `jobs.deleted` on the WebSocket `jobs` topic. Follow-up: PPK job errors (`run_ppk`'s cannot create / write, convbin's and file errors) still name host paths; reuse export's `_PathNames` with `DATA_DIR` there, with a job-level no-host-path test |
