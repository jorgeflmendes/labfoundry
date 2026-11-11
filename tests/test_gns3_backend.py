import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from labfoundry.backends.gns3.backend import (
    DeploymentState,
    GNS3Backend,
)
from labfoundry.backends.gns3.host import _find_udp_binding_conflicts
from labfoundry.backends.gns3.lifecycle import NodeLifecycle
from labfoundry.backends.gns3.models import GNS3Compute, GNS3Node, GNS3Template, GNS3Version
from labfoundry.backends.gns3.virtualbox import (
    VirtualBoxNodeLifecycle,
    _recover_windows_orphaned_virtualbox_nodes,
    _unregister_windows_virtualbox_nodes_for_project,
)
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.errors import BackendError, GNS3APIError
from labfoundry.schema.loader import load_scenario


@pytest.mark.parametrize("operation", ["recover", "unregister"])
def test_virtualbox_registration_paths_unescape_backslashes(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    run = Mock(
        side_effect=[
            SimpleNamespace(stdout='"tiago_ws" {vm-id}\n'),
            SimpleNamespace(stdout=r'CfgFile="C:\\GNS3\\projects\\project-id\\tiago_ws.vbox"'),
            SimpleNamespace(stdout=""),
        ]
    )
    monkeypatch.setattr("labfoundry.backends.gns3.virtualbox.subprocess.run", run)
    if operation == "recover":
        _recover_windows_orphaned_virtualbox_nodes("project-id", {"tiago_ws"})
        assert run.call_count == 2
    else:
        _unregister_windows_virtualbox_nodes_for_project("project-id", {"tiago_ws"})
        assert run.call_args.args[0][1:] == ["unregistervm", "vm-id"]
    assert all(
        item.kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for item in run.call_args_list
    )


@pytest.mark.parametrize(
    ("properties", "linked_clone", "vmname", "expected"),
    [
        ({"linked_clone": True}, False, "base", "guest"),
        ({}, True, "base", "guest"),
        ({"linked_clone": False, "vmname": "registered"}, True, "base", "registered"),
        ({}, False, "base", "base"),
    ],
)
def test_virtualbox_lifecycle_resolves_runtime_identity(properties, linked_clone, vmname, expected):
    template = GNS3Template(
        template_id="template-id",
        name="template",
        template_type="virtualbox",
        linked_clone=linked_clone,
        vmname=vmname,
    )
    node = GNS3Node(
        node_id="node-id",
        name="guest",
        node_type="virtualbox",
        compute_id="local",
        properties=properties,
    )

    assert VirtualBoxNodeLifecycle().runtime_id(node, template) == expected


@pytest.mark.asyncio
async def test_container_cleanup_does_not_touch_virtualbox(monkeypatch):
    client = SimpleNamespace(delete_project=AsyncMock())
    cleanup = Mock()
    monkeypatch.setattr("labfoundry.backends.gns3.virtualbox.sys.platform", "win32")
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._unregister_windows_virtualbox_nodes_for_project",
        cleanup,
    )
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        guest_runtime_ids={"container": "container-id"},
        template_metadata={"container": {"template_type": "docker"}},
    )

    await GNS3Backend(client).destroy(deployment)

    client.delete_project.assert_awaited_once_with("project-id")
    cleanup.assert_not_called()


