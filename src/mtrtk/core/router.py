"""Turns incoming bytes into frames and publishes them on bus topics."""

from __future__ import annotations

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Frame, Framer, Proto

TOPIC_RAW_UBX = "raw.ubx"
TOPIC_RAW_RTCM = "raw.rtcm"
TOPIC_RAW_NMEA = "raw.nmea"


def topics_for(frame: Frame) -> tuple[str, ...]:
    """The raw-protocol topic plus the per-message topic this frame publishes on."""
    if frame.proto is Proto.UBX:
        return (TOPIC_RAW_UBX, f"ubx.{frame.identity}")
    if frame.proto is Proto.RTCM3:
        return (TOPIC_RAW_RTCM, f"rtcm.{frame.rtcm_type}")
    return (TOPIC_RAW_NMEA, f"nmea.{frame.identity}")


class Router:
    """Feeds bytes through a `Framer` and publishes every complete frame on the bus."""

    def __init__(self, bus: Bus, framer: Framer | None = None) -> None:
        self.bus = bus
        self.framer = framer or Framer()
        self.bytes_in = 0

    def feed(self, data: bytes) -> list[Frame]:
        self.bytes_in += len(data)
        frames = self.framer.feed(data)
        for frame in frames:
            for topic in topics_for(frame):
                self.bus.publish(topic, frame)
        return frames
