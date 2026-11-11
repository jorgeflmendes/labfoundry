"""Scenario YAML loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from labfoundry.errors import ScenarioValidationError
from labfoundry.schema.scenario import ScenarioSpec


def load_scenario(path: Path) -> ScenarioSpec:
    """Load YAML from disk and validate it as a strict ScenarioSpec."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ScenarioValidationError(f"Cannot read scenario {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioValidationError("scenario root must be a mapping")
    try:
        return ScenarioSpec.model_validate(raw)
    except ValidationError as exc:
        raise ScenarioValidationError(str(exc)) from exc