def _client(compute: GNS3Compute, templates: tuple[GNS3Template, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        assert_supported_2_2=AsyncMock(return_value=GNS3Version(version="2.2.61")),
        get_compute=AsyncMock(return_value=compute),
        list_templates=AsyncMock(return_value=list(templates)),
    )


@pytest.mark.asyncio
async def test_backend_uses_injected_node_lifecycle() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    plan = ScenarioCompiler().compile(
        ScenarioSpec.model_validate(
            {
                "scenario": {"id": "custom-lifecycle", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {},
                    "nodes": {
                        "guest": {"template": "custom", "template_type": "virtualbox"},
                    },
                },
            }
        )
    )
    compute = GNS3Compute(
        compute_id="remote",
        name="remote",
        host="192.0.2.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["ethernet_switch", "virtualbox"]},
    )
    template = GNS3Template(template_id="template-id", name="custom", template_type="virtualbox")
    node = GNS3Node(node_id="node-id", name="guest", node_type="virtualbox", compute_id="remote")
    client = _client(compute, (template,))
    client.create_project = AsyncMock(return_value=SimpleNamespace(project_id="project-id"))
    client.create_node_from_template = AsyncMock(return_value=node)
    client.start_node = AsyncMock()
    client.start_all_nodes = AsyncMock()
    client.delete_project = AsyncMock()
    lifecycle = Mock(spec=NodeLifecycle)
    lifecycle.runtime_id.return_value = "custom-runtime"
    lifecycle.prepare_node.return_value = node
    lifecycle.start_priority.return_value = 1
    backend = GNS3Backend(client, compute_id="remote", node_lifecycles={"virtualbox": lifecycle})

    deployment = await backend.deploy(plan)
    await backend.start(deployment)
    await backend.destroy(deployment)

    lifecycle.validate_template.assert_called_once_with(plan.nodes[0], template)
    lifecycle.runtime_id.assert_called_once_with(node, template)
    assert deployment.guest_runtime_ids == {"guest": "custom-runtime"}
    lifecycle.before_start.assert_awaited_once_with(deployment, "remote")
    client.start_node.assert_awaited_once_with("project-id", "node-id")
    client.start_all_nodes.assert_not_awaited()
    lifecycle.after_start.assert_awaited_once_with(deployment, "remote")
    lifecycle.after_destroy.assert_awaited_once_with(deployment, "remote")


@pytest.mark.asyncio
async def test_lifecycle_verification_failure_stops_nodes() -> None:
    client = SimpleNamespace(start_all_nodes=AsyncMock(), stop_all_nodes=AsyncMock())
    lifecycle = Mock(spec=NodeLifecycle)
    lifecycle.start_priority.return_value = 0
    lifecycle.after_start.side_effect = BackendError("ownership check failed")
    backend = GNS3Backend(client, compute_id="remote", node_lifecycles={"docker": lifecycle})
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        node_ids={"guest": "node-id"},
        template_metadata={"guest": {"template_type": "docker"}},
    )

    with pytest.raises(BackendError, match="ownership check failed"):
        await backend.start(deployment)

    client.stop_all_nodes.assert_awaited_once_with("project-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("egress", [None, {"segment": "uplink"}])
async def test_preflight_rejects_nat_templates_without_declared_type(egress) -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    plan = ScenarioCompiler().compile(
        ScenarioSpec.model_validate(
            {
                "scenario": {"id": "nat-check", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {"uplink": {"subnet": "192.168.122.0/24"}},
                    "nodes": {"edge": {"template": "NAT"}},
                    "egress": egress,
                },
            }
        )
    )
    compute = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["ethernet_switch", "nat"]},
    )
    template = GNS3Template(template_id="nat-id", name="NAT", template_type="nat")
    client = _client(compute, (template,))
    client.create_project = AsyncMock()

    with pytest.raises(BackendError, match=r"declare NAT through network\.egress"):
        await GNS3Backend(client).deploy(plan)

    client.create_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_rejects_virtualbox_adapter_outside_template_capacity(
    scenario_path: Path,
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    compute = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["virtualbox", "ethernet_switch"]},
    )
    template = GNS3Template(
        template_id="template-1",
        name="test-gateway",
        template_type="virtualbox",
        compute_id="local",
        vmname="test-gateway",
        adapters=2,
        linked_clone=True,
    )
    backend = GNS3Backend(_client(compute, (template,)), compute_id="local")

    with pytest.raises(BackendError, match=r"requests \[2\]"):
        await backend.preflight(plan)


@pytest.mark.asyncio
async def test_preflight_requires_compute_capabilities(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    compute = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={},
    )
    backend = GNS3Backend(_client(compute), compute_id="local")

    with pytest.raises(BackendError, match="did not report node-type capabilities"):
        await backend.preflight(plan)


@pytest.mark.asyncio
async def test_preflight_requires_nat_capability_for_explicit_egress() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "egress", "seed": 1},
            "clock": {"start": "2025-01-01T00:00:00+00:00"},
            "network": {
                "segments": {"uplink": {"subnet": "192.168.122.0/24"}},
                "egress": {"mode": "nat", "segment": "uplink"},
                "nodes": {},
            },
        }
    )
    plan = ScenarioCompiler().compile(spec)
    compute = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["ethernet_switch"]},
    )
    backend = GNS3Backend(_client(compute), compute_id="local")

    with pytest.raises(BackendError, match="NAT"):
        await backend.preflight(plan)


