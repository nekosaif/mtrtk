# Setup

This page takes a Linux box and a ZED-F9P from nothing to a running base station. It covers
Docker (the default) and the native install, keeping the station up to date, backups, and notes
for the Raspberry Pi and the Jetson. Antenna and receiver hardware are in
[hardware.md](hardware.md). Reaching the station from outside the tailnet is in
[exposure.md](exposure.md).

## 1. Prepare the host

You need a 64-bit Linux (Raspberry Pi OS 64-bit, Ubuntu 22.04/24.04, Debian 12, JetPack 6). The
images are built for `linux/amd64` and `linux/arm64`. `uname -m` must print `x86_64` or
`aarch64`.

```bash
# Docker Engine and the compose plugin (Docker's own convenience script)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER        # then log out and back in
docker compose version               # must print v2 or newer

# Tailscale: the default way rovers and browsers reach the station
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up                    # open the printed URL and log in
tailscale ip -4                      # the address you will use below: <tailscale-ip>
```

Plug in the F9P by USB-C and check that it shows up:

```bash
ls -l /dev/serial/by-id/             # usb-u-blox_AG_..._u-blox_GNSS_receiver-if00 -> ../../ttyACM0
```

## 2. Clone and configure

```bash
git clone https://github.com/nekosaif/mtrtk.git && cd mtrtk
```

On a host with ModemManager (Ubuntu desktop, most laptops), install the udev rule that keeps it
off the receiver. `install.sh` does this for you; the Docker path needs it by hand, from the
clone:

