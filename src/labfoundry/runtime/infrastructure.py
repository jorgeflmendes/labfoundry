"""Runtime actions that control the GNS3 project itself."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labfoundry.backends.gns3.backend import DeploymentState, GNS3Backend
from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.compiler.plan import CompiledEvent, ScenarioPlan
from labfoundry.errors import BackendError
from labfoundry.evidence.capture import CaptureManager
from labfoundry.guest.manager import GuestManager
from labfoundry.runtime.journal import RunJournal


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkUpdateParams(_Params):
    """Parameters for ``gns3.link.update``."""

    link: str = Field(min_length=1)
    suspend: bool | None = None
    filters: dict[str, list[int | str]] | None = None

    @model_validator(mode="after")
    def at_least_one_change(self) -> LinkUpdateParams:
        """Require a suspend state, filters, or both."""
        if self.suspend is None and self.filters is None:
            raise ValueError("gns3.link.update requires suspend and/or filters")
        return self


class SnapshotParams(_Params):
    """Snapshot action parameters."""

    name: str = Field(min_length=1)


class NodeParams(_Params):
    """Node lifecycle action parameters."""

    node: str = Field(min_length=1)


class InfrastructureActionDispatcher:
    """Execute generic link and snapshot actions through the GNS3 Controller API."""

    ACTIONS = frozenset(
        {
            "gns3.link.update",
            "gns3.snapshot.create",
            "gns3.snapshot.restore",
            "gns3.snapshot.delete",
            "gns3.node.start",
            "gns3.node.stop",
            "gns3.node.restart",
        }
    )

    def __init__(
        self,
        client: GNS3Client,
        plan: ScenarioPlan,
        deployment: DeploymentState,
        journal: RunJournal | None = None,
        guests: GuestManager | None = None,
        backend: GNS3Backend | None = None,
        captures: CaptureManager | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.plan = plan
        self.deployment = deployment
        self.journal = journal
        self.guests = guests
        self.backend = backend
        self.captures = captures
        self.run_dir = run_dir
        self._lock = asyncio.Lock()
        self._snapshots: dict[str, str] = {}

    @classmethod
    def validate_static(cls, plan: ScenarioPlan, event: CompiledEvent) -> None:
        """Validate one infrastructure event without requiring a deployed project."""
        if event.action == "gns3.link.update":
            link_params = LinkUpdateParams.model_validate(event.parameters)
            if link_params.link not in {link.name for link in plan.links}:
                raise BackendError(f"unknown compiled link {link_params.link!r}")
            return
        if event.action in {
            "gns3.snapshot.create",
            "gns3.snapshot.restore",
            "gns3.snapshot.delete",
        }:
            SnapshotParams.model_validate(event.parameters)
            return
        if event.action in {"gns3.node.start", "gns3.node.stop", "gns3.node.restart"}:
            node_params = NodeParams.model_validate(event.parameters)
            if node_params.node not in {node.name for node in plan.nodes}:
                raise BackendError(f"unknown compiled node {node_params.node!r}")
            return
        raise BackendError(f"unknown infrastructure action {event.action!r}")

    def validate_event(self, event: CompiledEvent) -> None:
        """Validate one infrastructure event against this dispatcher's plan."""
        self.validate_static(self.plan, event)

    async def handle_event(self, event: CompiledEvent) -> None:
        """Execute one infrastructure action and journal its outcome without parameters."""
        if self.journal is not None:
            self.journal.record("gns3.action.start", action=event.action, event_id=event.id)
        try:
            async with self._lock:
                await self._handle_event(event)
        except BaseException as exc:
            if self.journal is not None:
                self.journal.record(
                    "gns3.action.failed",
                    action=event.action,
                    event_id=event.id,
                    error=type(exc).__name__,
                )
            raise
        if self.journal is not None:
            self.journal.record("gns3.action.completed", action=event.action, event_id=event.id)

    async def _handle_event(self, event: CompiledEvent) -> None:
        """Execute one infrastructure action after the public journaling wrapper."""
        if event.action == "gns3.link.update":
            link_params = LinkUpdateParams.model_validate(event.parameters)
            link_id = self.deployment.link_ids[link_params.link]
            if link_params.filters is not None:
                await self._validate_filters(link_id, link_params.filters)
            await self.client.update_link(
                self.deployment.project_id,
                link_id,
                suspend=link_params.suspend,
                filters=link_params.filters,
            )
            return
        if event.action in {"gns3.node.start", "gns3.node.stop", "gns3.node.restart"}:
            node_params = NodeParams.model_validate(event.parameters)
            node_id = self.deployment.node_ids[node_params.node]
            affected = (node_params.node,)
            if event.action != "gns3.node.start":
                self._prepare_capture_restart(affected)
            if event.action == "gns3.node.stop":
                await self.client.stop_node(self.deployment.project_id, node_id)
                if self.backend is not None:
                    await self.backend.node_action(self.deployment, node_params.node, "stop")
                if self.guests is not None:
                    self.guests.forget_processes((node_params.node,))
                await self._pause_captures(affected)
                return
            if event.action == "gns3.node.restart":
                await self.client.stop_node(self.deployment.project_id, node_id)
                if self.backend is not None:
                    await self.backend.node_action(self.deployment, node_params.node, "stop")
                if self.guests is not None:
                    self.guests.forget_processes((node_params.node,))
                await self._pause_captures(affected)
            await self.client.start_node(self.deployment.project_id, node_id)
            await self._resume_captures(affected)
            if self.backend is not None:
                await self.backend.node_action(self.deployment, node_params.node, "start")
            if self.guests is not None:
                await self.guests.refresh_nodes((node_params.node,))
            return

        snapshot_params = SnapshotParams.model_validate(event.parameters)
        if event.action == "gns3.snapshot.create":
            started = await self._stop_started_nodes()
            try:
                snapshot = await self.client.create_snapshot(
                    self.deployment.project_id, snapshot_params.name
                )
                if self.backend is not None and self.backend.environment is not None:
                    try:
                        await self.backend.environment.snapshot(
                            self.deployment.environment, "take", snapshot.snapshot_id
                        )
                    except Exception as exc:
                        try:
                            await self.client.delete_snapshot(
                                self.deployment.project_id, snapshot.snapshot_id
                            )
                        except Exception as cleanup:
                            exc.add_note(f"GNS3 snapshot cleanup failed: {cleanup}")
                        raise
            finally:
                await self._restart_nodes(started)
            self._snapshots[snapshot_params.name] = snapshot.snapshot_id
            return
        snapshot_id = await self._snapshot_id(snapshot_params.name)
        if event.action == "gns3.snapshot.restore":
            started = await self._stop_started_nodes()
            resume = True
            try:
                if self.backend is not None and self.backend.environment is not None:
                    await self.backend.environment.snapshot(
                        self.deployment.environment, "restore", snapshot_id
                    )
                await self.client.restore_snapshot(self.deployment.project_id, snapshot_id)
            except Exception:
                if self.backend is not None and self.backend.environment is not None:
                    self.backend.environment.block_resume()
                    resume = False
                raise
            finally:
                if resume:
                    await self._restart_nodes(started)
            return
        if event.action == "gns3.snapshot.delete":
            await self.client.delete_snapshot(self.deployment.project_id, snapshot_id)
            if self.backend is not None and self.backend.environment is not None:
                await self.backend.environment.snapshot(
                    self.deployment.environment, "delete", snapshot_id
                )
            self._snapshots.pop(snapshot_params.name, None)
            return
        raise BackendError(f"unknown infrastructure action {event.action!r}")

    async def _stop_started_nodes(self) -> tuple[str, ...]:
        self._prepare_capture_restart()
        nodes = await self.client.list_nodes(self.deployment.project_id)
        started = tuple(node.node_id for node in nodes if node.status == "started")
        await self.client.stop_all_nodes(self.deployment.project_id)
        if self.deployment.environment:
            if self.backend is None:
                raise BackendError("scenario VM snapshots require the deployment backend")
            await self.backend.suspend_environment(self.deployment)
        await self._pause_captures()
        if self.guests is not None:
            logical = self._logical_names(started)
            self.guests.forget_processes(logical)
        return started

    async def _restart_nodes(self, node_ids: tuple[str, ...]) -> None:
        for node_id in node_ids:
            await self.client.start_node(self.deployment.project_id, node_id)
        await self._resume_captures()
        if self.deployment.environment:
            if self.backend is None:
                raise BackendError("scenario VM resume requires the deployment backend")
            await self.backend.resume_environment(self.deployment)
        if self.guests is not None:
            await self.guests.refresh_nodes(self._logical_names(node_ids))

    def _prepare_capture_restart(self, nodes: tuple[str, ...] | None = None) -> None:
        if self.deployment.environment and self.plan.captures and self.captures is None:
            raise BackendError("captured Incus lifecycle actions require the active CaptureManager")
        if self.captures is not None:
            if self.run_dir is None:
                raise BackendError("capture restart requires run_dir")
            self.captures.prepare_restart(self.plan, nodes)

    async def _pause_captures(self, nodes: tuple[str, ...] | None = None) -> None:
        if self.captures is not None and self.run_dir is not None:
            await self.captures.pause(self.plan, self.deployment, run_dir=self.run_dir, nodes=nodes)

    async def _resume_captures(self, nodes: tuple[str, ...] | None = None) -> None:
        if self.captures is not None and self.run_dir is not None:
            await self.captures.resume(
                self.plan, self.deployment, run_dir=self.run_dir, nodes=nodes
            )

    def _logical_names(self, node_ids: tuple[str, ...]) -> tuple[str, ...]:
        selected = set(node_ids)
        return tuple(
            name for name, node_id in self.deployment.node_ids.items() if node_id in selected
        )

    async def _snapshot_id(self, name: str) -> str:
        cached = self._snapshots.get(name)
        if cached is not None:
            return cached
        matches = [
            item
            for item in await self.client.list_snapshots(self.deployment.project_id)
            if item.name == name
        ]
        if len(matches) != 1:
            raise BackendError(
                f"snapshot {name!r} resolved to {len(matches)} entries; expected exactly one"
            )
        self._snapshots[name] = matches[0].snapshot_id
        return matches[0].snapshot_id

    async def _validate_filters(self, link_id: str, filters: dict[str, list[int | str]]) -> None:
        available = {
            item.type: item
            for item in await self.client.available_link_filters(
                self.deployment.project_id, link_id
            )
        }
        for filter_type, values in filters.items():
            capability = available.get(filter_type)
            if capability is None:
                raise BackendError(f"GNS3 link does not advertise filter {filter_type!r}")
            if len(values) != len(capability.parameters):
                raise BackendError(
                    f"filter {filter_type!r} expects {len(capability.parameters)} parameters; "
                    f"got {len(values)}"
                )
            for value, parameter in zip(values, capability.parameters, strict=True):
                if parameter.type == "int":
                    if not isinstance(value, int) or isinstance(value, bool):
                        raise BackendError(
                            f"filter {filter_type!r} parameter {parameter.name!r} must be int"
                        )
                    if parameter.minimum is not None and value < parameter.minimum:
                        raise BackendError(
                            f"filter {filter_type!r} parameter {parameter.name!r} is below "
                            f"minimum {parameter.minimum}"
                        )
                    if parameter.maximum is not None and value > parameter.maximum:
                        raise BackendError(
                            f"filter {filter_type!r} parameter {parameter.name!r} exceeds "
                            f"maximum {parameter.maximum}"
                        )
                elif not isinstance(value, str):
                    raise BackendError(
                        f"filter {filter_type!r} parameter {parameter.name!r} must be text"
                    )
