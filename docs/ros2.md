# ROS 2 bridge

`mtrtk_bridge` publishes the rover state on ROS 2 topics. It talks to the daemon's WebSocket, so the core stays ROS-free and the bridge can run on another machine on the tailnet.

Two packages live under `ros2/`: `mtrtk_msgs` (the `RtkStatus` and `TimeMark` messages) and `mtrtk_bridge` (an rclpy node). Both build on **Humble** (Ubuntu 22.04, JetPack 6) and **Jazzy** (Ubuntu 24.04). The node's only non-ROS dependency is **websocket-client** (`import websocket`, apt `python3-websocket`). It does not use `websockets`: Humble's 9.1 cannot connect on Python 3.10.

## Run with Docker

```bash
ROS_DISTRO=humble docker compose --profile ros2 up -d       # or ROS_DISTRO=jazzy
ros2 topic echo /mtrtk/fix                                  # from any node in the same ROS_DOMAIN_ID (host network)
```

The `mtrtk-ros2` service builds `ros2/Dockerfile` for `ROS_DISTRO` and runs `ros2 launch mtrtk_bridge bridge.launch.py` with host networking, so DDS discovery works like a native node's.

The image's Fast DDS profile (`ros2/fastdds.xml`, set through `FASTRTPS_DEFAULT_PROFILES_FILE` and `FASTDDS_DEFAULT_PROFILES_FILE`) sends over UDPv4 only. Fast DDS's default sends to nodes on the same host through shared memory in `/dev/shm`, and the container's `/dev/shm` is private. A native node, or a node in another container, would then see the topics but get no data. With UDP only, consumers on the robot get the data whatever their own IPC setup, with no `ipc: host`.

Set these in `.env` (compose reads them; the daemon ignores them):

| Variable | Default | What it does |
|---|---|---|
| `ROS_DISTRO` | `humble` | `humble` or `jazzy`: the base image and the image tag `ghcr.io/nekosaif/mtrtk-ros2:<distro>` |
| `ROS_DOMAIN_ID` | `0` | DDS domain; nodes only see each other inside one domain |
| `MTRTK_WS_URL` | `ws://127.0.0.1:8080/ws` | The daemon's WebSocket. It overrides the parameter file's `ws_url` |

Things to check:

- **Which address the daemon listens on.** `ws://127.0.0.1:8080/ws` only works when `WEB_BIND` is `lan`, `all` or `127.0.0.1`. With the default `WEB_BIND=tailscale` the daemon listens only on the tailnet address, so use `MTRTK_WS_URL=ws://<tailscale-ip>:8080/ws`.
- **The token.** When `WEB_PASSWORD` is set, the WebSocket handshake needs the web token. That token is derived from the password and is not the password itself. Get it once:
  ```bash
  curl -s -X POST http://<host>:8080/api/login -H 'content-type: application/json' -d '{"password":"…"}'
  ```
  The answer is `{"token":"<hex>"}`. Then either add it to the URL (`MTRTK_WS_URL=ws://<host>:8080/ws?token=<hex>`), or give the node `token:=<hex>`, which it sends as an `Authorization: Bearer` header. Without the token, the bridge logs `handshake refused with HTTP 403 (is WEB_PASSWORD set? give the bridge its token)`, publishes a no-fix `NavSatFix`, and keeps retrying. The token is redacted (`token=***`) in the log.
- **Bridge on another machine.** The service `depends_on` the daemon, so on a robot that only runs the bridge, start it without the daemon: `docker compose --profile ros2 up -d --no-deps mtrtk-ros2`, with `MTRTK_WS_URL` pointing at the daemon's host.
- **Stopping.** `docker compose --profile ros2 down` stops it. The image's stop signal is SIGINT (`ros2 launch` ignores SIGTERM as PID 1), so `docker stop` shuts the node down cleanly within a second.

To build the image without compose:

