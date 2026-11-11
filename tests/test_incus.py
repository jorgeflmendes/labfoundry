import asyncio
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from labfoundry.backends.gns3.backend import DeploymentState, GNS3Backend
from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.backends.gns3.incus import IncusEnvironment
from labfoundry.backends.gns3.models import GNS3Compute, GNS3Node, GNS3Project, GNS3Version
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.errors import BackendError, GuestExecutionError
from labfoundry.guest import _incus_host
from labfoundry.guest.base import ExecResult, GuestCommand
from labfoundry.guest.config import GuestInventory, IncusGuestProfile
from labfoundry.guest.incus import IncusGuestExecutor
from labfoundry.guest.manager import GuestManager
from labfoundry.schema.scenario import ScenarioSpec


@pytest.fixture
def inventory() -> GuestInventory:
    return GuestInventory.model_validate(
        {
            "profiles": {
                "control": {"transport": "virtualbox", "os": "linux", "username": "operator"},
                "node": {
                    "transport": "incus",
                    "environment": "lab",
                    "network_control": "exclusive",
                },
            },
            "environments": {
                "lab": {
                    "vmname": "base",
                    "snapshot": "clean",
                    "guest_profile": "control",
                    "host_only_adapter": "host-only",
                    "host_address": "192.168.56.1",
                    "guest_addresses": ["192.168.56.10/24", "192.168.56.11/24"],
                    "memory_mib": 2048,
                    "cpus": 2,
                    "storage_pool": "local",
                    "reserve_memory_mib": 0,
                }
            },
        }
    )


@pytest.fixture
def plan():
    return ScenarioCompiler().compile(
        ScenarioSpec.model_validate(
            {
                "scenario": {"id": "incus-test", "seed": 2},
                "clock": {"start": "2019-01-02T00:00:00Z", "sync_guests": True},
                "network": {
                    "segments": {"lan": {"subnet": "10.42.0.0/24"}},
                    "nodes": {
                        "node": {
                            "template": "ubuntu",
                            "template_type": "incus",
                            "guest_profile": "node",
                            "count": 2,
                            "interfaces": {"eth0": {"segment": "lan", "adapter": 3}},
                        }
                    },
                },
            }
        )
    )


