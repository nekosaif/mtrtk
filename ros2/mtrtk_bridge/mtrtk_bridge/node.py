"""rclpy node: mtrtk WebSocket -> /mtrtk/* topics.

Two background threads feed it: one runs the WebSocket client (`websocket-client`, which is
`python3-websocket` on both Humble and Jazzy) and reconnects for as long as the node lives, the
other - only when `nmea_tcp` is set - reads the daemon's NMEA TCP stream. A 1 Hz timer on the
executor publishes a no-fix `NavSatFix` while no epoch arrives, so a consumer sees the loss
instead of the last good fix going quiet. Not `websockets`: Humble's python3-websockets 9.1
cannot connect at all on Python 3.10 (it passes `loop=` to `asyncio.Lock`).
All the field mapping is in `convert` and `link`, which need no ROS and are unit-tested.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from typing import Any

import rclpy
import websocket
from builtin_interfaces.msg import Time
from geometry_msgs.msg import TwistWithCovarianceStamped
from mtrtk_msgs.msg import RtkStatus, TimeMark
from nmea_msgs.msg import Sentence
from rcl_interfaces.msg import ParameterDescriptor
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from sensor_msgs.msg import Imu, NavSatFix, NavSatStatus, TimeReference
from std_msgs.msg import Float64

from mtrtk_bridge.convert import (
    SERVICE_ALL,
    EpochAccumulator,
    finite_twist,
    imu_fields,
    navsat_fix_fields,
    rtk_status_fields,
    time_mark_fields,
    twist_fields,
)
from mtrtk_bridge.link import (
    nmea_sentences,
    parse_host_port,
    redact_url,
    ws_connect_url,
    ws_error_reason,
    ws_headers,
)

# The daemon pings every 20 s, and a ping counts: this long with no frame at all is a dead link
# (a half-open TCP connection after a Wi-Fi drop), and the socket is redialled.
WS_TIMEOUT_S = 60.0
WS_CONNECT_TIMEOUT_S = 10.0
NMEA_CONNECT_TIMEOUT_S = 10.0
NMEA_IDLE_TIMEOUT_S = 30.0  # this long without a byte and the stream is redialled
THREAD_JOIN_S = 2.0
LOG_THROTTLE_S = 10.0
TIME_MARK_FIELDS = ("channel", "count", "time_base", "week", "tow", "time_valid", "acc_est_ns")


class MtrtkBridge(Node):
    def __init__(self) -> None:
        super().__init__("mtrtk_bridge")
        self.declare_parameter("ws_url", "ws://127.0.0.1:8080/ws")
        self.declare_parameter("token", "")
        self.declare_parameter("frame_id", "gnss")
        self.declare_parameter("namespace", "/mtrtk")
        self.declare_parameter("nmea_tcp", "")
        # `reconnect_s:=2` is an int: take it rather than fail on the parameter's type.
        seconds = ParameterDescriptor(dynamic_typing=True)
        self.declare_parameter("reconnect_s", 2.0, seconds)
        self.declare_parameter("stale_s", 5.0, seconds)
        ns = str(self.get_parameter("namespace").value).rstrip("/")
        self.frame_id = str(self.get_parameter("frame_id").value)
        self.reconnect_s = max(0.1, float(self.get_parameter("reconnect_s").value))
        self.pub_fix = self.create_publisher(NavSatFix, f"{ns}/fix", 10)
        self.pub_vel = self.create_publisher(TwistWithCovarianceStamped, f"{ns}/vel", 10)
        self.pub_time = self.create_publisher(TimeReference, f"{ns}/time_reference", 10)
        self.pub_rtk = self.create_publisher(RtkStatus, f"{ns}/rtk_status", 10)
        self.pub_mark = self.create_publisher(TimeMark, f"{ns}/time_mark", 50)
        self.pub_imu = self.create_publisher(Imu, f"{ns}/imu", 10)
        self.pub_heading = self.create_publisher(Float64, f"{ns}/heading", 10)
        self.pub_nmea = self.create_publisher(Sentence, f"{ns}/nmea", 50)

        self.acc = EpochAccumulator()
        self._lock = threading.Lock()  # the accumulator and the epoch clock
        self._stop = threading.Event()
        # Counted from start-up: a daemon that is down from the outset is a loss too.
        self._last_epoch = time.monotonic()
        self._stale = False
        self._ws: websocket.WebSocket | None = None
        self._nmea_sock: socket.socket | None = None
        self._threads = [threading.Thread(target=self._ws_thread, name="mtrtk-ws", daemon=True)]
        nmea = str(self.get_parameter("nmea_tcp").value)
        if nmea:
            try:
                host, port = parse_host_port(nmea)
            except ValueError as exc:
                self.get_logger().error(f"nmea_tcp: {exc}; not republishing NMEA")
            else:
                self._threads.append(
                    threading.Thread(
                        target=self._nmea_thread, args=(host, port), name="mtrtk-nmea", daemon=True
                    )
                )
        for thread in self._threads:
            thread.start()
        self.create_timer(1.0, self._watchdog)

    # ------------------------------------------------------------- publishing
    def _stamp(self, stamp: tuple[int, int] | None) -> Time:
        if stamp is None:
            return self.get_clock().now().to_msg()
        return Time(sec=stamp[0], nanosec=stamp[1])

    def _handle(self, raw: str | bytes) -> None:
        """One WebSocket message: fold it into the accumulator and publish what it completes."""
        try:
            msg: Any = json.loads(raw)
        except ValueError:
            self.get_logger().warning(
                "ignoring a WebSocket message that is not JSON",
                throttle_duration_sec=LOG_THROTTLE_S,
            )
            return
        if not isinstance(msg, dict):
            return
        with self._lock:
            if self._stop.is_set():
                return
            try:
                if self.acc.ingest(msg):
                    self._last_epoch = time.monotonic()
                    if self._stale:
                        self._stale = False
                        self.get_logger().info("epochs are arriving again")
                    self._publish_epoch()
                self._publish_time_marks()
            except Exception as exc:  # one bad message must not drop the connection
                self.get_logger().error(
                    f"could not publish a {msg.get('type')!r} message: {exc!r}",
                    throttle_duration_sec=LOG_THROTTLE_S,
                )

    def _publish_epoch(self) -> None:
        acc = self.acc
        if acc.pvt is None:
            return
        fix = navsat_fix_fields(acc.pvt)
        msg = NavSatFix()
        msg.header.stamp = self._stamp(fix["stamp"])
        msg.header.frame_id = self.frame_id
        msg.status = NavSatStatus(status=fix["status"], service=fix["service"])
        msg.latitude = fix["latitude"]
        msg.longitude = fix["longitude"]
        msg.altitude = fix["altitude"]
        msg.position_covariance = fix["position_covariance"]
        msg.position_covariance_type = fix["position_covariance_type"]
        self.pub_fix.publish(msg)

        tw = finite_twist(twist_fields(acc.pvt))
        vel = TwistWithCovarianceStamped()
        vel.header = msg.header
        linear = vel.twist.twist.linear
        linear.x, linear.y, linear.z = tw["linear"]
        vel.twist.covariance = tw["covariance"]
        self.pub_vel.publish(vel)

        if fix["stamp"] is not None:  # only a receiver time is a time reference
            tref = TimeReference()
            tref.header.stamp = self.get_clock().now().to_msg()
            tref.header.frame_id = self.frame_id
            tref.time_ref = msg.header.stamp
            tref.source = "gnss"
            self.pub_time.publish(tref)

        rtk = RtkStatus()
        rtk.header = msg.header
        for key, value in rtk_status_fields(acc.pvt, acc.rtk, acc.ntrip_connected).items():
            if key != "stamp":
                setattr(rtk, key, value)
        self.pub_rtk.publish(rtk)

        att = acc.fresh_attitude  # never a snapshot's (or an old epoch's) attitude restamped
        if att and att.get("heading_deg") is not None:
            q = imu_fields(att)
            imu = Imu()
            imu.header = msg.header
            o = imu.orientation
            o.x, o.y, o.z, o.w = q["orientation"]
            imu.orientation_covariance = q["orientation_covariance"]
            imu.angular_velocity_covariance[0] = -1.0  # orientation only
            imu.linear_acceleration_covariance[0] = -1.0
            self.pub_imu.publish(imu)
            self.pub_heading.publish(Float64(data=float(att["heading_deg"])))

    def _publish_time_marks(self) -> None:
        """As they arrive (they are `update`s, not part of an epoch), not one epoch later."""
        for mark in self.acc.pop_time_marks():
            m = time_mark_fields(mark)
            tm = TimeMark()
            tm.header.stamp = self._stamp(m["stamp"])
            tm.header.frame_id = self.frame_id
            for key in TIME_MARK_FIELDS:
                setattr(tm, key, m[key])
            self.pub_mark.publish(tm)

    def _watchdog(self) -> None:
        stale_s = float(self.get_parameter("stale_s").value)
        with self._lock:
            silent = time.monotonic() - self._last_epoch
            if silent <= stale_s:
                return
            if not self._stale:
                self._stale = True
                self.get_logger().warning(f"no epoch for {silent:.0f} s; publishing no fix")
        msg = NavSatFix()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.frame_id
        msg.status = NavSatStatus(status=NavSatStatus.STATUS_NO_FIX, service=SERVICE_ALL)
        msg.latitude = msg.longitude = msg.altitude = float("nan")
        msg.position_covariance_type = NavSatFix.COVARIANCE_TYPE_UNKNOWN
        self.pub_fix.publish(msg)

    # ------------------------------------------------------------- websocket thread
    def _ws_thread(self) -> None:
        url = ws_connect_url(str(self.get_parameter("ws_url").value))
        shown = redact_url(url)  # a token in the URL never reaches a log line
        try:
            headers = ws_headers(str(self.get_parameter("token").value))
        except ValueError as exc:
            self.get_logger().error(f"token: {exc}; connecting without it")
            headers = []
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(
                    url, timeout=WS_CONNECT_TIMEOUT_S, header=headers, enable_multithread=True
                )
                ws.settimeout(WS_TIMEOUT_S)
            except Exception as exc:  # refused, unreachable, rejected handshake: keep retrying
                reason = ws_error_reason(exc)
            else:
                reason = self._read_ws(ws, shown)
            if self._stop.is_set():
                return
            self.get_logger().warning(  # a daemon that stays down is logged every 10 s
                f"websocket disconnected: {reason}; retrying in {self.reconnect_s:g}s",
                throttle_duration_sec=LOG_THROTTLE_S,
            )
            self._stop.wait(self.reconnect_s)

    def _read_ws(self, ws: websocket.WebSocket, shown: str) -> str:
        """Read until the socket goes away; returns why it did."""
        self._ws = ws
        try:
            if self._stop.is_set():  # destroyed while the handshake was in flight
                return "shutting down"
            self.get_logger().info(f"connected to {shown}")
            while not self._stop.is_set():
                raw = ws.recv()  # answers the daemon's pings itself
                if raw == "":  # a close frame
                    return "closed by the daemon"
                self._handle(raw)
            return "shutting down"
        except Exception as exc:
            return ws_error_reason(exc)
        finally:
            self._ws = None
            with contextlib.suppress(Exception):
                ws.close(timeout=1)

    # ------------------------------------------------------------- NMEA thread
    def _nmea_thread(self, host: str, port: int) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection((host, port), timeout=NMEA_CONNECT_TIMEOUT_S) as sock:
                    sock.settimeout(NMEA_IDLE_TIMEOUT_S)
                    self._nmea_sock = sock
                    self.get_logger().info(f"reading NMEA from {host}:{port}")
                    with sock.makefile("rb") as fh:
                        for text in nmea_sentences(fh.readline):
                            if self._stop.is_set():
                                break
                            self._publish_nmea(text)
                reason = "closed by the daemon"
            except (OSError, ValueError) as exc:  # ValueError: the file closed under us
                reason = str(exc) or type(exc).__name__
            finally:
                self._nmea_sock = None
            if self._stop.is_set():
                return
            self.get_logger().warning(
                f"NMEA TCP {host}:{port} disconnected: {reason}; retrying in {self.reconnect_s:g}s",
                throttle_duration_sec=LOG_THROTTLE_S,
            )
            self._stop.wait(self.reconnect_s)

    def _publish_nmea(self, text: str) -> None:
        s = Sentence()
        s.header.stamp = self.get_clock().now().to_msg()
        s.header.frame_id = self.frame_id
        s.sentence = text
        self.pub_nmea.publish(s)

    # ------------------------------------------------------------- shutdown
    def destroy_node(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.abort()  # wakes the blocked recv
        sock = self._nmea_sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked read
        for thread in self._threads:
            thread.join(THREAD_JOIN_S)
        super().destroy_node()


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = MtrtkBridge()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