```bash
docker build -f ros2/Dockerfile --build-arg ROS_DISTRO=jazzy -t mtrtk-ros2:jazzy .   # from the repository root
docker run --rm --network host -e ROS_DOMAIN_ID=0 -e MTRTK_WS_URL=ws://<host>:8080/ws mtrtk-ros2:jazzy
```

## Run natively (colcon)

```bash
sudo apt install ros-$ROS_DISTRO-nmea-msgs python3-websocket   # with ROS 2 Humble or Jazzy installed and sourced
cd ros2 && colcon build && source install/setup.bash
ros2 launch mtrtk_bridge bridge.launch.py ws_url:=ws://<rover-ip>:8080/ws
```

`ros2 launch` takes `ws_url:=` first, then `$MTRTK_WS_URL`, then the parameter file's `ws_url`. To use another parameter file, pass `params:=<file.yaml>`. `ros2 run mtrtk_bridge mtrtk_bridge --ros-args -p ws_url:=…` also works. Under `ros2 run`, Ctrl-C reaches the node, but `kill -INT <pid of ros2 run>` does not: signal the process group, or use `ros2 launch`.

## Topics

All topics sit under `namespace` (default `/mtrtk`). Every message's `header.frame_id` is `frame_id` (default `gnss`).

| Topic | Type | Notes |
|---|---|---|
| `/mtrtk/fix` | `sensor_msgs/NavSatFix` | status −1/0/1/2 = none/GNSS/DGNSS/RTK; diagonal covariance from hAcc, vAcc |
| `/mtrtk/vel` | `geometry_msgs/TwistWithCovarianceStamped` | ENU velocity |
| `/mtrtk/time_reference` | `sensor_msgs/TimeReference` | receiver UTC |
| `/mtrtk/rtk_status` | `mtrtk_msgs/RtkStatus` | carrier solution, correction age, baseline, RTCM counters |
| `/mtrtk/time_mark` | `mtrtk_msgs/TimeMark` | EXTINT pulses (camera triggers) |
| `/mtrtk/imu`, `/mtrtk/heading` | `sensor_msgs/Imu`, `std_msgs/Float64` | only with an INS driver: see below |
| `/mtrtk/nmea` | `nmea_msgs/Sentence` | when `nmea_tcp` is set |

Details:

- **`/mtrtk/fix`.** One message per receiver epoch, stamped with the receiver's UTC.
  - `status` is −1 (`STATUS_NO_FIX`) without a usable fix, 0 for a plain GNSS fix, 1 (`SBAS_FIX`) for a differential fix, and 2 (`GBAS_FIX`) for RTK float or fixed. `/mtrtk/rtk_status` tells float from fixed.
  - `altitude` is above the WGS 84 ellipsoid, as `NavSatFix` defines it.
  - The covariance is `[hAcc²/2, hAcc²/2, vAcc²]` on the diagonal, type `COVARIANCE_TYPE_DIAGONAL_KNOWN`.
  - When no epoch has arrived for `stale_s`, because the daemon is down, unreachable or refusing the token, the node publishes a `STATUS_NO_FIX` fix with NaN coordinates once a second. A consumer sees the loss instead of a frozen position.
- **`/mtrtk/vel`.** East, north and up in `linear`, with sAcc² as each axis's variance. The angular rate is not measured: its variances are large (1e6), never 0.
- **`/mtrtk/rtk_status`.** `carr_soln` (`CARR_NONE`/`CARR_FLOAT`/`CARR_FIXED`), `fix_type`, `diff_soln`, `num_sv`, accuracies and DOPs, and `corr_age` (seconds since the last RTCM frame was injected). It also carries the base-to-rover `baseline` and `rel_pos_n/e/d`/`rel_pos_heading` (from NAV-RELPOSNED), `ref_station_id`, the RTCM counters and `ntrip_connected`. A value the receiver does not report is NaN.
- **`/mtrtk/time_mark`.** One message per EXTINT edge, as soon as the daemon reports it. `header.stamp` is the pulse time in UTC when it is known (`time_valid`). `week`/`tow` are TIM-TM2's rising edge in `time_base`.
- **`/mtrtk/imu`, `/mtrtk/heading`.** With an INS driver (`ROVER_DRIVER=sbg_ellipse|vectornav`) the daemon sends the attitude in the `ins` topic's per-epoch bundle (`{ins, imu, attitude}`), which the bridge asks for. The two topics publish on each epoch whose attitude has a heading; a snapshot's attitude is never restamped as current. A u-blox rover sends no attitude, so they stay silent there:
  - The `Imu` orientation is the body (FLU) in ENU, per REP 103, with no rates or accelerations.
  - `heading` is degrees clockwise from true north.
