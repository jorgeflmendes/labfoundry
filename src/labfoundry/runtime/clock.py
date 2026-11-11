"""Scenario time mapped onto monotonic runtime time."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class TimeWarpSegment:
    """Map raw epoch timestamps to a scenario anchor at the segment's speed."""

    raw_start: Decimal
    scenario_start: datetime
    speed: float = 1.0


class ScenarioClock:
    """Map monotonic runtime time to timezone-aware scenario time at a configurable speed."""

    def __init__(
        self,
        start: datetime,
        speed: float = 1.0,
        *,
        warp_gaps: bool = False,
        settle_delay: float = 3.0,
        time_fn: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("scenario clock start must be timezone-aware")
        if speed <= 0:
            raise ValueError("scenario clock speed must be positive")
        self.start = start
        self.speed = speed
        self.warp_gaps = warp_gaps
        self.settle_delay = settle_delay
        self._time = time_fn
        self._monotonic = monotonic
        self._monotonic_start: float | None = None
        self._scenario_anchor: datetime = start
        self._raw_anchor: Decimal | None = None
        self.segments: list[TimeWarpSegment] = []

    @property
    def running(self) -> bool:
        """Whether the runtime anchor has been initialized."""
        return self._monotonic_start is not None

    def start_runtime(self) -> None:
        """Anchor scenario time to the current monotonic runtime clock."""
        if self._monotonic_start is not None:
            raise RuntimeError("scenario clock is already running")
        now_mono = self._monotonic()
        now_epoch = Decimal(str(self._time()))
        self._monotonic_start = now_mono
        self._scenario_anchor = self.start
        self._raw_anchor = now_epoch
        self.segments.append(
            TimeWarpSegment(
                raw_start=now_epoch,
                scenario_start=self.start,
                speed=self.speed,
            )
        )

    def now(self) -> datetime:
        """Return the current scenario timestamp."""
        if self._monotonic_start is None:
            return self.start
        elapsed = self._monotonic() - self._monotonic_start
        return self._scenario_anchor + timedelta(seconds=elapsed * self.speed)

    def runtime_delay_until(self, target: datetime) -> float:
        """Convert a target scenario timestamp to remaining real runtime seconds."""
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("target scenario time must be timezone-aware")
        scenario_seconds = (target - self.now()).total_seconds()
        return max(0.0, scenario_seconds / self.speed)

    async def sleep_until(
        self,
        target: datetime,
        *,
        before_warp: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Wait for a target, returning whether a new warp segment was created.

        Before a warp, wait the settle delay and invoke the optional hook. Recheck the
        target after the hook because settling connections may consume the whole gap.
        """
        delay = self.runtime_delay_until(target)
        if not self.warp_gaps or delay <= self.settle_delay:
            await asyncio.sleep(delay)
            return False

        await asyncio.sleep(self.settle_delay)
        if before_warp is not None:
            await before_warp()
            delay = self.runtime_delay_until(target)
            if delay <= self.settle_delay:
                await asyncio.sleep(delay)
                return False
        now_mono = self._monotonic()
        now_epoch = Decimal(str(self._time()))
        self._monotonic_start = now_mono
        self._scenario_anchor = target
        self._raw_anchor = now_epoch
        self.segments.append(
            TimeWarpSegment(
                raw_start=now_epoch,
                scenario_start=target,
                speed=self.speed,
            )
        )
        return True
