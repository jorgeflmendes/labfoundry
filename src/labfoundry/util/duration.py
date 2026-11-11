"""Small deterministic duration parser used by the DSL."""

from __future__ import annotations

import re
from datetime import timedelta

from labfoundry.errors import ScenarioValidationError

_DURATION_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h|d)$", re.IGNORECASE)
_MULTIPLIERS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}


def parse_duration(value: str) -> timedelta:
    """Parse a compact duration such as ``250ms``, ``2s``, ``15m`` or ``3h``."""
    match = _DURATION_RE.fullmatch(value.strip())
    if match is None:
        raise ScenarioValidationError(f"Invalid duration {value!r}; expected <number><ms|s|m|h|d>")
    seconds = float(match.group("value")) * _MULTIPLIERS[match.group("unit").lower()]
    return timedelta(seconds=seconds)
