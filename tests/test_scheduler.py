import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from labfoundry.compiler.plan import CompiledEvent
from labfoundry.runtime.clock import ScenarioClock
from labfoundry.runtime.scheduler import EventScheduler, UnknownActionError


def _event(sequence: int, when: datetime, action: str) -> CompiledEvent:
    return CompiledEvent(
        sequence=sequence,
        id=f"event-{sequence}",
        timestamp=when,
        action=action,
        parameters={},
    )


@pytest.mark.asyncio
async def test_scheduler_fails_before_waiting_when_handler_is_missing() -> None:
    start = datetime.now(UTC)
    scheduler = EventScheduler(ScenarioClock(start))
    event = _event(0, start + timedelta(hours=1), "missing.action")

    with pytest.raises(UnknownActionError, match=r"missing\.action"):
        await scheduler.run((event,))


@pytest.mark.asyncio
async def test_later_timestamp_is_not_blocked_by_long_running_handler() -> None:
    start = datetime.now(UTC)
    scheduler = EventScheduler(ScenarioClock(start))
    observed: list[str] = []

    async def slow(_event: CompiledEvent) -> None:
        observed.append("slow-start")
        await asyncio.sleep(0.05)
        observed.append("slow-end")

    async def fast(_event: CompiledEvent) -> None:
        observed.append("fast")

    scheduler.register("slow", slow)
    scheduler.register("fast", fast)

    await scheduler.run(
        (
            _event(0, start, "slow"),
            _event(1, start + timedelta(seconds=0.01), "fast"),
        )
    )

    assert observed.index("fast") < observed.index("slow-end")


@pytest.mark.asyncio
async def test_scheduler_warps_large_idle_gaps() -> None:
    start = datetime(2025, 9, 5, 8, 55, 0, tzinfo=UTC)
    clock = ScenarioClock(start, warp_gaps=True, settle_delay=0.05)
    scheduler = EventScheduler(clock)
    executed: list[str] = []

    async def record(event: CompiledEvent) -> None:
        executed.append(event.id)

    scheduler.register("ping", record)

    # Two events separated by 6 hours
    event1 = CompiledEvent(
        sequence=0,
        id="e1",
        timestamp=start,
        action="ping",
        parameters={},
    )
    event2 = CompiledEvent(
        sequence=1,
        id="e2",
        timestamp=start + timedelta(hours=6),
        action="ping",
        parameters={},
    )

    t0 = asyncio.get_running_loop().time()
    await scheduler.run((event1, event2))
    elapsed = asyncio.get_running_loop().time() - t0

    assert executed == ["e1", "e2"]
    # The 6 hour gap must execute in ~settle_delay, definitely < 1.0 second real time
    assert elapsed < 1.0
    assert len(clock.segments) == 2
    assert clock.segments[0].scenario_start == start
    assert clock.segments[1].scenario_start == event2.timestamp


@pytest.mark.asyncio
async def test_time_warp_hook_runs_before_the_event_batch() -> None:
    start = datetime(2025, 9, 5, 8, 55, tzinfo=UTC)
    observed: list[tuple[str, datetime]] = []

    async def on_time_warp(timestamp: datetime) -> None:
        observed.append(("clock", timestamp))

    async def record(event: CompiledEvent) -> None:
        observed.append(("event", event.timestamp))

    scheduler = EventScheduler(
        ScenarioClock(start, warp_gaps=True, settle_delay=0),
        on_time_warp=on_time_warp,
    )
    scheduler.register("ping", record)
    target = start + timedelta(hours=1)

    await scheduler.run((_event(0, target, "ping"),))

    assert observed == [("clock", target), ("event", target)]


@pytest.mark.asyncio
async def test_warped_scheduler_does_not_wrap_handler_errors() -> None:
    start = datetime.now(UTC)
    scheduler = EventScheduler(ScenarioClock(start, warp_gaps=True))

    async def fail(_event: CompiledEvent) -> None:
        raise UnknownActionError("handler failed")

    scheduler.register("fail", fail)

    with pytest.raises(UnknownActionError, match="handler failed"):
        await scheduler.run((_event(0, start, "fail"),))


@pytest.mark.asyncio
async def test_pending_connections_expire_before_clock_anchor_changes() -> None:
    start = datetime(2026, 3, 5, tzinfo=UTC)
    clock = ScenarioClock(start, warp_gaps=True, settle_delay=0)
    order = []

    async def expire(target):
        assert clock.now() < target
        assert len(clock.segments) == 1
        order.append("expire")

    async def synchronize(target):
        assert len(clock.segments) == 2
        order.append("synchronize")

    async def execute(event):
        order.append("event")

    scheduler = EventScheduler(clock, before_time_warp=expire, on_time_warp=synchronize)
    scheduler.register("ping", execute)
    await scheduler.run((_event(0, start + timedelta(days=1), "ping"),))
    assert order == ["expire", "synchronize", "event"]


@pytest.mark.asyncio
async def test_clock_does_not_warp_when_pending_connections_consume_the_gap() -> None:
    start = datetime(2026, 3, 5, tzinfo=UTC)
    elapsed = [0.0]
    clock = ScenarioClock(
        start,
        warp_gaps=True,
        settle_delay=0,
        monotonic=lambda: elapsed[0],
        time_fn=lambda: 1000 + elapsed[0],
    )
    clock.start_runtime()

    async def settle():
        elapsed[0] = 5.0

    assert not await clock.sleep_until(start + timedelta(seconds=5), before_warp=settle)
    assert len(clock.segments) == 1
    assert clock.now() == start + timedelta(seconds=5)
