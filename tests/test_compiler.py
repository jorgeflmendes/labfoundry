from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.errors import CompilationError
from labfoundry.groundtruth.manifest import canonical_plan_hash
from labfoundry.schema.loader import load_scenario


def test_compiler_is_deterministic(scenario_path: Path) -> None:
    spec = load_scenario(scenario_path)
    compiler = ScenarioCompiler()
    first = compiler.compile(spec)
    second = compiler.compile(spec)

    assert first == second
    assert canonical_plan_hash(first) == canonical_plan_hash(second)
    assert first.aliases["selected"] == ("ws-03",)
    assert first.events[0].from_ == first.aliases["selected"][0]
    assert first.events[1].timestamp.isoformat() == "2025-04-17T09:17:34+01:00"


def test_seed_override_changes_procedural_plan(scenario_path: Path) -> None:
    spec = load_scenario(scenario_path)
    compiler = ScenarioCompiler()
    first = compiler.compile(spec, seed=1)
    second = compiler.compile(spec, seed=2)

    first_addresses = [iface.address for node in first.nodes for iface in node.interfaces]
    second_addresses = [iface.address for node in second.nodes for iface in node.interfaces]
    assert first_addresses != second_addresses


def test_capture_resolves_to_compiled_link(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    capture = {item.name: item for item in plan.captures}
    assert capture["users-perimeter"].link == "gateway:users--users"
    assert capture["wan-perimeter"].link == "gateway:wan--simulated_internet"


def test_pcapng_capture_combines_multiple_interfaces(scenario_data: dict[str, Any]) -> None:
    raw = scenario_data
    raw["captures"] = [
        {
            "name": "perimeter",
            "format": "pcapng",
            "timestamp_policy": "preserve",
            "metadata": {"comment": "combined perimeter view"},
            "endpoints": [
                {"name": "users", "node": "gateway", "interface": "users"},
                {"name": "wan", "node": "gateway", "interface": "wan"},
            ],
        }
    ]
    from labfoundry.schema.scenario import ScenarioSpec

    plan = ScenarioCompiler().compile(ScenarioSpec.model_validate(raw))

    capture = plan.captures[0]
    assert capture.output_format == "pcapng"
    assert capture.timestamp_policy == "preserve"
    assert [item.name for item in capture.endpoints] == ["users", "wan"]


def test_pcapng_capture_expands_all_segment_links(scenario_data: dict[str, Any]) -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    raw = scenario_data
    raw["captures"] = [
        {"name": "network", "format": "pcapng", "segments": ["users", "simulated_internet"]}
    ]
    plan = ScenarioCompiler().compile(ScenarioSpec.model_validate(raw))

    assert {endpoint.link for endpoint in plan.captures[0].endpoints} == {
        link.name for link in plan.links if link.segment in {"users", "simulated_internet"}
    }
    assert len({endpoint.name for endpoint in plan.captures[0].endpoints}) == len(
        plan.captures[0].endpoints
    )


def test_capture_rejects_unknown_or_empty_segment(scenario_data: dict[str, Any]) -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    raw = scenario_data
    raw["captures"] = [{"name": "network", "format": "pcapng", "segments": ["missing"]}]
    with pytest.raises(CompilationError, match="unknown segment"):
        ScenarioCompiler().compile(ScenarioSpec.model_validate(raw))

    raw["network"]["segments"]["empty"] = {"subnet": "10.255.0.0/24"}
    raw["captures"][0]["segments"] = ["empty"]
    with pytest.raises(CompilationError, match="has no links"):
        ScenarioCompiler().compile(ScenarioSpec.model_validate(raw))


def test_event_condition_requires_an_earlier_guest_exec_result() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "conditional", "seed": 1},
            "clock": {"start": "2025-01-01T00:00:00+00:00"},
            "network": {"segments": {}, "nodes": {"ws": {"template": "ws"}}},
            "events": [
                {
                    "id": "probe",
                    "at": "00:00:01",
                    "action": "guest.exec",
                    "from": "ws",
                    "parameters": {"script": "true", "check": False},
                },
                {
                    "id": "follow-up",
                    "after": "1s",
                    "action": "guest.exec",
                    "from": "ws",
                    "parameters": {"script": "true"},
                    "when": {"event": "probe", "exit_code": 0},
                },
            ],
        }
    )

    plan = ScenarioCompiler().compile(spec)

    assert plan.events[1].when is not None
    assert plan.events[1].when.event == "probe"


def test_event_after_duration_is_rejected() -> None:
    import pytest

    from labfoundry.errors import CompilationError
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "short", "seed": 1},
            "clock": {
                "start": "2025-01-01T00:00:00+00:00",
                "duration": "10s",
            },
            "network": {"segments": {}, "nodes": {}},
            "events": [
                {
                    "at": "00:00:11",
                    "action": "snapshot.create",
                    "parameters": {"name": "late"},
                }
            ],
        }
    )

    with pytest.raises(CompilationError, match=r"after clock\.duration"):
        ScenarioCompiler().compile(spec)


def test_provision_actions_expand_node_groups(
    tmp_path: Path, scenario_data: dict[str, Any]
) -> None:
    import yaml

    raw = scenario_data
    raw["network"]["nodes"]["ws"]["guest_profile"] = "linux-vbox"
    raw["provision"] = [
        {
            "nodes": "ws",
            "action": "guest.exec",
            "parameters": {"script": "echo provisioned"},
        }
    ]
    scenario = tmp_path / "scenario.yaml"
    scenario.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    plan = ScenarioCompiler().compile(load_scenario(scenario))

    ws_nodes = [node.name for node in plan.nodes if node.group == "ws"]
    assert [item.node for item in plan.provision] == ws_nodes
    assert all(item.action == "guest.exec" for item in plan.provision)


