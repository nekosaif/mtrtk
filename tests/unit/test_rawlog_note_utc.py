"""`RawLogWriter.note_utc`: an INS driver sets the rotation clock when no NAV-PVT flows."""

from datetime import UTC, datetime
from pathlib import Path

from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer
from mtrtk.rawlog.writer import RawLogWriter, Sidecar, log_path, sidecar_path
from ubxtest import ubx_frame

RAWX = ubx_frame(0x02, 0x15, b"\x11" * 16)


def test_note_utc_drives_rotation_without_nav_pvt(tmp_path: Path) -> None:
    w = RawLogWriter(Bus(), tmp_path, "MTRK", ["RXM-RAWX"], role="rover")
    (rawx,) = Framer().feed(RAWX)
    w.handle(rawx)
    assert w.current_path is None  # buffered: no clock yet
    w.note_utc(datetime(2026, 10, 1, 9, 59, 58, tzinfo=UTC))
    w.handle(rawx)
    w.note_utc(datetime(2026, 10, 1, 10, 0, 0, tzinfo=UTC))
    w.handle(rawx)
    w.close()
    p09 = log_path(tmp_path, "MTRK", datetime(2026, 10, 1, 9, tzinfo=UTC))
    p10 = log_path(tmp_path, "MTRK", datetime(2026, 10, 1, 10, tzinfo=UTC))
    assert p09.read_bytes() == RAWX * 2 and p10.read_bytes() == RAWX
    sc = Sidecar.load(sidecar_path(p09))
    assert sc.time_source == "receiver" and sc.start_utc == "2026-10-01T09:59:58+00:00"
    assert w.receiver_utc == datetime(2026, 10, 1, 10, tzinfo=UTC)


def test_note_utc_treats_a_naive_datetime_as_utc(tmp_path: Path) -> None:
    w = RawLogWriter(Bus(), tmp_path, "MTRK", ["RXM-RAWX"], role="rover")
    w.note_utc(datetime(2026, 10, 1, 9, 30))
    assert w.receiver_utc == datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
