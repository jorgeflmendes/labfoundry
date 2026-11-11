"""Deterministic background-traffic timing engine.

The engine intentionally delegates actual packet/application generation to a
TrafficExecutor, keeping scenario semantics independent of SSH/agent details.
"""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable

from labfoundry.compiler.plan import CompiledTrafficAction
from labfoundry.runtime.clock import ScenarioClock

TrafficExecutor = Callable[[CompiledTrafficAction], Awaitable[None]]


class TrafficEngine:
    """Execute recurring semantic traffic actions with deterministic interval sampling."""

    def __init__(self, clock: ScenarioClock, executor: TrafficExecutor) -> None:
        self.clock = clock
        self.executor = executor
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Request graceful termination of all background action loops."""
        self._stop.set()

    async def run_action(self, action: CompiledTrafficAction) -> None:
        """Run one compiled recurring action until the engine is stopped."""
        rng = random.Random(action.seed)
        while not self._stop.is_set():
            delay = self._next_scenario_delay(action, rng) / self.clock.speed
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                await self.executor(action)

    @staticmethod
    def _next_scenario_delay(action: CompiledTrafficAction, rng: random.Random) -> float:
        if action.distribution == "fixed":
            assert action.interval_seconds is not None
            return action.interval_seconds
        if action.distribution == "uniform":
            assert action.min_seconds is not None and action.max_seconds is not None
            return rng.uniform(action.min_seconds, action.max_seconds)
        if action.distribution == "exponential":
            assert action.mean_seconds is not None
            value = rng.expovariate(1.0 / action.mean_seconds)
            return max(value, math.ulp(action.mean_seconds))
        if action.distribution == "normal":
            assert action.mean_seconds is not None and action.stddev_seconds is not None
            return max(
                rng.gauss(action.mean_seconds, action.stddev_seconds),
                math.ulp(action.mean_seconds),
            )
        if action.distribution == "lognormal":
            assert action.median_seconds is not None and action.sigma is not None
            return rng.lognormvariate(math.log(action.median_seconds), action.sigma)
        assert action.scale_seconds is not None and action.shape is not None
        if action.distribution == "gamma":
            return rng.gammavariate(action.shape, action.scale_seconds)
        if action.distribution == "weibull":
            return rng.weibullvariate(action.scale_seconds, action.shape)
        assert action.distribution == "pareto"
        return action.scale_seconds * rng.paretovariate(action.shape)
