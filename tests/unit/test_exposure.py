import asyncio

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
