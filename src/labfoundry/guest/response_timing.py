"""Delivery schedules for recorded response bodies; no transport packet synthesis."""

from __future__ import annotations

import math
import random
import re
from difflib import SequenceMatcher
from itertools import accumulate
from typing import Any


def simulated_schedule(
    size: int,
    *,
    seed: str,
    first_byte: tuple[float, float],
    duration: tuple[float, float],
) -> list[list[Any]]:
    """Pace application bytes with seeded delays, independently of TCP framing."""
    if size < 0:
        raise ValueError("response size must be non-negative")
    for lower, upper in (first_byte, duration):
        if not (math.isfinite(lower) and math.isfinite(upper) and 0 < lower <= upper <= 300):
            raise ValueError("response timing bounds must satisfy 0 < min <= max <= 300")
    rng = random.Random(seed)
    start = rng.uniform(*first_byte)
    if not size:
        return [[start, 0]]
    span = rng.uniform(*duration)
    count = min(size, max(2, int(span / rng.uniform(0.12, 0.3))))
    weights = [rng.lognormvariate(0, 0.55) for _ in range(count)]
    pauses = [rng.uniform(0.5, 1.5) for _ in range(count - 1)]
    total_weight, total_pause = sum(weights), sum(pauses)
    weight, pause, end = 0.0, 0.0, 0
    schedule = []
    for index, value in enumerate(weights):
        weight += value
        end = max(end + 1, min(size - count + index + 1, round(size * weight / total_weight)))
        schedule.append([start + span * pause / total_pause if total_pause else start, end])
        if index < len(pauses):
            pause += pauses[index]
    validate_schedule(schedule, size)
    return schedule


def validate_schedule(schedule: list[list[Any]], size: int) -> None:
    """Reject non-monotonic timing, invalid offsets and incomplete response coverage."""
    previous_time, previous_end = 0.0, 0
    if not schedule:
        raise ValueError("response delivery schedule is empty")
    for elapsed, end in schedule:
        if (
            not isinstance(elapsed, (int, float))
            or not math.isfinite(elapsed)
            or elapsed < previous_time
            or not isinstance(end, int)
            or end < previous_end
            or end > size
        ):
            raise ValueError("invalid response delivery schedule")
        previous_time, previous_end = elapsed, end
    if previous_end != size:
        raise ValueError("response delivery schedule does not cover the body")


def remap_schedule(
    before: bytes,
    after: bytes,
    schedule: list[list[Any]],
) -> list[list[Any]]:
    """Release replacements when their original bytes would have arrived."""
    validate_schedule(schedule, len(before))
    if before == after:
        return [list(step) for step in schedule]
    tokens_before = re.findall(rb"[A-Za-z0-9_.:-]+|[^A-Za-z0-9_.:-]+", before)
    tokens_after = re.findall(rb"[A-Za-z0-9_.:-]+|[^A-Za-z0-9_.:-]+", after)
    old_offsets = [0, *accumulate(map(len, tokens_before))]
    new_offsets = [0, *accumulate(map(len, tokens_after))]
    edits: list[tuple[str, int, int, int, int]] = []
    for operation, first, last, new_first, new_last in SequenceMatcher(
        None,
        tokens_before,
        tokens_after,
    ).get_opcodes():
        start, stop = old_offsets[first], old_offsets[last]
        new_start, new_stop = new_offsets[new_first], new_offsets[new_last]
        # Bound byte-level comparisons; repeated HTML otherwise makes them quadratic.
        if operation == "replace" and max(stop - start, new_stop - new_start) <= 4096:
            edits.extend(
                (kind, start + a, start + b, new_start + c, new_start + d)
                for kind, a, b, c, d in SequenceMatcher(
                    None,
                    before[start:stop],
                    after[new_start:new_stop],
                    autojunk=False,
                ).get_opcodes()
            )
        else:
            edits.append((operation, start, stop, new_start, new_stop))
    result = []
    for elapsed, end in schedule:
        mapped = 0
        for tag, start, stop, new_start, new_stop in edits:
            if stop <= end:
                mapped = new_stop
            elif start < end:
                mapped = new_start + (end - start if tag == "equal" else 0)
                break
            else:
                break
        result.append([elapsed, mapped])
    validate_schedule(result, len(after))
    return result


def available_bytes(schedule: list[list[Any]], elapsed: float) -> int:
    """Return the cumulative body bytes due by the elapsed request time."""
    available = 0
    for due, end in schedule:
        if due > elapsed:
            break
        available = int(end)
    return available
