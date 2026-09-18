"""Blocking recorder that dumps the raw receiver byte stream to a file (fixtures, replay)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import serial

from mtrtk.core.frames import Framer
from mtrtk.core.source import find_ublox_port


@dataclass
class RecordStats:
    bytes: int
    frames: dict[str, int]
    garbage_bytes: int
    checksum_errors: int


def record_stream(port: str, baud: int, seconds: float, out_path: Path) -> RecordStats:
    if port == "auto":
        found = find_ublox_port()
        if found is None:
            raise RuntimeError("no u-blox receiver found; pass --port explicitly")
        port = found
    framer = Framer()
    total = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    with serial.Serial(port, baud, timeout=0.5) as ser, out_path.open("wb") as fh:
        while time.monotonic() < deadline:
            data = ser.read(4096)
            if not data:
                continue
            fh.write(data)
            total += len(data)
            framer.feed(data)
    return RecordStats(
        total, dict(framer.stats.frames), framer.stats.garbage_bytes, framer.stats.checksum_errors
    )