@pytest.mark.asyncio
async def test_preflight_allows_nat_on_a_second_compute() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    spec = ScenarioSpec.model_validate(
        {
            "version": "0.2",
            "scenario": {"id": "egress", "seed": 1},
            "clock": {"start": "2025-01-01T00:00:00+00:00"},
            "network": {
                "segments": {"uplink": {"subnet": "192.168.122.0/24"}},
                "egress": {"mode": "nat", "segment": "uplink"},
                "nodes": {},
            },
        }
    )
    plan = ScenarioCompiler().compile(spec)
    primary = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["ethernet_switch"]},
    )
    nat = GNS3Compute(
        compute_id="gns3-vm",
        name="GNS3 VM",
        host="192.0.2.1",
        port=80,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["nat"]},
    )
    client = _client(primary)
    client.get_compute.side_effect = lambda compute_id: {
        "local": primary,
        "gns3-vm": nat,
    }[compute_id]
    backend = GNS3Backend(client, compute_id="local", nat_compute_id="gns3-vm")

    await backend.preflight(plan)

    assert client.get_compute.await_count == 2


@pytest.mark.asyncio
async def test_deploy_stops_nat_after_linking_it() -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    plan = ScenarioCompiler().compile(
        ScenarioSpec.model_validate(
            {
                "version": "0.2",
                "scenario": {"id": "egress", "seed": 1},
                "clock": {"start": "2025-01-01T00:00:00+00:00"},
                "network": {
                    "segments": {"uplink": {"subnet": "192.168.122.0/24"}},
                    "egress": {"mode": "nat", "segment": "uplink"},
                    "nodes": {},
                },
            }
        )
    )
    compute = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["ethernet_switch", "nat"]},
    )
    client = _client(compute)
    client.create_project = AsyncMock(return_value=SimpleNamespace(project_id="project-id"))
    client.create_ethernet_switch = AsyncMock(return_value=SimpleNamespace(node_id="switch-id"))
    client.create_nat_node = AsyncMock(return_value=SimpleNamespace(node_id="nat-id"))
    client.create_link = AsyncMock(return_value=SimpleNamespace(link_id="link-id"))
    client.stop_node = AsyncMock()
    backend = GNS3Backend(client)

    await backend.deploy(plan)

    project_name = client.create_project.await_args.args[0]
    assert project_name.startswith("lf-egress-1-")
    client.stop_node.assert_awaited_once_with("project-id", "nat-id")


@pytest.mark.asyncio
async def test_destroy_retries_transient_virtualbox_log_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = GNS3APIError(
        409,
        "[WinError 32] The process cannot access the file because it is "
        "being used by another process",
        method="DELETE",
        path="/v2/projects/project-id",
    )
    client = SimpleNamespace(delete_project=AsyncMock(side_effect=[locked, None]))
    sleep = AsyncMock()
    monkeypatch.setattr("labfoundry.backends.gns3.backend.asyncio.sleep", sleep)
    backend = GNS3Backend(client)
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
    )

    await backend.destroy(deployment)

    assert client.delete_project.await_count == 2
    sleep.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_destroy_removes_orphaned_windows_virtualbox_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(delete_project=AsyncMock())
    backend = GNS3Backend(client, compute_id="local")
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        guest_runtime_ids={"guest": "guest-vm"},
        template_metadata={"guest": {"template_type": "virtualbox"}},
    )
    cleanup = Mock()
    monkeypatch.setattr("labfoundry.backends.gns3.backend.sys.platform", "win32")
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._unregister_windows_virtualbox_nodes_for_project",
        cleanup,
    )

    await backend.destroy(deployment)

    cleanup.assert_called_once_with("project-id", {"guest-vm"})


@pytest.mark.asyncio
async def test_destroy_does_not_retry_other_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conflict = GNS3APIError(
        409,
        "project is outside the default directory",
        method="DELETE",
        path="/v2/projects/project-id",
    )
    client = SimpleNamespace(delete_project=AsyncMock(side_effect=conflict))
    sleep = AsyncMock()
    monkeypatch.setattr("labfoundry.backends.gns3.backend.asyncio.sleep", sleep)
    backend = GNS3Backend(client)
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
    )

    with pytest.raises(GNS3APIError):
        await backend.destroy(deployment)

    sleep.assert_not_awaited()


def test_udp_binding_conflicts_detect_overlapping_gns3_listener() -> None:
    endpoints = [
        {
            "local_address": "0.0.0.0",
            "local_port": 10010,
            "process_id": 34644,
            "process_name": "VBoxHeadless",
        },
        {
            "local_address": "127.0.0.1",
            "local_port": 10010,
            "process_id": 18132,
            "process_name": "nvcontainer",
        },
        {
            "local_address": "127.0.0.1",
            "local_port": 10011,
            "process_id": 18132,
            "process_name": "nvcontainer",
        },
    ]

    conflicts = _find_udp_binding_conflicts(endpoints)

    assert conflicts == [(10010, endpoints[0], endpoints[1])]


