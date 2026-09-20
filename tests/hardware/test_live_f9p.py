"""Live tests against the attached ZED-F9P. Run with: uv run pytest -m hardware -q

They reconfigure the receiver (RAM+BBR+Flash) with the base profile - approved by the owner.
"""

import asyncio
import os

import pytest

from mtrtk.config import Settings
from mtrtk.core.bus import Bus
from mtrtk.core.link import UbxLink
from mtrtk.core.receiver import ReceiverController
from mtrtk.core.router import Router
from mtrtk.core.source import SerialSource, find_ublox_port
from mtrtk.core.ubx_config import base_profile

pytestmark = pytest.mark.hardware

PORT = os.environ.get("MTRTK_TEST_PORT") or find_ublox_port() or "/dev/ttyACM0"


@pytest.fixture
async def live():
    bus = Bus()
    source = SerialSource(PORT, 115200)
    await source.open()
    router = Router(bus)

    async def pump() -> None:
        while True:
            router.feed(await source.read())

    pump_task = asyncio.create_task(pump())
    link = UbxLink(source, bus)
    await link.start()
    try:
        yield bus, source, link
    finally:
        pump_task.cancel()
        await link.stop()
        await source.close()


async def test_probe_reports_f9p_firmware(live) -> None:
    bus, source, link = live
    ctrl = ReceiverController(bus, lambda: source, None)
    caps = await ctrl.probe(link)
    assert caps.module == "ZED-F9P"
    assert caps.protver.startswith("27.") or caps.protver.startswith("32.")
    print("capabilities:", caps)


async def test_base_profile_applies_and_verifies(live) -> None:
    bus, source, link = live
    settings = Settings(_env_file=None, ntrip_password="x")
    ctrl = ReceiverController(bus, lambda: source, base_profile(settings))
    caps = await ctrl.configure(link, first=True)
    print("unsupported optional features:", sorted(caps.unsupported))
    assert await ctrl.verify(link, base_profile(settings)) == {}
