import asyncio

from mtrtk.core.bus import Bus, Policy


async def test_exact_prefix_and_all_patterns() -> None:
    bus = Bus()
    exact = bus.subscribe("ubx.NAV-PVT")
    prefix = bus.subscribe("ubx.*")
    everything = bus.subscribe("*")
    bus.publish("ubx.NAV-PVT", 1)
    bus.publish("ubx.NAV-SAT", 2)
    bus.publish("raw.rtcm", 3)
    assert exact.queue.qsize() == 1
    assert prefix.queue.qsize() == 2
    assert everything.queue.qsize() == 3


async def test_drop_oldest_keeps_newest() -> None:
    bus = Bus()
    sub = bus.subscribe("t", maxsize=2)
    for i in range(5):
        bus.publish("t", i)
    assert sub.dropped == 3
    assert [(await sub.get())[1] for _ in range(2)] == [3, 4]


async def test_unbounded_never_drops_but_counts_high_water() -> None:
    bus = Bus()
    sub = bus.subscribe("t", policy=Policy.UNBOUNDED, high_water=3)
    for i in range(5):
        bus.publish("t", i)
    assert sub.dropped == 0
    assert sub.queue.qsize() == 5
    assert sub.high_water_hits == 3


async def test_async_iteration_ends_on_close() -> None:
    bus = Bus()
    sub = bus.subscribe("t")
    bus.publish("t", "x")
    bus.publish("t", "y")
    sub.close()
    assert [item async for _, item in sub] == ["x", "y"]


async def test_close_wakes_waiting_consumer() -> None:
    bus = Bus()
    sub = bus.subscribe("t")

    async def consume() -> list[object]:
        return [item async for _, item in sub]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    sub.close()
    assert await asyncio.wait_for(task, 1.0) == []


async def test_unsubscribe_stops_delivery() -> None:
    bus = Bus()
    sub = bus.subscribe("t")
    bus.unsubscribe(sub)
    bus.publish("t", 1)
    assert bus.subscriber_count == 0


async def test_publish_after_close_is_ignored() -> None:
    bus = Bus()
    sub = bus.subscribe("t", maxsize=1)
    sub.close()
    bus.publish("t", 1)
    assert [item async for _, item in sub] == []
