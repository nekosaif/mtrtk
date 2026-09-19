"""Live base-station smoke test. Requires the F9P and ~2 minutes.

Run: uv run pytest -m hardware tests/hardware/test_live_base.py -s

It reconfigures the receiver (RAM+BBR+Flash) with the base profile and starts a short
survey-in - approved by the owner. Port selection follows `test_live_f9p.py`:
`MTRTK_TEST_PORT`, else the first u-blox device found; with no receiver the test skips.
"""

import asyncio
import base64
import os
from pathlib import Path

import pytest

from mtrtk.config import Settings
from mtrtk.core.frames import Framer, Proto
from mtrtk.core.source import find_ublox_port
from mtrtk.daemon import Daemon
from mtrtk.rawlog.index import list_logs

pytestmark = pytest.mark.hardware

PORT = os.environ.get("MTRTK_TEST_PORT") or find_ublox_port()


async def test_live_base_serves_rtcm_and_logs(tmp_path: Path) -> None:
    if PORT is None:
        pytest.skip("no u-blox receiver found; set MTRTK_TEST_PORT to run this test")
    settings = Settings(
        _env_file=None,
        role="base",
        mtrtk_source=PORT,
        data_dir=tmp_path,
        ntrip_bind="127.0.0.1",
        ntrip_port=0,
        ntrip_user="rover",
        ntrip_password="pw",
        base_mode="survey-in",
        svin_min_duration_s=60,
        svin_acc_limit_m=5.0,
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    try:
        for _ in range(300):
            await asyncio.sleep(0.1)
            if run_task.done():
                await run_task  # a startup failure must surface as itself, not as a timeout
            if daemon.caster is not None and daemon.controller.connected:
                break
        assert daemon.caster is not None
        auth = base64.b64encode(b"rover:pw").decode()
        reader, writer = await asyncio.open_connection("127.0.0.1", daemon.caster.port)
        writer.write(
            f"GET /MTRK HTTP/1.1\r\nHost: x\r\nNtrip-Version: Ntrip/2.0\r\n"
            f"User-Agent: NTRIP pytest\r\nAuthorization: Basic {auth}\r\n\r\n".encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
        assert head.startswith(b"HTTP/1.1 200 OK")
        deadline = asyncio.get_running_loop().time() + 90
        types: set[int] = set()
        framer = Framer()
        while asyncio.get_running_loop().time() < deadline:
            chunk = await asyncio.wait_for(reader.read(65536), 10.0)
            # Strip the v2 chunk framing crudely: this can corrupt an RTCM frame that happens
            # to contain 0D 0A, and the framer's CRC check then drops it. Fine for a smoke test.
            for f in framer.feed(chunk.replace(b"\r\n", b"")):
                if f.proto is Proto.RTCM3:
                    types.add(f.rtcm_type)
            if 1005 in types and daemon.store.state.survey_in.valid:
                break
        print("RTCM types seen:", sorted(types), "survey-in:", daemon.store.state.survey_in)
        assert types & {1077, 1074}, "no MSM observations received"
        writer.close()
    finally:
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
    logs = list_logs(tmp_path)
    assert logs and logs[-1].msg_counts.get("RXM-RAWX", 0) > 30
