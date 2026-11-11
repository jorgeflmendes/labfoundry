from __future__ import annotations

import tomllib
from pathlib import Path

import labfoundry

ROOT = Path(__file__).parents[1]


def test_distribution_metadata_matches_runtime_version() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["version"] == labfoundry.__version__
    assert project["requires-python"] == ">=3.12"
    assert "Programming Language :: Python :: 3.12" in project["classifiers"]
    assert "Programming Language :: Python :: 3.13" in project["classifiers"]
    assert project["keywords"]
