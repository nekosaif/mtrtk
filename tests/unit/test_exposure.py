import asyncio
import threading
from pathlib import Path

import pytest

from mtrtk.core import exposure


def test_resolve_bind_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: "100.100.50.10")
    assert exposure.resolve_bind("tailscale") == "100.100.50.10"
    assert exposure.resolve_bind("lan") == "0.0.0.0"
    assert exposure.resolve_bind("all") == "0.0.0.0"
    assert exposure.resolve_bind("192.168.1.20") == "192.168.1.20"
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: None)
    assert exposure.resolve_bind("tailscale") is None


def test_resolve_bind_rejects_a_bind_that_is_neither_mode_nor_address() -> None:
    with pytest.raises(ValueError, match="bind"):
        exposure.resolve_bind("tailscale0")


async def test_wait_for_bind_retries_until_available(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([None, None, "100.100.50.10"])
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: next(answers))
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(exposure.asyncio, "sleep", fake_sleep)
    bind = await exposure.wait_for_bind("tailscale", asyncio.Event(), retry_s=5.0)
    assert bind == "100.100.50.10"
    assert sleeps == [5.0, 5.0]


async def test_wait_for_bind_gives_up_on_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: None)
    stop = asyncio.Event()

    async def fake_sleep(d: float) -> None:
        stop.set()

    monkeypatch.setattr(exposure.asyncio, "sleep", fake_sleep)
    assert await exposure.wait_for_bind("tailscale", stop, retry_s=0.01) is None


async def test_wait_for_bind_wakes_up_as_soon_as_stop_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown must not wait out the retry interval."""
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: None)
    stop = asyncio.Event()
    waiter = asyncio.create_task(exposure.wait_for_bind("tailscale", stop, retry_s=30.0))
    await asyncio.sleep(0)
    stop.set()
    async with asyncio.timeout(1.0):
        assert await waiter is None


async def test_wait_for_bind_logs_the_wait_at_most_once_a_minute(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answers = iter([None] * 24 + ["100.100.50.10"])
    monkeypatch.setattr(exposure, "tailscale_ipv4", lambda: next(answers))

    async def fake_sleep(d: float) -> None:
        return None

    monkeypatch.setattr(exposure.asyncio, "sleep", fake_sleep)
    with caplog.at_level("WARNING"):
        assert await exposure.wait_for_bind("tailscale", asyncio.Event(), retry_s=5.0) is not None
    assert len([r for r in caplog.records if "tailscale" in r.getMessage()]) == 2


async def test_wait_for_bind_returns_any_for_lan() -> None:
    assert await exposure.wait_for_bind("lan", asyncio.Event()) == "0.0.0.0"


# ------------------------------------------------------------- rebind on an address change


class FakeTailnet:
    """A `tailscale_ipv4` whose answer the test moves, like tailscaled getting a new netmap."""

    def __init__(self, address: str | None) -> None:
        self.address = address
        self.reads = 0

    def __call__(self) -> str | None:
        self.reads += 1
        return self.address


async def test_wait_for_rebind_returns_the_new_tailnet_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Pi on 2026-10-03: it bound the cached 100.93.95.104, then got 100.100.10.100."""
    tailnet = FakeTailnet("100.93.95.104")
    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    stop = asyncio.Event()
    waiter = asyncio.create_task(
        exposure.wait_for_rebind("tailscale", "100.93.95.104", stop, check_s=0.01)
    )
    await asyncio.sleep(0.05)
    assert not waiter.done()  # the same address: nothing to do
    tailnet.address = "100.100.10.100"
    async with asyncio.timeout(1.0):
        assert await waiter == "100.100.10.100"


async def test_wait_for_rebind_sits_out_a_tailnet_without_an_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tailscale0 briefly empty (tailscaled restarting): no address to move to, never 0.0.0.0."""
    tailnet = FakeTailnet(None)
    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    stop = asyncio.Event()
    waiter = asyncio.create_task(
        exposure.wait_for_rebind("tailscale", "100.93.95.104", stop, check_s=0.01)
    )
    for _ in range(100):
        if tailnet.reads >= 5:
            break
        await asyncio.sleep(0.01)
    assert tailnet.reads >= 5 and not waiter.done()
    tailnet.address = "100.93.95.104"  # the same address back: the socket on it works again
    await asyncio.sleep(0.05)
    assert not waiter.done()
    stop.set()
    async with asyncio.timeout(1.0):
        assert await waiter is None


async def test_wait_for_rebind_never_moves_a_fixed_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    """lan/all/an explicit IP name the same address for ever: only `stop` ends the wait."""
    tailnet = FakeTailnet("100.100.10.100")
    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    for mode, host in (("lan", "0.0.0.0"), ("all", "0.0.0.0"), ("192.168.1.20", "192.168.1.20")):
        stop = asyncio.Event()
        waiter = asyncio.create_task(exposure.wait_for_rebind(mode, host, stop, check_s=0.01))
        await asyncio.sleep(0.05)
        assert not waiter.done(), mode
        stop.set()
        async with asyncio.timeout(1.0):
            assert await waiter is None
    assert tailnet.reads == 0


async def test_wait_for_rebind_wakes_up_as_soon_as_stop_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(exposure, "tailscale_ipv4", FakeTailnet("100.93.95.104"))
    stop = asyncio.Event()
    waiter = asyncio.create_task(
        exposure.wait_for_rebind("tailscale", "100.93.95.104", stop, check_s=30.0)
    )
    await asyncio.sleep(0)
    stop.set()
    async with asyncio.timeout(1.0):
        assert await waiter is None


async def test_wait_for_rebind_survives_an_interface_read_that_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([OSError("netlink hiccup"), "100.100.10.100"])

    def flaky() -> str | None:
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(exposure, "tailscale_ipv4", flaky)
    async with asyncio.timeout(1.0):
        new = await exposure.wait_for_rebind(
            "tailscale", "100.93.95.104", asyncio.Event(), check_s=0.01
        )
    assert new == "100.100.10.100"


async def test_wait_for_rebind_reads_the_interface_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil walks every interface (dozens of veths on a Docker host) every 5 s for the life of
    the process; the loop carrying the RTCM fan-out must not be the thread that does it."""
    loop_thread = threading.get_ident()
    readers: list[int] = []

    def tailnet() -> str | None:
        readers.append(threading.get_ident())
        return "100.100.10.100"

    monkeypatch.setattr(exposure, "tailscale_ipv4", tailnet)
    async with asyncio.timeout(1.0):
        new = await exposure.wait_for_rebind(
            "tailscale", "100.93.95.104", asyncio.Event(), check_s=0.01
        )
    assert new == "100.100.10.100"
    assert readers and loop_thread not in readers


def test_the_default_rebind_check_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The brief's bounded time: a moved address is noticed within 5-10 s, not "eventually"."""
    from mtrtk.config import Settings
    from mtrtk.core.source import NoReceiverSource
    from mtrtk.daemon import Daemon

    assert 5.0 <= exposure.REBIND_CHECK_S <= 10.0
    monkeypatch.setenv("NTRIP_PASSWORD", "pw")
    settings = Settings(_env_file=None, role="base", data_dir=tmp_path)
    daemon = Daemon(settings, source_factory=NoReceiverSource)
    assert daemon.rebind_check_s == exposure.REBIND_CHECK_S
