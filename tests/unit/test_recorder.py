from pathlib import Path

import pytest

from mtrtk.core import recorder
from ubxtest import ubx_frame


class FakeSerial:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __enter__(self) -> "FakeSerial":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


def test_record_stream_writes_bytes_and_counts_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pvt = ubx_frame(0x01, 0x07, b"\x00" * 92)
    chunks = [pvt[:40], pvt[40:] + b"\x00\x00", pvt]
    monkeypatch.setattr(recorder.serial, "Serial", lambda port, baud, timeout: FakeSerial(chunks))
    # Patching time.monotonic hits the shared module, so Framer._emit consumes ticks too:
    # hand out cheap ticks inside the deadline, then 10.0 forever to end the loop. The
    # trailing ticks make read() return b"" (the timeout path) before the deadline passes.
    ticks = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    monkeypatch.setattr(recorder.time, "monotonic", lambda: next(ticks, 10.0))
    out = tmp_path / "sub" / "x.ubx"
    stats = recorder.record_stream("/dev/fake", 115200, 5.0, out)
    assert out.read_bytes() == pvt + b"\x00\x00" + pvt
    assert stats.bytes == len(pvt) * 2 + 2
    assert stats.frames["ubx"] == 2
    assert stats.garbage_bytes == 2


def test_record_stream_requires_a_receiver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recorder, "find_ublox_port", lambda: None)
    with pytest.raises(RuntimeError, match="no u-blox receiver"):
        recorder.record_stream("auto", 115200, 1.0, tmp_path / "x.ubx")
