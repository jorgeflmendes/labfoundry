from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.backends.gns3.models import (
    GNS3AvailableFilter,
    GNS3FilterParameter,
    GNS3Node,
    GNS3Snapshot,
)
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.compiler.plan import CompiledEvent
from labfoundry.errors import BackendError
from labfoundry.runtime.infrastructure import InfrastructureActionDispatcher
from labfoundry.schema.scenario import ScenarioSpec


def _plan():
    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "infra", "seed": 7},
            "clock": {"start": "2025-01-01T00:00:00+00:00"},
            "network": {
                "segments": {"lan": {"subnet": "10.0.0.0/24"}},
                "nodes": {
                    "ws": {
                        "template": "vbox",
                        "interfaces": {"eth0": {"segment": "lan", "ip": "auto"}},
                    }
                },
            },
        }
    )
    return ScenarioCompiler().compile(spec)


def _deployment(plan) -> DeploymentState:
    return DeploymentState(
        project_id="project",
        project_name="lf-infra",
        gns3_version="2.2.61",
        node_ids={plan.nodes[0].name: "node-id"},
        link_ids={plan.links[0].name: "link-id"},
    )


@pytest.mark.asyncio
async def test_node_restart_stops_then_starts_same_gns3_node() -> None:
    plan = _plan()
    client = SimpleNamespace(stop_node=AsyncMock(), start_node=AsyncMock())
    guests = SimpleNamespace(forget_processes=Mock(), refresh_nodes=AsyncMock())
    dispatcher = InfrastructureActionDispatcher(client, plan, _deployment(plan), guests=guests)
    event = CompiledEvent(
        sequence=0,
        id="restart",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="gns3.node.restart",
        parameters={"node": "ws"},
    )

    await dispatcher.handle_event(event)

    client.stop_node.assert_awaited_once_with("project", "node-id")
    client.start_node.assert_awaited_once_with("project", "node-id")
    guests.forget_processes.assert_called_once_with(("ws",))
    guests.refresh_nodes.assert_awaited_once_with(("ws",))


@pytest.mark.asyncio
async def test_failed_environment_restore_does_not_resume_partially_restored_guests():
    plan = _plan()
    deployment = _deployment(plan).model_copy(update={"environment": {"name": "lab"}})
    snapshot = GNS3Snapshot(
        snapshot_id="snapshot-id", name="checkpoint", project_id="project", created_at=1
    )
    client = SimpleNamespace(
        list_snapshots=AsyncMock(return_value=[snapshot]),
        list_nodes=AsyncMock(
            return_value=[
                GNS3Node(
                    node_id="node-id",
                    name="ws",
                    node_type="cloud",
                    compute_id="local",
                    status="started",
                )
            ]
        ),
        stop_all_nodes=AsyncMock(),
        start_node=AsyncMock(),
        restore_snapshot=AsyncMock(side_effect=RuntimeError("controller restore failed")),
    )
    environment = SimpleNamespace(snapshot=AsyncMock(), block_resume=Mock())
    backend = SimpleNamespace(
        environment=environment, suspend_environment=AsyncMock(), resume_environment=AsyncMock()
    )
    dispatcher = InfrastructureActionDispatcher(client, plan, deployment, backend=backend)
    event = CompiledEvent(
        sequence=0,
        id="restore",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="gns3.snapshot.restore",
        parameters={"name": "checkpoint"},
    )
    with pytest.raises(RuntimeError, match="controller restore failed"):
        await dispatcher.handle_event(event)
    environment.block_resume.assert_called_once()
    backend.resume_environment.assert_not_awaited()
    client.start_node.assert_not_awaited()


def test_node_action_rejects_unknown_compiled_node() -> None:
    plan = _plan()
    event = CompiledEvent(
        sequence=0,
        id="stop",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="gns3.node.stop",
        parameters={"node": "missing"},
    )

    with pytest.raises(BackendError, match="unknown compiled node"):
        InfrastructureActionDispatcher.validate_static(plan, event)


@pytest.mark.asyncio
async def test_link_filter_parameters_follow_controller_advertised_limits() -> None:
    plan = _plan()
    link_name = plan.links[0].name
    client = SimpleNamespace(
        available_link_filters=AsyncMock(
            return_value=[
                GNS3AvailableFilter(
                    type="delay",
                    name="Delay",
                    parameters=[
                        GNS3FilterParameter(name="latency", type="int", minimum=0, maximum=32767)
                    ],
                )
            ]
        ),
        update_link=AsyncMock(),
    )
    dispatcher = InfrastructureActionDispatcher(client, plan, _deployment(plan))
    event = CompiledEvent(
        sequence=0,
        id="delay",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="gns3.link.update",
        parameters={"link": link_name, "filters": {"delay": [80]}},
    )

    await dispatcher.handle_event(event)
    client.update_link.assert_awaited_once()

    bad = event.model_copy(
        update={"parameters": {"link": link_name, "filters": {"delay": [50000]}}}
    )
    with pytest.raises(BackendError, match="maximum 32767"):
        await dispatcher.handle_event(bad)


@pytest.mark.asyncio
async def test_snapshot_create_stops_and_restarts_project_nodes() -> None:
    plan = _plan()
    client = SimpleNamespace(
        list_nodes=AsyncMock(
            return_value=[
                GNS3Node(
                    node_id="node-id",
                    name="ws",
                    node_type="virtualbox",
                    compute_id="local",
                    project_id="project",
                    status="started",
                ),
                GNS3Node(
                    node_id="stopped-id",
                    name="nat",
                    node_type="nat",
                    compute_id="local",
                    project_id="project",
                    status="stopped",
                ),
            ]
        ),
        stop_all_nodes=AsyncMock(),
        start_node=AsyncMock(),
        create_snapshot=AsyncMock(
            return_value=GNS3Snapshot(
                snapshot_id="snapshot-id",
                name="checkpoint",
                project_id="project",
                created_at=1,
            )
        ),
    )
    dispatcher = InfrastructureActionDispatcher(client, plan, _deployment(plan))
    event = CompiledEvent(
        sequence=0,
        id="snapshot",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="gns3.snapshot.create",
        parameters={"name": "checkpoint"},
    )

    await dispatcher.handle_event(event)

    client.stop_all_nodes.assert_awaited_once_with("project")
    client.start_node.assert_awaited_once_with("project", "node-id")
