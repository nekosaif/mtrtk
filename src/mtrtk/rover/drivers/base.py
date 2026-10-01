"""What every rover receiver driver provides. The F9P driver lives in ublox.py; INS drivers come
later."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DriverCapabilities:
    accepts_rtcm: bool
    raw_gnss_log: bool
    attitude: bool
    imu: bool
    sats: bool
    spectrum: bool


class RoverDriver(Protocol):
    name: str

    @property
    def capabilities(self) -> DriverCapabilities:
        """Read-only: a class attribute (u-blox) or a property that follows what the unit
        streams (INS drivers) both satisfy it."""
        ...

    async def inject_rtcm(self, data: bytes) -> None:
        """Forward one CRC-valid RTCM3 frame to the receiver (no-op when it cannot accept
        corrections). Must not raise for a receiver that has gone away: drop and count."""
        ...