def _environment(inventory, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    environment = IncusEnvironment("lab", inventory, base_dir=tmp_path)

    async def vbox(*args, **kwargs):
        if args[0] == "showvminfo":
            vm = args[1] if args[1].startswith("lf-") else "lf-incus-project"
            return ExecResult(
                0,
                f'name="{vm}"\r\nUUID="12345678-abcd-abcd-abcd-123456789012"\r\n'
                'VMState="poweroff"\nnic1="nat"\nnic2="bridged"\n',
                "",
            )
        return ExecResult(0, "", "")

    environment._vbox = AsyncMock(side_effect=vbox)
    environment._host = Mock(return_value=Mock(disable_host_time_sync=AsyncMock()))
    return environment


@pytest.mark.parametrize(
    "address",
    ["192.168.56.1/24", "8.8.8.8/24", "127.0.0.2/8", "192.168.56.255/24", "192.168.57.2/24"],
)
def test_environment_rejects_unsafe_tunnel_addresses(inventory, address):
    data = inventory.model_dump(mode="json")
    data["environments"]["lab"]["guest_addresses"] = [address]
    with pytest.raises(ValidationError):
        GuestInventory.model_validate(data)


def test_container_has_no_implicit_application_resource_limit():
    profile = IncusGuestProfile(environment="lab")
    assert profile.memory_mib is None and profile.cpus is None and profile.processes is None
    with pytest.raises(ValidationError):
        IncusGuestProfile(environment="lab", privileged=True)


def test_host_helper_closes_stdin_for_noninteractive_initialization(monkeypatch):
    run = Mock(return_value=Mock(stdout="ready", returncode=0))
    monkeypatch.setattr(_incus_host.subprocess, "run", run)
    assert _incus_host._incus("init", "image", "container") == "ready"
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


@pytest.mark.asyncio
async def test_prepare_uses_linked_snapshot_and_disables_other_vm_networks(
    inventory, plan, tmp_path, monkeypatch
):
    environment = _environment(inventory, tmp_path, monkeypatch)
    state = await environment.prepare(plan, "project")
    calls = [call.args for call in environment._vbox.await_args_list]
    assert calls[0] == (
        "clonevm",
        "base",
        "--snapshot",
        "clean",
        "--name",
        "lf-incus-project",
        "--options",
        "link",
        "--register",
    )
    modification = next(call for call in calls if call[0] == "modifyvm")
    assert modification[modification.index("--nic2") + 1] == "none"
    assert modification[modification.index("--memory") + 1] == "2048"
    ports = [nic["host_port"] for node in state["nodes"].values() for nic in node["interfaces"]]
    assert len(set(ports)) == 2
    assert all(Path(path).exists() for path in state["port_leases"])
    await environment.destroy(state)
    assert not Path(state["lease"]).exists()
    assert all(not Path(path).exists() for path in state["port_leases"])


@pytest.mark.asyncio
async def test_failed_clone_releases_its_address(inventory, plan, tmp_path, monkeypatch):
    environment = _environment(inventory, tmp_path, monkeypatch)
    environment._vbox.side_effect = GuestExecutionError("clone failed")
    with pytest.raises(GuestExecutionError, match="clone failed"):
        await environment.prepare(plan, "project")
    assert not list((tmp_path / ".labfoundry/incus/leases").glob("*.json"))


@pytest.mark.asyncio
async def test_environment_will_not_delete_a_different_vm(inventory, plan, tmp_path, monkeypatch):
    environment = _environment(inventory, tmp_path, monkeypatch)
    state = await environment.prepare(plan, "project")
    environment._vbox.return_value = ExecResult(0, 'name="original"\n', "")
    environment._vbox.side_effect = None
    with pytest.raises(BackendError, match="ownership"):
        await environment.destroy(state)
    assert Path(state["lease"]).exists()
    assert not any(call.args[0] == "unregistervm" for call in environment._vbox.await_args_list)


@pytest.mark.asyncio
async def test_host_ram_admission_fails_before_cloning(inventory, plan, tmp_path, monkeypatch):
    environment = _environment(inventory, tmp_path, monkeypatch)
    monkeypatch.setattr("labfoundry.backends.gns3.incus._available_memory_mib", lambda: 1024)
    with pytest.raises(BackendError, match="free host RAM"):
        await environment.preflight(plan)
    environment._vbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_udp_node_payload_cannot_select_host_interfaces():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            201, json={"node_id": "id", "name": "node", "node_type": "cloud", "compute_id": "local"}
        )

    async with GNS3Client() as client:
        await client._http.aclose()
        client._http = httpx.AsyncClient(
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        port = {
            "name": "eth0",
            "port_number": 0,
            "type": "udp",
            "lport": 50000,
            "rhost": "192.168.56.10",
            "rport": 30000,
        }
        await client.create_udp_tunnel_node(
            "project", name="node", compute_id="local", ports=[port], x=0, y=0
        )
        for ports in ([], [{**port, "interface": "Ethernet"}], [{**port, "type": "ethernet"}]):
            with pytest.raises(ValueError):
                await client.create_udp_tunnel_node(
                    "project", name="node", compute_id="local", ports=ports, x=0, y=0
                )
    assert calls[0]["properties"] == {"ports_mapping": [port]}
    assert len(calls) == 1


def _client():
    client = AsyncMock(spec=GNS3Client)
    client.base_url = "http://127.0.0.1:3080"
    client.assert_supported_2_2.return_value = GNS3Version(version="2.2.61")
    client.get_compute.return_value = GNS3Compute(
        compute_id="local",
        name="local",
        host="127.0.0.1",
        port=3080,
        protocol="http",
        connected=True,
        capabilities={"node_types": ["cloud", "ethernet_switch"]},
    )
    client.list_templates.return_value = []
    client.create_project.return_value = GNS3Project(project_id="project")
    client.create_ethernet_switch.return_value = GNS3Node(
        node_id="switch", name="lan", node_type="ethernet_switch", compute_id="local"
    )
    client.create_udp_tunnel_node.side_effect = [
        GNS3Node(node_id=f"id-{i}", name=f"node-{i}", node_type="cloud", compute_id="local")
        for i in range(2)
    ]
    client.create_link.return_value = Mock(link_id="link")
    return client


@pytest.mark.asyncio
async def test_gns3_deploy_maps_sparse_container_adapters_and_rolls_back(
    inventory, plan, tmp_path, monkeypatch
):
    environment = _environment(inventory, tmp_path, monkeypatch)
    state = await environment.prepare(plan, "project")
    client = _client()
    managed = AsyncMock(spec=IncusEnvironment)
    managed.prepare.return_value = state
    backend = GNS3Backend(client, environment=managed)
    deployment = await backend.deploy(plan)
    assert deployment.environment["vm_name"] == "lf-incus-project"
    assert all(item["template_type"] == "incus" for item in deployment.template_metadata.values())
    client.create_node_from_template.assert_not_awaited()
    for call in client.create_link.await_args_list:
        if call.kwargs["a_node_id"].startswith("id-"):
            assert (call.kwargs["a_adapter"], call.kwargs["a_port"]) == (0, 0)
        else:
            assert (call.kwargs["b_adapter"], call.kwargs["b_port"]) == (0, 0)
    client = _client()
    client.create_link.side_effect = RuntimeError("link failure")
    with pytest.raises(RuntimeError, match="link failure"):
        await GNS3Backend(client, environment=managed).deploy(plan)
    client.delete_project.assert_awaited_once_with("project")
    managed.destroy.assert_awaited_once_with(state)


@pytest.mark.asyncio
async def test_shared_clock_is_set_once_for_all_containers(inventory, plan, tmp_path):
    manager = GuestManager(plan, inventory, base_dir=tmp_path)
    manager.bind_deployment(
        DeploymentState(
            project_id="project",
            project_name="project",
            gns3_version="2.2.61",
            environment={
                "name": "lab",
                "vm_name": "vm",
                "nodes": {
                    node.name: {"name": f"container-{i}"} for i, node in enumerate(plan.nodes)
                },
            },
        )
    )
    clocks = []
    for executor in manager._executors.values():
        executor.set_clock = AsyncMock()
        clocks.append(executor.set_clock)
    await manager.set_clock_all(datetime(2019, 1, 2, tzinfo=UTC))
    assert sum(clock.await_count for clock in clocks) == 1


@pytest.mark.asyncio
async def test_exec_preserves_arguments_and_kills_process_group_on_timeout():
    host = Mock()

    async def execute(command):
        if command.executable == "/usr/bin/sudo" and "kill -KILL" in " ".join(command.args):
            return ExecResult(0, "", "")
        await asyncio.Event().wait()

    host.exec = AsyncMock(side_effect=execute)
    executor = IncusGuestExecutor(
        "container",
        IncusGuestProfile(environment="lab"),
        host=host,
        vm_name="vm",
        host_sudo=True,
        host_username="operator",
    )
    with pytest.raises(GuestExecutionError, match="timeout"):
        await executor.exec(GuestCommand("printf", ("%s", "a; b 'c'"), timeout=0.01))
    command = host.exec.await_args_list[0].args[0]
    assert command.args[-3:] == ("printf", "%s", "a; b 'c'")
    assert any("kill -KILL" in " ".join(call.args[0].args) for call in host.exec.await_args_list)


def test_host_helper_rejects_privileged_existing_container(monkeypatch):
    node = {"name": "container"}

    def incus(*args, **kwargs):
        if args[0] == "list":
            return json.dumps([{"name": "container"}])
        return json.dumps(
            {"expanded_config": {"security.privileged": "true", "security.idmap.isolated": "true"}}
        )

    monkeypatch.setattr(_incus_host, "_incus", incus)
    with pytest.raises(RuntimeError, match="unsafe isolation"):
        _incus_host._container(node, {}, "fingerprint", {"container"})


def test_container_start_failure_includes_incus_log(monkeypatch):
    def incus(*args, **kwargs):
        if args[0] == "start":
            raise RuntimeError("forkstart failed")
        assert args == ("info", "container", "--show-log")
        return "Failed to attach network device"

    monkeypatch.setattr(_incus_host, "_incus", incus)
    with pytest.raises(RuntimeError, match="Failed to attach network device"):
        _incus_host._start("container")


def test_parallel_setup_does_not_overlap_incus_idmap_allocation(monkeypatch):
    allocating = Lock()

    def incus(*args, **kwargs):
        if args[0] == "init":
            if not allocating.acquire(blocking=False):
                raise RuntimeError("isolated idmap allocation overlapped")
            try:
                time.sleep(0.01)
            finally:
                allocating.release()
            return ""
        assert args[0] == "query"
        return json.dumps(
            {
                "expanded_config": {
                    "security.privileged": "false",
                    "security.idmap.isolated": "true",
                    "security.guestapi": "false",
                    "volatile.base_image": "fingerprint",
                }
            }
        )

    monkeypatch.setattr(_incus_host, "_incus", incus)
    nodes = [{"name": str(index), "nesting": False, "interfaces": []} for index in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda node: _incus_host._container(
                    node, {"storage_pool": "local"}, "fingerprint", set()
                ),
                nodes,
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [True, False])
async def test_shutdown_retries_only_console_session_transition(
    inventory, tmp_path, monkeypatch, transient
):
    environment = _environment(inventory, tmp_path, monkeypatch)
    error = GuestExecutionError("VBOX_E_INVALID_OBJECT_STATE" if transient else "access denied")
    environment._vbox = AsyncMock(
        side_effect=[
            ExecResult(0, 'VMState="running"', ""),
            error,
            ExecResult(0, 'VMState="poweroff"', ""),
        ]
    )
    environment._helper = AsyncMock()
    environment._root = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    if transient:
        await environment.stop({"vm_uuid": "owned"})
        assert environment._vbox.await_count == 3
    else:
        with pytest.raises(GuestExecutionError, match="access denied"):
            await environment.stop({"vm_uuid": "owned"})
