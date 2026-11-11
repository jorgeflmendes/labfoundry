"""Bounded page loading and seeded interaction timing for guest browsers."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable, Mapping
from typing import Any

PAGE_STATE = """(()=>{
  const resources=performance.getEntriesByType('resource');
  const visible=image=>image.getBoundingClientRect().top<innerHeight
    &&image.getBoundingClientRect().bottom>0;
  const images=Array.from(document.images).filter(visible);
  return {ready:document.readyState==='complete'&&document.fonts.status==='loaded'
      &&images.every(image=>image.complete),
    resources:resources.length,
    failure_statuses:resources.filter(item=>item.responseStatus>=400).reduce((all,item)=>{
      (all[item.name]??=[]).push(item.responseStatus);return all;
    },{}),
    failures:[...resources.filter(item=>item.responseStatus>=400
      &&['script','link','css','img'].includes(item.initiatorType)).map(item=>item.name),
      ...images.filter(image=>image.complete&&image.currentSrc&&!image.naturalWidth)
        .map(image=>image.currentSrc)]};
})()"""


def wait_for_page(
    evaluate: Callable[[str], object],
    timeout: float = 15.0,
    quiet: float = 0.8,
    *,
    expected_http_errors: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Wait for document rendering and resource quiescence, or fail within the budget."""
    deadline = time.monotonic() + timeout
    last_count = None
    stable_since = time.monotonic()
    while True:
        state = evaluate(PAGE_STATE)
        now = time.monotonic()
        if not isinstance(state, dict):
            raise RuntimeError("browser did not report page readiness")
        if state["resources"] != last_count or not state["ready"]:
            stable_since = now
            last_count = state["resources"]
        if state["ready"] and now - stable_since >= quiet:
            failures = [
                url
                for url in state["failures"]
                if not (
                    expected_http_errors
                    and url in expected_http_errors
                    and set(state.get("failure_statuses", {}).get(url, ()))
                    == {expected_http_errors[url]}
                )
            ]
            if failures:
                raise RuntimeError("page resources failed: " + ", ".join(failures))
            return state
        if now >= deadline:
            raise TimeoutError("page did not finish rendering before its load deadline")
        time.sleep(min(0.2, deadline - now))


def reading_delay(rng: random.Random, minimum: float, maximum: float) -> float:
    """Sample a bounded reading pause using the caller's random state."""
    if not all(math.isfinite(value) for value in (minimum, maximum)) or not (
        0 <= minimum <= maximum <= 300
    ):
        raise ValueError("reading times must satisfy 0 <= minimum <= maximum <= 300")
    return min(
        maximum,
        max(
            minimum,
            rng.lognormvariate(
                math.log(max(0.1, (minimum + maximum) / 2)),
                0.35,
            ),
        ),
    )
