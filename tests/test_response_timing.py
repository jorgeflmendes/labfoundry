from difflib import SequenceMatcher
from itertools import pairwise

import pytest

from labfoundry.guest import response_timing
from labfoundry.guest.response_timing import (
    available_bytes,
    remap_schedule,
    simulated_schedule,
    validate_schedule,
)


@pytest.mark.parametrize("size", [0, 1, 2, 8000, 100000])
def test_simulated_delivery_is_seeded_and_bounded(size) -> None:
    options = {"first_byte": (2, 4), "duration": (8, 16)}
    schedule = simulated_schedule(size, seed="request-1", **options)
    assert schedule == simulated_schedule(size, seed="request-1", **options)
    assert schedule != simulated_schedule(size, seed="request-2", **options)
    validate_schedule(schedule, size)
    assert 2 <= schedule[0][0] <= 4
    assert available_bytes(schedule, 1.99) == 0
    if size > 1:
        assert 8 <= schedule[-1][0] - schedule[0][0] <= 16
    if size > 100:
        assert len({b[1] - a[1] for a, b in pairwise(schedule)}) > 1


@pytest.mark.parametrize("bounds", [(0, 1), (3, 2), (1, float("nan")), (1, 301)])
def test_simulated_delivery_rejects_invalid_bounds(bounds) -> None:
    with pytest.raises(ValueError, match="timing bounds"):
        simulated_schedule(10, seed="x", first_byte=bounds, duration=(1, 2))


def test_sanitized_replacement_waits_for_original_bytes() -> None:
    schedule = [[0.2, 2], [0.4, 5], [1.3, 8], [1.5, 10]]
    mapped = remap_schedule(b"abSECRETcd", b"abIDcd", schedule)
    assert mapped == [[0.2, 2], [0.4, 2], [1.3, 4], [1.5, 6]]
    assert available_bytes(mapped, 0.1) == 0
    assert available_bytes(mapped, 0.6) == 2
    assert available_bytes(mapped, 1.5) == 6


@pytest.mark.parametrize(
    "schedule",
    [
        [],
        [[-1, 3]],
        [[float("nan"), 3]],
        [[1, 4]],
        [[1, 2]],
        [[2, 1], [1, 3]],
        [[1, 2], [2, 1], [3, 3]],
        [[1, 3.0]],
    ],
)
def test_rejects_invalid_delivery_schedule(schedule) -> None:
    with pytest.raises(ValueError):
        validate_schedule(schedule, 3)


def test_remapping_removes_secret_without_releasing_trailing_bytes_early() -> None:
    assert remap_schedule(b"abcSECRETdef", b"abcdef", [[1, 5], [2, 9], [3, 12]]) == [
        [1, 3],
        [2, 3],
        [3, 6],
    ]


def test_large_repetitive_body_never_uses_unbounded_byte_matching(monkeypatch) -> None:
    def matcher(_junk, before, after, **kwargs):
        if isinstance(before, bytes):
            assert max(len(before), len(after)) <= 4096
        return SequenceMatcher(_junk, before, after, **kwargs)

    monkeypatch.setattr(response_timing, "SequenceMatcher", matcher)
    before = b'<template data-id="original">' + b"content " * 10000 + b"</template>"
    after = before.replace(b"original", b"replacement")
    schedule = [[1, 500], [2, len(before) // 2], [3, len(before)]]
    mapped = remap_schedule(before, after, schedule)
    assert mapped == [[1, 503], [2, len(before) // 2 + 3], [3, len(after)]]


def test_large_changed_token_is_released_only_after_its_source() -> None:
    before, after = b"a" * 10000, b"b" * 10000
    assert remap_schedule(before, after, [[1, 100], [2, 10000]]) == [[1, 0], [2, 10000]]
