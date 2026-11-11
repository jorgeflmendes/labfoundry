import pytest
from pydantic import ValidationError

from labfoundry.schema.scenario import ScenarioSpec


def test_overlapping_segments_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(
            {
                "version": "0.2",
                "scenario": {"id": "x", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {
                        "a": {"subnet": "10.0.0.0/24"},
                        "b": {"subnet": "10.0.0.128/25"},
                    },
                    "nodes": {},
                },
            }
        )


def test_clock_requires_timezone() -> None:
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(
            {
                "version": "0.2",
                "scenario": {"id": "x", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00"},
                "network": {"segments": {}, "nodes": {}},
            }
        )


def test_legacy_dsl_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ScenarioSpec.model_validate(
            {
                "version": "0.1",
                "scenario": {"id": "x", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {"segments": {}, "nodes": {}},
            }
        )


def test_interval_fields_must_match_distribution() -> None:
    from labfoundry.schema.scenario import IntervalSpec

    with pytest.raises(ValidationError):
        IntervalSpec.model_validate({"distribution": "fixed", "value": "1s", "mean": "2s"})

    with pytest.raises(ValidationError):
        IntervalSpec.model_validate({"distribution": "uniform", "min": "1s"})

    with pytest.raises(ValidationError):
        IntervalSpec.model_validate({"distribution": "gamma", "shape": 2.0, "mean": "1s"})


def test_virtualbox_interfaces_use_one_port_per_adapter() -> None:
    from labfoundry.schema.scenario import NodeSpec

    with pytest.raises(ValidationError):
        NodeSpec.model_validate(
            {
                "template": "VBox",
                "template_type": "virtualbox",
                "interfaces": {
                    "eth0": {
                        "segment": "lan",
                        "adapter": 0,
                        "port": 1,
                    }
                },
            }
        )


def test_static_interface_prefix_must_match_segment() -> None:
    with pytest.raises(ValidationError, match="prefix /16 does not match"):
        ScenarioSpec.model_validate(
            {
                "version": "0.2",
                "scenario": {"id": "x", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {"lan": {"subnet": "10.0.0.0/24"}},
                    "nodes": {
                        "ws": {
                            "template": "vbox",
                            "interfaces": {"eth0": {"segment": "lan", "ip": "10.0.0.10/16"}},
                        }
                    },
                },
            }
        )


def test_gateway_cannot_be_network_or_broadcast_address() -> None:
    with pytest.raises(ValidationError, match="network or broadcast"):
        ScenarioSpec.model_validate(
            {
                "version": "0.2",
                "scenario": {"id": "x", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {"lan": {"subnet": "10.0.0.0/24"}},
                    "nodes": {
                        "ws": {
                            "template": "vbox",
                            "interfaces": {
                                "eth0": {
                                    "segment": "lan",
                                    "ip": "10.0.0.10/24",
                                    "gateway": "10.0.0.0",
                                }
                            },
                        }
                    },
                },
            }
        )