```bash
sudo install -m 0644 udev/99-mtrtk-ublox.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then make your settings file:

```bash
cp .env.example .env
$EDITOR .env
```

`.env.example` lists every setting with its default and a comment. The keys the template leaves
empty (`ACTIVE_SITE`, `WEB_PASSWORD`, `NTRIP_URL`, `ALERT_WEBHOOK_URL`, `PUBLIC_DOMAIN`, ...) count
as unset when empty. Any other key does not: `BAUD=` or `STATION_ID=` is an invalid value, and
the daemon refuses to start. To get a key's default back, delete the line rather than blanking
it. `NTRIP_PASSWORD=` (empty) is the one deliberate exception: it means anonymous rovers. These
eight are the ones to think about first:

| Setting | Set it to |
|---|---|
| `ROLE` | `base` for a base station, `rover` for a rover ([rover.md](rover.md)) |
| `NTRIP_PASSWORD` | A password for rovers. The base refuses to start while it is unset (the template's `change-me` counts as set: change it) |
| `STATION_ID` | Four upper-case letters or digits. It names the raw logs and the RINEX files |
| `COUNTRY` | ISO 3166 alpha-3 code (`BGD`, `DEU`, `USA`, ...). It goes into RINEX 3 file names |
| `BASE_MODE` | `survey-in` to start; `fixed` once you have a site ([base.md](base.md)) |
| `MTRTK_SOURCE` | `auto` finds the first u-blox receiver. Give a `/dev/serial/by-id/...` path when there are two |
| `WEB_BIND` / `WEB_PASSWORD` | Leave `WEB_BIND=tailscale` with no password, or set a password before any other bind |
| `DIALOUT_GID` | The host group that owns the serial device: `stat -c %g /dev/ttyACM0` (20 on Debian and Ubuntu) |

The rest, by section of `.env.example`:

- **role / receiver**: `BAUD`, `DATA_DIR` (leave `/data` under Docker; from a source checkout set
  `DATA_DIR=./data`, since `/data` is root-owned or missing on most hosts and the daemon cannot
  open its database there; `install.sh` does this for you), the RINEX header fields
  (`MARKER_NAME`, `ANTENNA_TYPE`, `ANTENNA_HEIGHT_M`, `OBSERVER`, `AGENCY`; see
  [ppp-workflow.md](ppp-workflow.md) before changing the antenna ones), `RECEIVER_STRICT`,
  `RECEIVER_ACK_TIMEOUT_S`, and the `REPLAY_*` switches for file sources.
- **base station**: survey-in limits (`SVIN_MIN_DURATION_S`, `SVIN_ACC_LIMIT_M`),
  `ACTIVE_SITE`, the RTCM message set (`RTCM_MSM`, `RTCM_1230_RATE`, `RTCM_STATION_ID`).
- **NTRIP caster**: `NTRIP_BIND`, `NTRIP_PORT`, `MOUNTPOINT`, `NTRIP_USER`, `NTRIP_MAX_CLIENTS`.
- **web UI**: `WEB_PORT`, `WEB_ALLOW_INSECURE`.
- **raw logging**: `LOG_MESSAGES`, `MIN_FREE_GB`, `FSYNC_INTERVAL_S`.
- **rover**: `NTRIP_URL` and the NMEA/JSON outputs ([rover.md](rover.md)).
- **alerts / exposure**: `ALERT_WEBHOOK_URL`, and `PUBLIC_DOMAIN`, `ACME_EMAIL`, `TUNNEL_TOKEN`
  for the `public` and `cloudflare` profiles ([exposure.md](exposure.md)).
- **INS drivers**: the `INS_*` settings ([ins-drivers.md](ins-drivers.md)).
- **ROS 2 bridge**: `ROS_DISTRO`, `ROS_DOMAIN_ID`, `MTRTK_WS_URL`, `MTRTK_WS_TOKEN`
  ([ros2.md](ros2.md)).
- **process / container**: `LOG_LEVEL`, `DIALOUT_GID`, `MTRTK_RUN_AS_ROOT`.

**Two settings files under Docker.** Compose hands the repository's `.env` to the container as
environment variables (`env_file: .env`). The web UI's Settings page writes a different file,
`data/.env` (the container's `/data/.env`, `MTRTK_ENV_FILE`), because the image's own directory
is read-only to the daemon. Environment variables win over that file. So a setting that is in
the repository's `.env` cannot be changed from the UI: change it in `.env` and run
`docker compose up -d`. A setting the repository's `.env` leaves out can be changed from the UI.
It applies after a restart, and it is kept in `data/.env`. A `.env` copied from `.env.example`
holds nearly every key, so under Docker that leaves the UI almost nothing it can change: the
Settings page accepts the write, but the change stays *pending* for good. Either edit settings in
the repository's `.env`, or delete from it the keys you want the UI to manage. The native install
has one file, the clone's `.env`, which both you and the UI edit. Keys that only compose reads
(`TUNNEL_TOKEN`, `PUBLIC_DOMAIN`, `ACME_EMAIL`) cannot be applied from the UI at all under Docker:
compose fills the `cloudflared` and `caddy` services from the repository's `.env` only.

## 3. Start and verify

```bash
docker compose up -d
docker compose exec mtrtk mtrtk doctor
docker compose logs -f               # one status line per second once the receiver is configured
```

`mtrtk doctor` checks Python, the receiver and its permissions, ModemManager, the host clock,
Tailscale, the ports, RTKLIB, Docker, the data directory and what is exposed beyond Tailscale.
Each line is `OK`, `WARN`, `FAIL` or `INFO`, with a `fix:` line under most that are not OK. It
exits 1 only on a `FAIL`. `mtrtk doctor --probe` also asks the receiver for its firmware; stop
the daemon first (`docker compose stop mtrtk`, then
`docker compose run --rm mtrtk doctor --probe`, then `docker compose start mtrtk` to bring it
back; native: `sudo systemctl stop mtrtk`, `.venv/bin/mtrtk doctor --probe`,
`sudo systemctl start mtrtk`), because only one program can read the port.

Then open `http://<tailscale-ip>:8080` from any device on your tailnet. The Dashboard shows the
position, the sky plot and the satellites within a few seconds. A survey-in takes at least
`SVIN_MIN_DURATION_S` (5 minutes) under open sky. Rovers connect to
`ntrip://rover:<NTRIP_PASSWORD>@<tailscale-ip>:2101/MTRK` ([base.md](base.md)).

The container is `healthy` once `GET /healthz` answers on the web port
(`docker compose ps`). With `WEB_BIND=tailscale` that waits for Tailscale: the daemon retries the
bind every 5 s and never falls back to `0.0.0.0`.

## Updating

### Docker images

