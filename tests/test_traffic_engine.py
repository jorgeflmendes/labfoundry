from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from labfoundry.compiler.plan import CompiledTrafficAction
from labfoundry.runtime.clock import ScenarioClock
from labfoundry.traffic.engine import TrafficEngine


def _action(distribution: str, **kwargs) -> CompiledTrafficAction:
    return CompiledTrafficAction(
        assignment="profile",
        node="ws",
        action="guest.exec",
        distribution=distribution,
        parameters={"script": "true"},
        seed=7,
        **kwargs,
    )


def test_interval_sampling_is_deterministic() -> None:
    import random

    uniform = _action("uniform", min_seconds=1.0, max_seconds=5.0)
    left = random.Random(7)
    right = random.Random(7)
    assert TrafficEngine._next_scenario_delay(uniform, left) == TrafficEngine._next_scenario_delay(
        uniform, right
    )

    fixed = _action("fixed", interval_seconds=3.0)
    assert TrafficEngine._next_scenario_delay(fixed, random.Random(1)) == 3.0

    exponential = _action("exponential", mean_seconds=4.0)
    assert TrafficEngine._next_scenario_delay(exponential, random.Random(1)) > 0

    for distribution, kwargs in (
        ("normal", {"mean_seconds": 4.0, "stddev_seconds": 1.0}),
        ("lognormal", {"median_seconds": 4.0, "sigma": 0.5}),
        ("gamma", {"shape": 2.0, "scale_seconds": 2.0}),
        ("weibull", {"shape": 1.5, "scale_seconds": 2.0}),
        ("pareto", {"shape": 2.5, "scale_seconds": 2.0}),
    ):
        action = _action(distribution, **kwargs)
        assert TrafficEngine._next_scenario_delay(action, random.Random(9)) > 0


@pytest.mark.asyncio
async def test_background_loop_stops_cleanly() -> None:
    clock = ScenarioClock(datetime.now(UTC), speed=1000.0)
    clock.start_runtime()
    executor = AsyncMock()
    engine = TrafficEngine(clock, executor)
    action = _action("fixed", interval_seconds=1.0)

    task = asyncio.create_task(engine.run_action(action))
    await asyncio.sleep(0.01)
    engine.stop()
    await asyncio.wait_for(task, timeout=1.0)

    assert executor.await_count >= 1