- **`/mtrtk/nmea`.** The sentences the daemon's NMEA TCP server sends (`NMEA_TCP_PORT`, default 10110), one per message. Sentences with a bad checksum, and overlong lines, are dropped.

The message definitions are in `ros2/mtrtk_msgs/msg/`.

## Parameters

Set them in `config/bridge.yaml`, with `params:=<file>`, or with `-p name:=value`. All of them are read once, at start-up, and are read-only afterwards.

| Parameter | Default | What it does |
|---|---|---|
| `ws_url` | `ws://127.0.0.1:8080/ws` | The daemon's WebSocket (`ws://` or `wss://`). The node adds `topics=pvt,rtk,ins` itself. A `?token=` in it is used and redacted in logs |
| `token` | `""` | Web token, sent as `Authorization: Bearer`; empty = none (or the URL's) |
| `frame_id` | `gnss` | `header.frame_id` of every message: the antenna's frame |
| `namespace` | `/mtrtk` | Prefix of every topic |
| `nmea_tcp` | `""` | `host:port` of an NMEA TCP stream to republish on `nmea`, e.g. `127.0.0.1:10110`; empty = off |
| `reconnect_s` | `2.0` | Seconds between reconnect attempts (WebSocket and NMEA) |
| `stale_s` | `5.0` | Seconds without an epoch before `fix` reports no fix (at least 0.5) |

With `robot_localization`, feed `/mtrtk/fix` to `navsat_transform_node`. On an INS rover, `/mtrtk/imu` (orientation, with heading) can join it.

## Troubleshooting

- **`ros2 topic echo` shows nothing.** Check that both sides use the same `ROS_DOMAIN_ID`, and the same RMW. Do not mix a Humble CLI with a Jazzy node in one domain: their type hashes differ, and the CLI fails with errors such as `unknown tag 'rclpy.type_hash.TypeHash'`.
- **`ros2 topic list` shows `/mtrtk/*`, but `ros2 topic echo` prints nothing.** Discovery works and the data does not arrive. This is Fast DDS shared memory across a private `/dev/shm`. The image avoids it with its UDP-only profile, with two exceptions:
  - **`ROS_LOCALHOST_ONLY=1` (Humble) or `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST` (Jazzy) on the bridge.** In that mode rmw_fastrtps sets its own transports, shared memory included, and ignores the profile. A consumer outside the container then gets no data, even with `--ipc host` when it runs as another user than the container's root. Do not set these on the bridge; use a `ROS_DOMAIN_ID` of its own instead.
  - **A custom image or an overridden `FASTRTPS_DEFAULT_PROFILES_FILE`.** Use a UDP-only profile like `ros2/fastdds.xml`, or give the bridge and its consumers `ipc: host` and the same user.

  The same applies between your own ROS containers.
- **`/mtrtk/fix` keeps `status: -1` with NaN coordinates.** The bridge has no epochs. Its log says why: `Connection refused` (wrong host/port or `WEB_BIND`), `HTTP 403` (token), or `no epoch for N s` while connected (the daemon has no receiver data).
- **`Invalid close opcode.` on Humble when the daemon restarts.** This is websocket-client 1.2.3 misreading the close frame. The bridge reconnects as usual, so it is harmless.
