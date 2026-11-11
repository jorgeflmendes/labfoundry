"""GNS3 deployment adapter for compiled plans."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.backends.gns3.host import _udp_conflict_key, _windows_udp_binding_conflicts
from labfoundry.backends.gns3.lifecycle import NodeLifecycle
from labfoundry.backends.gns3.models import GNS3Template
from labfoundry.backends.gns3.virtualbox import VirtualBoxNodeLifecycle
from labfoundry.compiler.plan import ScenarioPlan
from labfoundry.errors import BackendError, GNS3APIError

if TYPE_CHECKING:
    from labfoundry.backends.gns3.incus import IncusEnvironment


class DeploymentState(BaseModel):
    """Immutable identifiers and metadata produced by a GNS3 deployment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    project_name: str
    gns3_version: str
    node_ids: dict[str, str] = Field(default_factory=dict)
    link_ids: dict[str, str] = Field(default_factory=dict)
    template_ids: dict[str, str] = Field(default_factory=dict)
    template_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    guest_runtime_ids: dict[str, str] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)


class GNS3Backend:
    """Deploy a ScenarioPlan using documented GNS3 Controller endpoints."""

    def __init__(
        self,
        client: GNS3Client,
        *,
        compute_id: str = "local",
        nat_compute_id: str | None = None,
        node_lifecycles: Mapping[str, NodeLifecycle] | None = None,
        environment: IncusEnvironment | None = None,
    ) -> None:
        self.client = client
        self.compute_id = compute_id
        self.nat_compute_id = nat_compute_id or compute_id
        self.environment = environment
        self.node_lifecycles: dict[str, NodeLifecycle] = {
            "virtualbox": VirtualBoxNodeLifecycle(),
        }
        if node_lifecycles is not None:
            self.node_lifecycles.update(node_lifecycles)

    async def _delete_project(self, project_id: str) -> None:
        for attempt in range(10):
            try:
                await self.client.delete_project(project_id)
                return
            except GNS3APIError as exc:
                locked = exc.status_code == 409 and "being used by another process" in exc.message
                if not locked or attempt == 9:
                    raise
                await asyncio.sleep(1)

    async def preflight(self, plan: ScenarioPlan) -> tuple[str, dict[str, GNS3Template]]:
        """Validate controller capabilities and resolve every template required by a plan."""
        version = await self.client.assert_supported_2_2()
        compute = await self.client.get_compute(self.compute_id)
        if compute.connected is False:
            raise BackendError(f"GNS3 compute {self.compute_id!r} is not connected")
        node_types = compute.capabilities.node_types
        if not node_types:
            raise BackendError(
                f"GNS3 compute {self.compute_id!r} did not report node-type capabilities"
            )

        # VM-only scenarios still need built-in switches for their logical segments.
        if "ethernet_switch" not in node_types:
            raise BackendError(
                f"GNS3 compute {self.compute_id!r} does not advertise Ethernet switch support"
            )

        incus_nodes = [node for node in plan.nodes if node.expected_template_type == "incus"]
        if incus_nodes:
            if self.environment is None:
                raise BackendError("Incus nodes require a configured scenario environment")
            if self.compute_id != "local" or urlparse(self.client.base_url).hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise BackendError(
                    "Incus scenario tunnels require a local GNS3 controller and compute"
                )
            platform = compute.capabilities.platform
            if platform is not None and (
                platform.startswith("win") != sys.platform.startswith("win")
                or platform.startswith("linux") != sys.platform.startswith("linux")
            ):
                raise BackendError("Incus tunnels require GNS3 and VirtualBox on the same host OS")
            if any(node.interfaces for node in incus_nodes) and "cloud" not in node_types:
                raise BackendError("GNS3 compute does not advertise UDP cloud endpoints")
            await self.environment.preflight(plan)

        for node in plan.nodes:
            if node.expected_template_type == "incus":
                continue
            if (
                node.expected_template_type is not None
                and node.expected_template_type not in node_types
            ):
                raise BackendError(
                    f"GNS3 compute {self.compute_id!r} does not advertise "
                    f"{node.expected_template_type} support"
                )
        if plan.egress is not None:
            nat_compute = (
                compute
                if self.nat_compute_id == self.compute_id
                else await self.client.get_compute(self.nat_compute_id)
            )
            if nat_compute.connected is False:
                raise BackendError(f"GNS3 NAT compute {self.nat_compute_id!r} is not connected")
            if "nat" not in nat_compute.capabilities.node_types:
                raise BackendError(
                    f"GNS3 compute {self.nat_compute_id!r} does not advertise NAT-node support"
                )

        templates = await self.client.list_templates()
        by_name: dict[str, list[GNS3Template]] = {}
        for template in templates:
            by_name.setdefault(template.name, []).append(template)

        resolved: dict[str, GNS3Template] = {}
        for node in plan.nodes:
            if node.expected_template_type == "incus":
                continue
            matches = by_name.get(node.template, [])
            if not matches:
                raise BackendError(f"GNS3 template {node.template!r} was not found")
            if len(matches) > 1:
                exact_compute = [item for item in matches if item.compute_id == self.compute_id]
                if len(exact_compute) == 1:
                    matches = exact_compute
                else:
                    raise BackendError(
                        f"GNS3 template name {node.template!r} is ambiguous; "
                        "use unique template names"
                    )
            template = matches[0]
            if template.compute_id not in (None, self.compute_id):
                raise BackendError(
                    f"Template {node.template!r} belongs to compute {template.compute_id!r}; "
                    f"LabFoundry 0.2 runs each scenario on one compute ({self.compute_id!r}) "
                    "to keep capture clocks coherent"
                )
            if (
                node.expected_template_type is not None
                and template.template_type != node.expected_template_type
            ):
                raise BackendError(
                    f"Template {node.template!r} has type {template.template_type!r}, expected "
                    f"{node.expected_template_type!r}"
                )
            if template.template_type == "nat":
                raise BackendError(
                    f"template {node.template!r} resolves to a NAT node; "
                    "declare NAT through network.egress instead"
                )
            if template.template_type == "cloud" and not plan.allow_cloud_nodes:
                raise BackendError(
                    f"template {node.template!r} resolves to a cloud node; "
                    "set network.allow_cloud_nodes=true explicitly to expose host interfaces"
                )
            if template.template_type not in node_types:
                raise BackendError(
                    f"GNS3 compute {self.compute_id!r} does not advertise support for "
                    f"template type {template.template_type!r} required by node {node.name!r}"
                )
            lifecycle = self.node_lifecycles.get(template.template_type)
            if lifecycle is not None:
                lifecycle.validate_template(node, template)
            resolved[node.template] = template
        return version.version, resolved

    async def deploy(self, plan: ScenarioPlan) -> DeploymentState:
        """Materialize a compiled scenario plan as a GNS3 project."""
        version, templates = await self.preflight(plan)
        project_name = f"lf-{plan.scenario_id}-{plan.root_seed}-{uuid4().hex[:8]}"
        project = await self.client.create_project(project_name)

        node_ids: dict[str, str] = {}
        link_ids: dict[str, str] = {}
        guest_runtime_ids: dict[str, str] = {}
        environment: dict[str, Any] = {}
        try:
            if self.environment is not None:
                environment = await self.environment.prepare(plan, project.project_id)
            for segment in plan.segments:
                switch = await self.client.create_ethernet_switch(
                    project.project_id,
                    name=segment.switch_name,
                    compute_id=self.compute_id,
                    port_count=segment.port_count,
                    x=segment.x,
                    y=segment.y,
                )
                node_ids[segment.switch_name] = switch.node_id

            for node in plan.nodes:
                if node.expected_template_type == "incus":
                    instance = environment["nodes"][node.name]
                    if instance["interfaces"]:
                        ports = [
                            {
                                "name": item["name"],
                                "port_number": item["port"],
                                "type": "udp",
                                "lport": item["host_port"],
                                "rhost": environment["guest_address"].split("/")[0],
                                "rport": item["guest_port"],
                            }
                            for item in instance["interfaces"]
                        ]
                        created_node = await self.client.create_udp_tunnel_node(
                            project.project_id,
                            name=node.name,
                            compute_id=self.compute_id,
                            ports=ports,
                            x=node.x,
                            y=node.y,
                        )
                    else:
                        created_node = await self.client.create_ethernet_switch(
                            project.project_id,
                            name=node.name,
                            compute_id=self.compute_id,
                            port_count=1,
                            x=node.x,
                            y=node.y,
                        )
                    node_ids[node.name] = created_node.node_id
                    guest_runtime_ids[node.name] = instance["name"]
                    continue
                template = templates[node.template]
                created_node = await self.client.create_node_from_template(
                    project.project_id,
                    template.template_id,
                    name=node.name,
                    x=node.x,
                    y=node.y,
                    compute_id=self.compute_id if template.compute_id is None else None,
                )
                if created_node.compute_id != self.compute_id:
                    raise BackendError(
                        f"GNS3 created node {node.name!r} on compute {created_node.compute_id!r}, "
                        f"expected {self.compute_id!r}"
                    )
                if (
                    node.expected_template_type is not None
                    and created_node.node_type != node.expected_template_type
                ):
                    raise BackendError(
                        f"GNS3 created node {node.name!r} with type {created_node.node_type!r}, "
                        f"expected {node.expected_template_type!r}"
                    )
                if node.interfaces and not created_node.ports:
                    raise BackendError(
                        f"GNS3 created node {node.name!r} without port inventory; "
                        "cannot verify scenario adapter mappings safely"
                    )
                available_ports = {
                    (port.adapter_number, port.port_number) for port in created_node.ports
                }
                for interface in node.interfaces:
                    requested = (interface.adapter, interface.port)
                    if requested not in available_ports:
                        raise BackendError(
                            f"node {node.name!r} does not expose requested GNS3 port "
                            f"adapter={interface.adapter} port={interface.port}; available="
                            f"{sorted(available_ports)!r}"
                        )
                node_ids[node.name] = created_node.node_id
                lifecycle = self.node_lifecycles.get(created_node.node_type)
                if lifecycle is not None:
                    created_node = await lifecycle.prepare_node(created_node, template)
                    guest_runtime_ids[node.name] = lifecycle.runtime_id(created_node, template)

            for link in plan.links:
                a_adapter, a_port = self._port(
                    environment, link.a.node, link.a.adapter, link.a.port
                )
                b_adapter, b_port = self._port(
                    environment, link.b.node, link.b.adapter, link.b.port
                )
                created_link = await self.client.create_link(
                    project.project_id,
                    a_node_id=node_ids[link.a.node],
                    a_adapter=a_adapter,
                    a_port=a_port,
                    b_node_id=node_ids[link.b.node],
                    b_adapter=b_adapter,
                    b_port=b_port,
                )
                link_ids[link.name] = created_link.link_id

            if plan.egress is not None:
                segment = next(item for item in plan.segments if item.name == plan.egress.segment)
                nat = await self.client.create_nat_node(
                    project.project_id,
                    name=plan.egress.name,
                    compute_id=self.nat_compute_id,
                    x=segment.x - 220,
                    y=segment.y,
                )
                node_ids[plan.egress.name] = nat.node_id
                egress_link = await self.client.create_link(
                    project.project_id,
                    a_node_id=nat.node_id,
                    a_adapter=0,
                    a_port=0,
                    b_node_id=node_ids[segment.switch_name],
                    b_adapter=0,
                    b_port=plan.egress.switch_port,
                )
                link_ids[f"{plan.egress.name}:nat--{plan.egress.segment}"] = egress_link.link_id
                await self.client.stop_node(project.project_id, nat.node_id)
        except BaseException as exc:
            # A failed deployment must not silently leave a half-built project.
            try:
                await self._delete_project(project.project_id)
            except Exception as cleanup_exc:
                exc.add_note(f"cleanup also failed: {cleanup_exc}")
            if self.environment is not None and environment:
                try:
                    await self.environment.destroy(environment)
                except Exception as cleanup_exc:
                    exc.add_note(f"scenario VM cleanup also failed: {cleanup_exc}")
            raise

        return DeploymentState(
            project_id=project.project_id,
            project_name=project_name,
            gns3_version=version,
            node_ids=node_ids,
            link_ids=link_ids,
            template_ids={name: template.template_id for name, template in templates.items()},
            guest_runtime_ids=guest_runtime_ids,
            environment=environment,
            template_metadata={
                node.name: (
                    {
                        "template_type": "incus",
                        "image": node.template,
                        "compute_id": self.compute_id,
                        "environment": environment["name"],
                    }
                    if node.expected_template_type == "incus"
                    else {
                        **templates[node.template].model_dump(mode="json", exclude_none=True),
                        "compute_id": self.compute_id,
                    }
                )
                for node in plan.nodes
            },
        )

    async def start(self, deployment: DeploymentState) -> None:
        """Start every node in a deployed scenario project."""
        if self.environment is not None and deployment.environment:
            await self.environment.start(deployment.environment)
        lifecycles = self._deployment_lifecycles(deployment)
        inspect_host = sys.platform == "win32" and self.compute_id == "local"
        existing = set()
        if inspect_host:
            try:
                existing = {
                    _udp_conflict_key(conflict) for conflict in _windows_udp_binding_conflicts()
                }
            except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                raise BackendError(f"failed to inspect Windows UDP tunnel bindings: {exc}") from exc
        for lifecycle in lifecycles:
            await lifecycle.before_start(deployment, self.compute_id)
        priorities: dict[str, int] = {}
        for name, node_id in deployment.node_ids.items():
            node_type = deployment.template_metadata.get(name, {}).get("template_type", "")
            node_lifecycle = self.node_lifecycles.get(node_type)
            priorities[node_id] = (
                node_lifecycle.start_priority(self.compute_id) if node_lifecycle is not None else 0
            )
        if priorities and (inspect_host or any(priorities.values())):
            for node_id in sorted(priorities, key=priorities.__getitem__):
                await self.client.start_node(deployment.project_id, node_id)
        else:
            await self.client.start_all_nodes(deployment.project_id)
        try:
            for lifecycle in lifecycles:
                await lifecycle.after_start(deployment, self.compute_id)
        except Exception:
            await self.client.stop_all_nodes(deployment.project_id)
            raise
        if not inspect_host:
            return
        try:
            conflicts = [
                conflict
                for conflict in _windows_udp_binding_conflicts()
                if _udp_conflict_key(conflict) not in existing
            ]
        except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            await self.client.stop_all_nodes(deployment.project_id)
            raise BackendError(f"failed to inspect Windows UDP tunnel bindings: {exc}") from exc
        if not conflicts:
            return

        port, wildcard, specific = conflicts[0]
        await self.client.stop_all_nodes(deployment.project_id)
        raise BackendError(
            "GNS3 UDP tunnel conflict on port "
            f"{port}: {wildcard['process_name']} (PID {wildcard['process_id']}) owns "
            f"{wildcard['local_address']}:{port}, while {specific['process_name']} "
            f"(PID {specific['process_id']}) owns {specific['local_address']}:{port}. "
            "Reconfigure the GNS3 UDP port range or stop the conflicting process."
        )

    async def stop(self, deployment: DeploymentState) -> None:
        """Stop every node in a deployed scenario project."""
        try:
            await self.client.stop_all_nodes(deployment.project_id)
        finally:
            if self.environment is not None and deployment.environment:
                await self.environment.stop(deployment.environment, force=True)

    async def destroy(self, deployment: DeploymentState) -> None:
        """Delete the deployed GNS3 project."""
        await self._delete_project(deployment.project_id)
        if self.environment is not None and deployment.environment:
            await self.environment.destroy(deployment.environment)
        for lifecycle in self._deployment_lifecycles(deployment):
            await lifecycle.after_destroy(deployment, self.compute_id)

    @staticmethod
    def _port(environment: dict[str, Any], node: str, adapter: int, port: int) -> tuple[int, int]:
        if node not in environment.get("nodes", {}):
            return adapter, port
        for interface in environment["nodes"][node]["interfaces"]:
            if interface["adapter"] == adapter:
                return 0, int(interface["port"])
        raise BackendError(f"missing Incus tunnel for {node!r} adapter {adapter}")

    async def node_action(self, deployment: DeploymentState, node: str, action: str) -> None:
        """Apply a node action to its container when it uses the shared VM."""
        if self.environment is not None and node in deployment.environment.get("nodes", {}):
            await self.environment.node_action(deployment.environment, node, action)

    async def suspend_environment(self, deployment: DeploymentState) -> None:
        """Quiesce the shared VM before a coordinated project snapshot."""
        if self.environment is not None and deployment.environment:
            await self.environment.stop(deployment.environment)

    async def resume_environment(self, deployment: DeploymentState) -> None:
        """Reconnect the shared VM before restarting project capture endpoints."""
        if self.environment is not None and deployment.environment:
            await self.environment.start(deployment.environment)

    def _deployment_lifecycles(self, deployment: DeploymentState) -> list[NodeLifecycle]:
        node_types = {
            template.get("template_type") for template in deployment.template_metadata.values()
        }
        return [
            lifecycle
            for node_type, lifecycle in self.node_lifecycles.items()
            if node_type in node_types
        ]
