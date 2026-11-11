import random

import pytest

from labfoundry.guest import browser_timing


def test_reading_delay_is_seeded_bounded_and_variable() -> None:
    first, repeat = random.Random(19), random.Random(19)
    values = [browser_timing.reading_delay(first, 5, 12) for _ in range(20)]
    assert values == [browser_timing.reading_delay(repeat, 5, 12) for _ in range(20)]
    assert all(5 <= value <= 12 for value in values)
    assert len(set(values)) > 5


@pytest.mark.parametrize("minimum,maximum", [(5, 2), (-1, 4), (0, float("nan"))])
def test_invalid_reading_budget(minimum, maximum) -> None:
    with pytest.raises(ValueError):
        browser_timing.reading_delay(random.Random(0), minimum, maximum)


def test_page_waits_for_rendering_and_a_quiet_interval(monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(browser_timing.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        browser_timing.time,
        "sleep",
        lambda delay: clock.__setitem__(0, clock[0] + delay),
    )
    states = iter(
        [
            {"ready": False, "resources": 0, "failures": []},
            {"ready": True, "resources": 1, "failures": []},
            {"ready": True, "resources": 2, "failures": []},
        ]
    )
    final = {"ready": True, "resources": 2, "failures": []}
    assert browser_timing.wait_for_page(lambda _: next(states, final), quiet=0.5) == final
    assert clock[0] >= 0.9


def test_page_failure_is_not_reported_as_a_completed_visit() -> None:
    with pytest.raises(RuntimeError, match=r"site\.css"):
        browser_timing.wait_for_page(
            lambda _: {
                "ready": True,
                "resources": 1,
                "failures": ["https://example.test/site.css"],
            },
            quiet=0,
        )


def test_page_load_has_a_deadline() -> None:
    with pytest.raises(TimeoutError, match="deadline"):
        browser_timing.wait_for_page(
            lambda _: {
                "ready": False,
                "resources": 0,
                "failures": [],
            },
            timeout=0,
        )


@pytest.mark.parametrize("statuses", [[], [404], [404, 404], [500], [500, 404]])
def test_expected_resource_error_requires_matching_status(statuses) -> None:
    url = "https://example.test/optional.svg"
    state = {"ready": True, "resources": 1, "failures": [url], "failure_statuses": {url: statuses}}
    if statuses and set(statuses) == {404}:
        assert (
            browser_timing.wait_for_page(
                lambda _: state,
                quiet=0,
                expected_http_errors={url: 404},
            )
            == state
        )
    else:
        with pytest.raises(RuntimeError, match=r"optional\.svg"):
            browser_timing.wait_for_page(
                lambda _: state,
                quiet=0,
                expected_http_errors={url: 404},
            )


def test_expected_resource_error_does_not_hide_another_failure() -> None:
    with pytest.raises(RuntimeError, match=r"site\.css"):
        browser_timing.wait_for_page(
            lambda _: {
                "ready": True,
                "resources": 2,
                "failures": ["https://example.test/icon.svg", "https://example.test/site.css"],
                "failure_statuses": {"https://example.test/icon.svg": [404]},
            },
            quiet=0,
            expected_http_errors={"https://example.test/icon.svg": 404},
        )
