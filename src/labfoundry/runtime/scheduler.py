"""Deterministic semantic event scheduler."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime

from labfoundry.compiler.plan import CompiledEvent
from labfoundry.errors import LabFoundryError
from labfoundry.runtime.clock import ScenarioClock

EventHandler = Callable[[CompiledEvent], Awaitable[None]]
BatchHook = Callable[[datetime], Awaitable[None]]


class UnknownActionError(LabFoundryError):
    """Raised when a compiled event has no registered semantic action handler."""

    pass


class EventScheduler:
    """Schedule compiled semantic events against a ScenarioClock."""

    def __init__(
        self,
        clock: ScenarioClock,
        *,
        on_time_warp: BatchHook | None = None,
        before_time_warp: BatchHook | None = None,
    ) -> None:
        self.clock = clock
        self._on_time_warp = on_time_warp
        self._before_time_warp = before_time_warp
        self._handlers: dict[str, EventHandler] = {}

    def register(self, action: str, handler: EventHandler) -> None:
        """Register a handler for one semantic event action name."""
        if action in self._handlers:
            raise ValueError(f"handler already registered for {action!r}")
        self._handlers[action] = handler

    def validate_handlers(self, events: tuple[CompiledEvent, ...]) -> None:
        """Fail before runtime when the plan references unregistered actions."""
        missing = sorted({event.action for event in events} - self._handlers.keys())
        if missing:
            raise UnknownActionError(
                "no runtime handler registered for: " + ", ".join(repr(item) for item in missing)
            )

    async def run(self, events: tuple[CompiledEvent, ...]) -> None:
        """Run events in order within each timestamp batch; overlap batches unless warping."""
        self.validate_handlers(events)
        if not self.clock.running:
            self.clock.start_runtime()

        batches: dict[datetime, list[CompiledEvent]] = defaultdict(list)
        for event in events:
            batches[event.timestamp].append(event)

        if self.clock.warp_gaps:
            for timestamp in sorted(batches):
                ordered = tuple(sorted(batches[timestamp], key=lambda item: item.sequence))
                await self._run_batch(timestamp, ordered)
            return

        async with asyncio.TaskGroup() as tasks:
            for timestamp in sorted(batches):
                ordered = tuple(sorted(batches[timestamp], key=lambda item: item.sequence))
                tasks.create_task(self._run_batch(timestamp, ordered))

    async def _run_batch(
        self,
        timestamp: datetime,
        events: tuple[CompiledEvent, ...],
    ) -> None:
        async def before_warp() -> None:
            if self._before_time_warp is not None:
                await self._before_time_warp(timestamp)

        warped = await self.clock.sleep_until(timestamp, before_warp=before_warp)
        if warped and self._on_time_warp is not None:
            await self._on_time_warp(timestamp)
        for event in events:
            await self._handlers[event.action](event)