`docker-compose.yml` runs `ghcr.io/nekosaif/mtrtk:latest`, and the `ros2` profile runs
`ghcr.io/nekosaif/mtrtk-ros2:${ROS_DISTRO:-humble}`. Every image is built for `linux/amd64` and
`linux/arm64`. The tags are:

| Tag | What it is | When it changes |
|---|---|---|
| `mtrtk:latest` | The newest release. This is the compose default. | When a `vX.Y.Z` release is published, and only if it is the newest release. |
| `mtrtk:X.Y.Z` (e.g. `mtrtk:0.1.0`) | One release, pinned. | Never. |
| `mtrtk:edge` | The current `main` branch. Its tests have passed, but it is not a release. | On every push to `main`. |
| `mtrtk-ros2:humble`, `mtrtk-ros2:jazzy` | The newest release of the ROS 2 bridge for that distro. | With `mtrtk:latest`. |
| `mtrtk-ros2:X.Y.Z-humble`, `mtrtk-ros2:X.Y.Z-jazzy` | One release of the bridge, pinned. | Never. |

To update to the newest release:

```bash
git pull                     # compose file, .env.example and docs for the new release
docker compose pull
docker compose up -d
```

Read the release's section in `CHANGELOG.md` first, and copy any new settings from
`.env.example` into `.env`. Settings that are not in `.env` keep their defaults.

`:latest` follows releases, not `main`. To pin a release or to track `main`, change the image in
a `docker-compose.override.yml` next to `docker-compose.yml`. Compose reads that file
automatically, so `git pull` never conflicts with it:

```yaml
services:
  mtrtk:
    image: ghcr.io/nekosaif/mtrtk:0.1.0   # or :edge to follow main
```

Then run `docker compose pull && docker compose up -d`. Delete the override to go back to
`:latest`. Before the first release, `:latest` is the last image that `main` built under the old
tagging, and it no longer changes. Use `:edge` until `0.1.0` is out.

To build from source rather than pull, run
`git pull && docker compose build && docker compose up -d`.

### Native install

`git pull && ./install.sh`. The installer is idempotent: it syncs the environment, rebuilds the
web UI, re-renders the unit and restarts the service.

## Logs

```bash
docker compose logs -f mtrtk         # Docker; json-file, 5 x 10 MB kept
journalctl -u mtrtk -f               # native install
```

`LOG_LEVEL=DEBUG` in `.env` (or `mtrtk -v`) shows every configuration write and reconnect. The
UI's Events page holds the alerts and state changes, kept 365 days in the database.

## Backups

The station's state is the database (`data/mtrtk.db`: sites, sessions, points, history, events)
and its settings. The raw logs are plain files under `data/ubx/`.

```bash
# Docker: the archive lands in ./data on the host
docker compose exec mtrtk mtrtk backup --out /data/mtrtk-backup.tar.gz
# native, from the clone
.venv/bin/mtrtk backup --out ~/mtrtk-backup.tar.gz
```

The backup is safe while the daemon runs. It holds a consistent SQLite snapshot, the sites as
JSON, a manifest, and the settings file the daemon reads with every password and token replaced
by `***`. `--with-secrets` keeps them, and the command then says the archive holds the station's
passwords. The archive is written owner-only (mode 0600).

Under Docker the settings file in the archive is `data/.env`, the one the UI writes, not the
repository's `.env`. Copy the repository's `.env` yourself. Raw logs are not in the archive
either: copy them with `rsync -a data/ubx/ <new-host>:mtrtk/data/ubx/`.

## Moving to a new host

1. On the old host: `docker compose exec mtrtk mtrtk backup --out /data/mtrtk-backup.tar.gz`,
   then `docker compose down`.
2. Copy `.env`, `data/mtrtk-backup.tar.gz` and, if you want them, `data/ubx/` to the new host.
3. On the new host: clone, put `.env` in the clone and the archive in `data/`, then restore
   before the first start:

   ```bash
   docker compose run --rm mtrtk restore /data/mtrtk-backup.tar.gz
   docker compose up -d
   ```