def test_warp_gaps_with_duration_is_rejected() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "invalid-clock", "seed": 1},
            "clock": {
                "start": "2025-01-01T00:00:00+00:00",
                "duration": "10s",
                "warp_gaps": True,
            },
            "network": {"segments": {}, "nodes": {}},
        }
    )

    with pytest.raises(CompilationError, match=r"warp_gaps.*clock\.duration"):
        ScenarioCompiler().compile(spec)


def test_warp_gaps_with_accelerated_bursts_is_rejected() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "invalid-clock", "seed": 1},
            "clock": {"start": "2025-01-01T00:00:00+00:00", "speed": 10, "warp_gaps": True},
            "network": {"segments": {}, "nodes": {}},
        }
    )

    with pytest.raises(CompilationError, match=r"warp_gaps requires clock\.speed"):
        ScenarioCompiler().compile(spec)


def test_background_traffic_fills_only_long_gaps_deterministically() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    events = [
        {
            "id": name,
            "at": time,
            "action": "guest.exec",
            "from": "ws",
            "parameters": {"script": "true"},
        }
        for name, time in (("first", "09:00:00"), ("second", "11:00:00"), ("third", "11:05:00"))
    ]
    spec = ScenarioSpec.model_validate(
        {
            "scenario": {"id": "idle-gap", "seed": 7},
            "clock": {"start": "2025-01-01T09:00:00+00:00", "warp_gaps": True},
            "network": {"segments": {}, "nodes": {"ws": {"template": "ws"}}},
            "events": events,
            "background_traffic": {
                "min_gap": "30m",
                "min_interval": "10m",
                "max_interval": "20m",
                "max_events_per_gap": 3,
                "activities": [
                    {"from": "ws", "weight": 1000, "parameters": {"script": "date"}},
                    {"from": "ws", "parameters": {"script": "false"}},
                ],
            },
        }
    )
    compiler = ScenarioCompiler()
    first = compiler.compile(spec)
    second = compiler.compile(spec)
    generated = [event for event in first.events if event.id.startswith("background-")]

    assert first.events == second.events
    assert len(generated) == 3
    assert all(
        first.events[0].timestamp < item.timestamp < first.events[-2].timestamp
        for item in generated
    )
    assert all(item.action == "guest.exec" and item.from_ == "ws" for item in generated)
    assert all(item.parameters["script"] == "date" for item in generated)
    assert len({item.parameters["env"]["LABFOUNDRY_BACKGROUND_SEED"] for item in generated}) == 3


def test_background_traffic_requires_warped_clock() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "scenario": {"id": "idle-gap", "seed": 7},
            "clock": {"start": "2025-01-01T09:00:00+00:00"},
            "network": {"segments": {}, "nodes": {"ws": {"template": "ws"}}},
            "events": [
                {
                    "at": "09:00:00",
                    "action": "guest.exec",
                    "from": "ws",
                    "parameters": {"script": "true"},
                },
            ],
            "background_traffic": {
                "min_gap": "30m",
                "min_interval": "10m",
                "max_interval": "20m",
                "max_events_per_gap": 3,
                "activities": [{"from": "ws", "parameters": {"script": "date"}}],
            },
        }
    )

    with pytest.raises(CompilationError, match=r"requires clock\.warp_gaps"):
        ScenarioCompiler().compile(spec)


def test_background_traffic_can_bound_idle_gaps() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    data = {
        "scenario": {"id": "bounded-idle-gap", "seed": 7},
        "clock": {"start": "2025-01-01T09:00:00+00:00", "warp_gaps": True},
        "network": {"segments": {}, "nodes": {"ws": {"template": "ws"}}},
        "events": [
            {
                "id": "first",
                "at": "09:00:00",
                "action": "guest.exec",
                "from": "ws",
                "parameters": {"script": "true"},
            },
            {
                "id": "second",
                "at": "10:00:00",
                "action": "guest.exec",
                "from": "ws",
                "parameters": {"script": "true"},
            },
        ],
        "background_traffic": {
            "min_gap": "5m",
            "min_interval": "2m",
            "max_interval": "4m",
            "max_idle_gap": "5m",
            "max_events_per_gap": 30,
            "activities": [{"from": "ws", "parameters": {"script": "date"}}],
        },
    }
    plan = ScenarioCompiler().compile(ScenarioSpec.model_validate(data))
    timestamps = [event.timestamp for event in plan.events]

    assert max((right - left).total_seconds() for left, right in pairwise(timestamps)) <= 300

    data["background_traffic"]["max_events_per_gap"] = 2
    with pytest.raises(CompilationError, match="cannot cover idle gap"):
        ScenarioCompiler().compile(ScenarioSpec.model_validate(data))


def test_duplicate_mac_addresses_are_rejected() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "duplicate-mac", "seed": 1},
            "clock": {"start": "2025-01-01T00:00:00+00:00"},
            "network": {
                "segments": {"lan": {"subnet": "10.0.0.0/24"}},
                "nodes": {
                    "ws": {
                        "template": "ws",
                        "count": 2,
                        "interfaces": {
                            "lan": {
                                "segment": "lan",
                                "mac": "02:00:00:00:00:01",
                            }
                        },
                    }
                },
            },
        }
    )

    with pytest.raises(CompilationError, match=r"MAC address.*ws-01.*ws-02"):
        ScenarioCompiler().compile(spec)
