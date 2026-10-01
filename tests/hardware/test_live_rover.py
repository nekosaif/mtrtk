"""Rover plumbing on the real F9P. Requires the receiver and about a minute.

Run: uv run pytest -m hardware tests/hardware/test_live_rover.py -s

The rover profile applies at 5 Hz, RTCM injected from an in-process NTRIP caster is seen by
the receiver (RXM-RTCM), and NMEA is served over TCP. The corrections are the recorded base
fixture, so they are stale: the receiver counts them (`count`) but does not use them
(`used == 0`), which is the expected outcome with one receiver. A real RTK fixed needs live
corrections from a second receiver - see docs/rover.md.

It reconfigures the receiver (RAM+BBR+Flash) with the rover profile. Port selection follows
`test_live_base.py`: `MTRTK_TEST_PORT`, else the first u-blox device found; with no receiver
the test skips.
"""

import asyncio
import os
from pathlib import Path

import pytest

from mtrtk.base.ntrip_caster import CasterConfig, NtripCaster
from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.frames import Framer, Proto
from mtrtk.core.router import TOPIC_RAW_RTCM
from mtrtk.core.source import find_ublox_port
from mtrtk.daemon import Daemon
from mtrtk.rover.sinks import TcpBroadcastSink

pytestmark = pytest.mark.hardware

BASE_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "f9p_hpg113_base_30s.ubx"


async def test_live_rover_plumbing(tmp_path: Path) -> None:
    port = os.environ.get("MTRTK_TEST_PORT") or find_ublox_port()
    if port is None:
        pytest.skip("no u-blox receiver found; set MTRTK_TEST_PORT to run this test")
    caster_bus = Bus()
    caster = NtripCaster(
        caster_bus,
        CasterConfig("MTRK", "rover", "pw", "MTRK", "BGD"),
        host="127.0.0.1",
        port=0,
    )
    await caster.start()
    frames = [f for f in Framer().feed(BASE_FIXTURE.read_bytes()) if f.proto is Proto.RTCM3]
    assert frames, "the base fixture carries no RTCM3"

    async def feed() -> None:
        # The recorded base corrections at ~1 Hz. Stale: the receiver counts but cannot use them.
        while True:
            for frame in frames[:20]:
                caster_bus.publish(TOPIC_RAW_RTCM, frame)
            await asyncio.sleep(1)

    feeder = asyncio.create_task(feed(), name="rtcm-feed")
    settings = Settings(
        _env_file=None,
        role="rover",
        mtrtk_source=port,
        data_dir=tmp_path,
        ntrip_url=f"ntrip://rover:pw@127.0.0.1:{caster.port}/MTRK",
        nmea_tcp_port=0,
        web_bind="127.0.0.1",
        web_port=0,
        web_allow_insecure=True,
        rover_nav_hz=5,
    )
    daemon = Daemon(settings)
    run_task = asyncio.create_task(daemon.run())
    try:
        for _ in range(600):
            await asyncio.sleep(0.1)
            if run_task.done():
                await run_task  # a startup failure must surface as itself, not as a timeout
            rover = daemon.rover
            if (
                rover is not None
                and rover.ntrip_client is not None
                and rover.ntrip_client.status.connected
                and daemon.controller.connected
                and daemon.store.state.epoch_count > 20
            ):
                break
        assert daemon.controller.connected and daemon.rover is not None
        await asyncio.sleep(15)
        st = daemon.store.state
        print(
            "epochs:",
            st.epoch_count,
            "rtcm_rx:",
            {k: v.model_dump() for k, v in st.rtk.rtcm_rx.items()},
            "corr_age:",
            st.rtk.corr_age_s,
            "receiver age code:",
            st.rtk.corr_age_receiver_s,
        )
        assert st.epoch_count > 50, "expected ~5 Hz epochs"
        assert st.rtk.rtcm_rx_total > 0, "receiver did not report any RXM-RTCM: injection broken"
        assert st.rtk.corr_age_s is not None and st.rtk.corr_age_s < 5
        nmea = daemon.rover.nmea
        assert nmea is not None
        tcp = next(s for s in nmea.sinks if isinstance(s, TcpBroadcastSink))
        reader, writer = await asyncio.open_connection("127.0.0.1", tcp.port)
        try:
            lines = [await asyncio.wait_for(reader.readline(), 5.0) for _ in range(10)]
        finally:
            writer.close()
        assert any(line.startswith(b"$GNGGA") for line in lines)
    finally:
        feeder.cancel()
        daemon.stop.set()
        await asyncio.wait_for(run_task, 30.0)
        await caster.stop()