`restore` checks the archived database (integrity, and a schema no newer than this mtrtk) and
refuses while another process holds the database. An existing database is only replaced with
`--force`, and the replaced one is kept beside it. The archived settings are never applied: they
are written to `data/restored.env` (owner-only), and `restore` lists the keys that differ from
the current settings file, without their secret values, for you to merge by hand. Native:
`sudo systemctl stop mtrtk && .venv/bin/mtrtk restore ~/mtrtk-backup.tar.gz`.

## Raspberry Pi

- **Storage.** The base writes about 8-9 MB of raw UBX an hour (about 200 MB a day), plus
  fsyncs every `FSYNC_INTERVAL_S` and SQLite writes every second. Use a USB SSD, or at least a
  high-endurance microSD card. Retention deletes the oldest raw hours (never `keep` ones) when
  free space falls under `MIN_FREE_GB` (5 GB by default). On a small card, lower it, for example
  `MIN_FREE_GB=2`.
- **Power.** Use the official supply (27 W USB-C for a Pi 5, 15 W for a Pi 4). Undervoltage
  resets the USB bus, and the receiver drops off with it. `vcgencmd get_throttled` should print
  `throttled=0x0`. The F9P itself is fine on a Pi USB port.
- **Wi-Fi.** Turn power saving off, or the caster stalls for rovers:
  `sudo nmcli connection modify <wifi-connection> 802-11-wireless.powersave 2` (Raspberry Pi OS
  Bookworm with NetworkManager), or `sudo iw dev wlan0 set power_save off` until the next boot.
  Wired Ethernet is better for a base.
- **Clock.** The Pi has no real-time clock. Raw logs rotate on the receiver's time, but export
  names and event times use the host clock, so keep NTP on (`timedatectl` must say
  `System clock synchronized: yes`; `mtrtk doctor` checks it).
- **Pi 3 and other slow boards.** A 5 Hz rover parses about 20 kB/s of UBX. If the CPU is pinned,
  lower `ROVER_NAV_HZ` to 2 or 1.

## Jetson

- Use the `arm64` image (compose pulls the right one) on JetPack 6 (Ubuntu 22.04). For the ROS 2
  bridge, `ROS_DISTRO=humble` matches JetPack 6.
- The F9P is `/dev/ttyACM0` over USB, as on any Linux. The Jetson's own UARTs are
  `/dev/ttyTHS*`. A receiver wired to one of them needs `MTRTK_SOURCE=/dev/ttyTHS1` (or
  whichever it is) and `BAUD` set to the receiver's UART rate.
- Some JetPack images run ModemManager: install the udev rule (step 2).

## Native install (no Docker)

On Debian, Ubuntu or Raspberry Pi OS with systemd, from the clone, as the user who will run
mtrtk (not root; the script uses `sudo` where it must):

```bash
./install.sh --dry-run   # print every command that would change something, run none
./install.sh
```

It installs `uv` if missing and creates `.venv` (`uv sync --frozen --no-dev`, so run it on a
deployment clone: it removes the development tools from a development checkout). It builds
RTKLIB demo5 `convbin` and `rnx2rtkp` when they are missing (`--rtklib apt` or `skip` instead;
`convbin` already on `PATH` is kept, even stock 2.4.3). It builds the web UI when Node.js 20 or
newer is present (`--no-web` skips it). It creates `.env` from `.env.example` with
`DATA_DIR=<clone>/data` and a random `NTRIP_PASSWORD`, which it prints. An existing `.env` that
still says the container's `DATA_DIR=/data` is pointed at `<clone>/data`, and the old file is kept
as `.env.bak-<UTC>`. It installs the udev rule, adds you to `dialout`, installs and starts
`mtrtk.service`, and runs `mtrtk doctor`.

```bash
systemctl status mtrtk
journalctl -u mtrtk -f
./uninstall.sh            # removes the service and the udev rule; keeps data/, .env and .venv
```

The unit runs `.venv/bin/mtrtk run` as you, with `Restart=always` (the UI's restart button exits
the process and systemd brings it back with the new `.env`), and starts after
`tailscaled.service`. Node.js 20 is not in the Ubuntu 24.04 or Raspberry Pi OS archives; without
it there is no web UI (the API and the caster still run). Install it from
[nodejs.org](https://nodejs.org) and re-run `./install.sh`.
