# Troubleshooting

Start with `mtrtk doctor` (`docker compose exec mtrtk mtrtk doctor` under Docker). Most lines that
are not `OK` come with a `fix:` (`INFO` lines are information only). Then look at the log (`docker compose logs -f mtrtk` or
`journalctl -u mtrtk -f`) and the Events page. The web UI's own problems (the map, pending
settings, a blank page) are also in [ui.md](ui.md#troubleshooting).

## Receiver and host

| Symptom | Cause | Fix |
|---|---|---|
| `no u-blox receiver found on USB`, or the log repeats a reconnect | The receiver is not plugged in, the cable carries power only, or the source names another device | `ls -l /dev/serial/by-id/` must show a `u-blox` link. Try another USB-C cable (many carry no data). Set `MTRTK_SOURCE=auto` or the exact by-id path |
| `/dev/... does not exist` for a path that is not a USB port | A tunnelled receiver whose `socat` (or `ser2net`) is not running | Start the link first ([exposure.md](exposure.md#remote-receivers-over-tailscale)) |
| `no read/write permission`, `Permission denied` on `/dev/ttyACM0` | The user (or the container) is not in the group that owns the device | Native: `sudo usermod -aG dialout $USER`, then log out and back in. Docker: set `DIALOUT_GID` to `stat -c %g /dev/ttyACM0` and run `docker compose up -d`. `MTRTK_RUN_AS_ROOT=1` is the last resort |
| The port appears about 30 s after plugging in, or the first configuration fails with garbage | ModemManager is probing the receiver with AT commands | Install `udev/99-mtrtk-ublox.rules` ([hardware.md](hardware.md#modemmanager)); doctor's `modemmanager` line says whether it is in place |
| `receiver configuration failed: receiver rejected core config keys: [...]` and the daemon exits 1 | The firmware refused a key the profile needs (`RECEIVER_STRICT=1`) | Note the keys and the firmware (Receiver page or `mtrtk doctor --probe`) and report them. `RECEIVER_STRICT=0` runs with what the receiver accepted |
| Configuration times out on a tunnelled receiver | A relayed Tailscale link stalls for seconds | `RECEIVER_ACK_TIMEOUT_S=5` (up to 30) |
| `receiver configuration failed: configuration verification got no answers after 3 attempts (N keys)` and the daemon exits 1 | The first start's readback heard nothing back: the link to the receiver is down or too slow (a receiver that refuses every CFG-VALGET is reported the same way) | Check the link (the `socat` relay, the cable) and raise `RECEIVER_ACK_TIMEOUT_S` for a tunnelled receiver. `RECEIVER_STRICT=0` does not help here: it only reconnects for ever. Once the daemon has configured the receiver, a silent reconnect is reported as `receiver.error` and retried |
| `jamming` or `antenna_fault` events, low C/N0 everywhere | RF interference, a bad cable or antenna | [hardware.md](hardware.md#rf-interference) |
| doctor warns `time_sync: host clock not NTP-synchronized` | The host has no NTP (a Pi without network at boot has no clock) | `sudo timedatectl set-ntp true`, or install `chrony`. Raw logs rotate on the receiver's time either way, but export names and event times use the host clock. mtrtk has no clock-skew alert of its own yet: doctor's check is the only warning |
| High CPU on a Raspberry Pi 3 (rover) | A 5 Hz rover parses about 20 kB/s of UBX | Lower `ROVER_NAV_HZ` to 2 or 1 |

## Starting up

| Symptom | Cause | Fix |
|---|---|---|
| `NTRIP_PASSWORD must be set for the base role` | The base never runs with the password undecided | Set `NTRIP_PASSWORD=<password>`, or `NTRIP_PASSWORD=` (empty) for anonymous rovers on the tailnet |
| `WEB_PASSWORD must be set when WEB_BIND is not 'tailscale'` | Any bind wider than Tailscale needs a login | Set `WEB_PASSWORD`, or `WEB_ALLOW_INSECURE=1` on a network you trust ([exposure.md](exposure.md#threat-notes)) |
| UI and caster unreachable after a reboot; the log says `bind mode 'tailscale' not available yet (is tailscaled running?); retrying every 5s` | mtrtk started before Tailscale had an address. It keeps retrying and never falls back to `0.0.0.0` | Wait: it binds as soon as `tailscale0` has an address. If it never does: `sudo systemctl enable --now tailscaled`, `sudo tailscale up`, and turn off key expiry for the station in the Tailscale admin console, or it drops off the tailnet when its key expires. The native unit already starts `After=tailscaled.service` |
| doctor: `ports: 2101 held by <program>` (FAIL) | Another program, such as a second caster, holds the port | Stop it, or change `NTRIP_PORT` / `WEB_PORT` |
| doctor: `ports: 2101 held by mtrtk (pid N)` (OK) | An mtrtk daemon is already running and owns its own ports | Nothing, if that is the station. Stop it before you start a second one on the same ports |
| A setting changed in the UI never applies under Docker | The repository's `.env` reaches the container as environment variables, which win over `data/.env`, the file the UI writes | Change it in `.env` and run `docker compose up -d` ([setup.md](setup.md#2-clone-and-configure)) |

## Web UI

| Symptom | Cause | Fix |
|---|---|---|
| The container is `healthy` but the UI shows no data and the tape keeps reconnecting | The page loaded but its WebSocket (`/ws`) is blocked: a corporate proxy, an ad or script blocker, or a reverse proxy without WebSocket support | Try another browser or network, or allow the station's host in the blocker. Caddy and the Cloudflare Tunnel pass WebSockets as they are; another proxy must forward `Upgrade` |
| A replay connects, but the tape says *live · waiting for epochs* and the map *Waiting for a position fix* | The recording has no NAV-EOE, which closes each epoch (`tests/fixtures/f9p_hpg113_raw_10s.ubx` and `_60s.ubx` lack it) | Replay `tests/fixtures/f9p_hpg113_base_30s.ubx`, or record with the profile applied |
| `{"detail": "UI not built; ..."}` (503) | A source checkout without the built SPA | `pnpm --dir web build:static`, or use the Docker image ([ui.md](ui.md#troubleshooting)) |
| The readings grey out | No epoch for 5 s while the socket is open: the receiver stopped talking | Check the USB cable and the antenna; the log shows the reconnect |

## Base station and rovers

| Symptom | Cause | Fix |
|---|---|---|
| No RTCM 1005 in the Corrections list; rovers never fix | The receiver has no valid time mode: the survey-in has not finished, or `BASE_MODE=off` | Wait for the survey-in (the Site page shows its σ against `SVIN_ACC_LIMIT_M`), or activate a site with `BASE_MODE=fixed` ([base.md](base.md#survey-in)). MSM flows without a position; 1005 does not |
| The survey-in never validates | The antenna has a poor sky view; indoors the mean accuracy stays around 10 m | Move the antenna under open sky, or save a site and use `BASE_MODE=fixed` |
| `site_mismatch` event | The 1005 the receiver broadcasts differs from the active site, or no "Time only" fix 30 s after the write | Re-activate the site; check its coordinates ([base.md](base.md#the-1005-check)) |
| The rover stays FLOAT | A baseline over about 20 km; a base position that is badly wrong; an antenna with a poor sky view or no ground plane; a rover that expects other messages than the base sends | Keep the baseline short. Give the base a PPP site ([ppp-workflow.md](ppp-workflow.md)). Fix the rover antenna ([hardware.md](hardware.md#the-antenna)). For a non-u-blox rover, try `RTCM_MSM=4` and check it uses GLONASS 1230 |
| Rover: `corrections_stale` / "No corrections" on the RTK page | The NTRIP client cannot reach the caster, or the password or mountpoint is wrong | Check `NTRIP_URL`, the base, then Tailscale, in that order ([rover.md](rover.md)). `scripts/check-exposure.sh` from the rover host tells which |
| `check-exposure.sh` through the tunnel: headers but no RTCM, or a buffering hint | The tunnel holds the chunked stream | Serve rovers over Tailscale or the public-IP path ([exposure.md](exposure.md#cloudflare-tunnel)) |
| Caddy has no certificate (`docker compose logs caddy` shows ACME errors) | Ports 80 and 443 are not forwarded, the DNS name points elsewhere, or the line is behind CGNAT | Fix the forward and the `A` record; behind CGNAT use the Cloudflare Tunnel |

## Logs, exports and PPK

| Symptom | Cause | Fix |
|---|---|---|
| Hours missing from the Logs page | The disk reached `MIN_FREE_GB` and retention deleted the oldest hours (`log_pruned` events), the receiver was disconnected (`receiver_disconnected`), or the logger failed (`logger_error`) | Free space or lower `MIN_FREE_GB`; mark hours *keep* to protect them; check the Events page for the hour in question |
| A RINEX export is refused before it starts | `STATION_ID` or `COUNTRY` cannot name the files, or the export would leave less than half of `MIN_FREE_GB` free | Fix the setting (four letters or digits; ISO alpha-3), or free space |
| CSRS-PPP rejects the upload or the solution is poor | Too short a window (the export warns under 1 h and under 90 % coverage), the wrong interval, or a marker name the service will not take | Use the `csrs-ppp` preset (30 s, RINEX 3.04, Hatanaka + gzip) on a full 24 h; keep `MARKER_NAME` to plain letters and digits. CSRS-PPP's own limits have not been checked against a real upload yet ([ppp-workflow.md](ppp-workflow.md)) |
| PPK gives 0 % fixed | No navigation data (a RINEX base without its `--base-nav`), base and rover windows that do not overlap, wrong base coordinates, or the base and rover are the same receiver's logs | Give the nav file; check `gaps` and the warnings in `summary.json`; give the right `--site` or `--base-xyz`. A zero-baseline self-test never fixes in RTKLIB ([ppk.md](ppk.md#reading-the-result)) |
| PPK says "base position unknown" | No site, no `--base-xyz`, and no active site on the base | Give one of them |

## ROS 2 bridge

| Symptom | Cause | Fix |
|---|---|---|
| `ros2 topic list` has no `/mtrtk/*` topics | The bridge runs on another `ROS_DOMAIN_ID`, or is not running | Use the same `ROS_DOMAIN_ID` on both sides; `docker compose --profile ros2 logs mtrtk-ros2` |
| Topics exist but `/mtrtk/fix` is always no-fix | The bridge cannot reach the daemon's WebSocket: `ws_url` (`MTRTK_WS_URL`) is `ws://127.0.0.1:8080/ws` while `WEB_BIND=tailscale` listens on the tailnet address only | `MTRTK_WS_URL=ws://<tailscale-ip>:8080/ws`, or `WEB_BIND=lan` |
| The bridge logs `handshake refused with HTTP 403` | `WEB_PASSWORD` is set and the bridge has no token | Put the token from `POST /api/login` in `MTRTK_WS_TOKEN` ([ros2.md](ros2.md)) |