def test_udp_binding_conflicts_ignore_same_process() -> None:
    endpoints = [
        {
            "local_address": "0.0.0.0",
            "local_port": 10010,
            "process_id": 42,
            "process_name": "VBoxHeadless",
        },
        {
            "local_address": "127.0.0.1",
            "local_port": 10010,
            "process_id": 42,
            "process_name": "VBoxHeadless",
        },
    ]

    assert _find_udp_binding_conflicts(endpoints) == []


@pytest.mark.asyncio
async def test_start_stops_nodes_when_windows_udp_tunnel_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        start_all_nodes=AsyncMock(),
        start_node=AsyncMock(),
        stop_all_nodes=AsyncMock(),
    )
    backend = GNS3Backend(client, compute_id="local")
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        node_ids={"switch": "switch-id", "vm": "vm-id"},
        template_metadata={"vm": {"template_type": "virtualbox"}},
    )
    conflict = (
        10010,
        {
            "local_address": "0.0.0.0",
            "process_id": 34644,
            "process_name": "VBoxHeadless",
        },
        {
            "local_address": "127.0.0.1",
            "process_id": 18132,
            "process_name": "nvcontainer",
        },
    )
    monkeypatch.setattr("labfoundry.backends.gns3.backend.sys.platform", "win32")
    monkeypatch.setattr(
        "labfoundry.backends.gns3.backend._windows_udp_binding_conflicts",
        Mock(side_effect=[[], [conflict]]),
    )
    recover = Mock()
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._recover_windows_orphaned_virtualbox_nodes",
        recover,
    )
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._assert_windows_virtualbox_node_ownership",
        Mock(),
    )

    with pytest.raises(BackendError, match=r"port 10010.*nvcontainer"):
        await backend.start(deployment)

    client.start_all_nodes.assert_not_awaited()
    assert client.start_node.await_args_list == [
        call("project-id", "switch-id"),
        call("project-id", "vm-id"),
    ]
    recover.assert_called_once_with("project-id", {"vm"})
    client.stop_all_nodes.assert_awaited_once_with("project-id")


@pytest.mark.asyncio
async def test_start_ignores_preexisting_windows_udp_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        start_all_nodes=AsyncMock(),
        start_node=AsyncMock(),
        stop_all_nodes=AsyncMock(),
    )
    backend = GNS3Backend(client, compute_id="local")
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        node_ids={"switch": "switch-id", "vm": "vm-id"},
        template_metadata={"vm": {"template_type": "virtualbox"}},
    )
    conflict = (
        10010,
        {
            "local_address": "0.0.0.0",
            "process_id": 34644,
            "process_name": "VBoxHeadless",
        },
        {
            "local_address": "127.0.0.1",
            "process_id": 18132,
            "process_name": "nvcontainer",
        },
    )
    monkeypatch.setattr("labfoundry.backends.gns3.backend.sys.platform", "win32")
    monkeypatch.setattr(
        "labfoundry.backends.gns3.backend._windows_udp_binding_conflicts",
        Mock(side_effect=[[conflict], [conflict]]),
    )
    recover = Mock()
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._recover_windows_orphaned_virtualbox_nodes",
        recover,
    )
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._assert_windows_virtualbox_node_ownership",
        Mock(),
    )

    await backend.start(deployment)

    client.start_all_nodes.assert_not_awaited()
    assert client.start_node.await_args_list == [
        call("project-id", "switch-id"),
        call("project-id", "vm-id"),
    ]
    recover.assert_called_once_with("project-id", {"vm"})
    client.stop_all_nodes.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_stops_nodes_when_virtualbox_resolves_another_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(
        start_all_nodes=AsyncMock(),
        start_node=AsyncMock(),
        stop_all_nodes=AsyncMock(),
    )
    backend = GNS3Backend(client, compute_id="local")
    deployment = DeploymentState(
        project_id="project-id",
        project_name="project",
        gns3_version="2.2.61",
        node_ids={"vm": "vm-id"},
        template_metadata={"vm": {"template_type": "virtualbox"}},
    )
    monkeypatch.setattr("labfoundry.backends.gns3.backend.sys.platform", "win32")
    monkeypatch.setattr(
        "labfoundry.backends.gns3.backend._windows_udp_binding_conflicts", Mock(return_value=[])
    )
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._recover_windows_orphaned_virtualbox_nodes",
        Mock(),
    )
    monkeypatch.setattr(
        "labfoundry.backends.gns3.virtualbox._assert_windows_virtualbox_node_ownership",
        Mock(side_effect=BackendError("outside the current GNS3 project")),
    )

    with pytest.raises(BackendError, match="outside the current GNS3 project"):
        await backend.start(deployment)

    client.stop_all_nodes.assert_awaited_once_with("project-id")
