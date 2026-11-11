"""End-to-end deterministic scenario lifecycle orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from labfoundry.backends.gns3.backend import DeploymentState, GNS3Backend
from labfoundry.compiler.plan import ScenarioPlan
from labfoundry.errors import RuntimeOrchestrationError
from labfoundry.evidence.capture import CaptureManager, ExportedCapture
from labfoundry.groundtruth.manifest import write_internal_metadata
from labfoundry.guest.manager import GuestManager
from labfoundry.runtime.actions import GuestActionDispatcher
from labfoundry.runtime.clock import ScenarioClock
from labfoundry.runtime.infrastructure import InfrastructureActionDispatcher
from labfoundry.runtime.journal import RunJournal
from labfoundry.runtime.scheduler import EventScheduler
from labfoundry.traffic.engine import TrafficEngine


@dataclass(frozen=True, slots=True)
class RunResult:
    """Completed scenario run and exported evidence metadata."""

    run_dir: Path
    deployment: DeploymentState
    captures: tuple[ExportedCapture, ...]


def _normalize_runtime_error(exc: BaseException) -> BaseException:
    if not isinstance(exc, ExceptionGroup):
        return exc

    leaves: list[Exception] = []

    def collect(group: ExceptionGroup) -> None:
        for item in group.exceptions:
            if isinstance(item, ExceptionGroup):
                collect(item)
            else:
                leaves.append(item)

    collect(exc)
    if len(leaves) == 1:
        return leaves[0]

    error = RuntimeOrchestrationError(f"{len(leaves)} concurrent runtime operations failed")
    for item in leaves:
        error.add_note(f"{type(item).__name__}: {item}")
    return error


class ScenarioRunner:
    """Execute a compiled plan from GNS3 deployment through evidence export and cleanup."""

    def __init__(
        self,
        *,
        backend: GNS3Backend,
        captures: CaptureManager,
        guests: GuestManager,
        source_root: Path,
        asset_root: Path | None = None,
        run_dir: Path,
        destroy_project: bool = True,
        keep_project_on_failure: bool = False,
    ) -> None:
        self.backend = backend
        self.captures = captures
        self.guests = guests
        self.source_root = source_root
        self.asset_root = asset_root
        self.run_dir = run_dir
        self.destroy_project = destroy_project
        self.keep_project_on_failure = keep_project_on_failure

    def preflight(self, plan: ScenarioPlan) -> None:
        """Validate runtime actions and run-directory safety before creating a GNS3 project."""
        if self.run_dir.exists():
            if not self.run_dir.is_dir():
                raise RuntimeOrchestrationError(
                    f"run path exists and is not a directory: {self.run_dir}"
                )
            if any(self.run_dir.iterdir()):
                raise RuntimeOrchestrationError(
                    f"run directory is not empty and will not be reused: {self.run_dir}"
                )
        dispatcher = GuestActionDispatcher(
            self.guests,
            source_root=self.source_root,
            asset_root=self.asset_root,
            run_dir=self.run_dir,
        )
        supported = dispatcher.ACTIONS | InfrastructureActionDispatcher.ACTIONS
        unknown = sorted({event.action for event in plan.events if event.action not in supported})
        if unknown:
            raise RuntimeOrchestrationError(
                "full runner only knows built-in actions: " + ", ".join(repr(x) for x in unknown)
            )
        for event in plan.events:
            if event.action in dispatcher.ACTIONS:
                dispatcher.validate_event(event)
            else:
                InfrastructureActionDispatcher.validate_static(plan, event)
        for traffic_action in plan.traffic:
            dispatcher.validate_traffic(traffic_action)
        for provision_action in plan.provision:
            dispatcher.validate_provision(provision_action)
        if plan.duration_seconds is not None and plan.events:
            latest = max(event.timestamp for event in plan.events)
            scenario_end = plan.scenario_start.timestamp() + plan.duration_seconds
            if latest.timestamp() > scenario_end:
                raise RuntimeOrchestrationError(
                    f"event {latest.isoformat()} occurs after configured scenario duration"
                )

    async def _run_runtime(
        self,
        plan: ScenarioPlan,
        dispatcher: GuestActionDispatcher,
        infrastructure: InfrastructureActionDispatcher,
        clock: ScenarioClock,
    ) -> None:
        async def synchronize_after_warp(timestamp: datetime) -> None:
            await self.guests.set_clock_all(timestamp)

        async def settle_before_warp(timestamp: datetime) -> None:
            remaining = clock.runtime_delay_until(timestamp)
            settled = await self.guests.settle_pending_connections(timeout=min(130.0, remaining))
            if not settled and clock.runtime_delay_until(timestamp) > 1:
                raise RuntimeOrchestrationError(
                    "pending TCP connections did not settle before time warp"
                )

        scheduler = EventScheduler(
            clock,
            on_time_warp=synchronize_after_warp if plan.sync_guest_clocks else None,
            before_time_warp=settle_before_warp if plan.warp_gaps else None,
        )
        for action in dispatcher.ACTIONS:
            scheduler.register(action, dispatcher.handle_event)
        for action in infrastructure.ACTIONS:
            scheduler.register(action, infrastructure.handle_event)
        traffic = TrafficEngine(clock, dispatcher.handle_traffic)
        dispatcher.prepare_events(plan.events)
        clock.start_runtime()

        duration_seconds = plan.duration_seconds
        if duration_seconds is None:
            await scheduler.run(plan.events)
            return

        async def stop_at_duration() -> None:
            await asyncio.sleep(duration_seconds / plan.clock_speed)
            traffic.stop()

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(scheduler.run(plan.events))
            tasks.create_task(stop_at_duration())
            for traffic_action in plan.traffic:
                tasks.create_task(traffic.run_action(traffic_action))

    async def run(self, plan: ScenarioPlan) -> RunResult:
        """Run one scenario and return exported capture metadata.

        After a runtime failure, started captures still undergo finalization and audit and
        may be published. The original failure is raised after cleanup; published captures
        alone do not indicate that the scenario completed.
        """
        self.preflight(plan)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        journal = RunJournal(self.run_dir)
        journal.record("run.started", scenario_id=plan.scenario_id, seed=plan.root_seed)
        deployment: DeploymentState | None = None
        captures_started = False
        exported: list[ExportedCapture] = []
        primary_error: BaseException | None = None
        clock: ScenarioClock | None = None
        dispatcher: GuestActionDispatcher | None = None
        processes_stopped = False

        async def finalize_guests() -> None:
            nonlocal processes_stopped
            await self.guests.stop_all_processes()
            processes_stopped = True
            journal.record("guest_processes.cleaned")
            if dispatcher is not None:
                await dispatcher.finalize_downloads(best_effort=primary_error is not None)

        try:
            journal.record("deployment.start")
            deployment = await self.backend.deploy(plan)
            journal.record("deployment.completed", project_id=deployment.project_id)
            write_internal_metadata(
                self.run_dir,
                plan=plan,
                deployment=deployment,
                extra_manifest={"status": "deployed"},
            )
            self.guests.bind_deployment(deployment)
            if plan.sync_guest_clocks:
                await self.guests.prepare_clock_control()
            await self.backend.start(deployment)
            journal.record("nodes.started")
            await self.guests.healthcheck_all()
            journal.record("guests.healthy")
            await self.guests.configure_network()
            journal.record("guests.network_configured")
            if plan.sync_guest_clocks:
                await self.guests.set_clock_all(plan.scenario_start)
            dispatcher = GuestActionDispatcher(
                self.guests,
                source_root=self.source_root,
                asset_root=self.asset_root,
                run_dir=self.run_dir,
                journal=journal,
            )
            for provision_action in plan.provision:
                await dispatcher.handle_provision(provision_action)
            if plan.provision:
                journal.record("guests.provisioned", count=len(plan.provision))
            await self.guests.check_identity_uniqueness()
            journal.record("guests.identities_checked")
            if plan.sync_guest_clocks:
                await self.guests.set_clock_all(plan.scenario_start)
                journal.record("guests.clock_synchronized", at=plan.scenario_start.isoformat())
            await self.captures.start_all(plan, deployment, run_dir=self.run_dir)
            journal.record("captures.started", count=len(plan.captures))
            captures_started = bool(plan.captures)
            infrastructure = InfrastructureActionDispatcher(
                self.backend.client,
                plan,
                deployment,
                journal=journal,
                guests=self.guests,
                backend=self.backend,
                captures=self.captures,
                run_dir=self.run_dir,
            )
            journal.record("runtime.start")
            clock = ScenarioClock(
                plan.scenario_start,
                plan.clock_speed,
                warp_gaps=plan.warp_gaps,
                settle_delay=plan.settle_delay_seconds,
            )
            await self._run_runtime(plan, dispatcher, infrastructure, clock)
            journal.record("runtime.completed")
        except BaseException as exc:
            primary_error = _normalize_runtime_error(exc)
            journal.record(
                "run.failed", error=type(primary_error).__name__, detail=str(primary_error)
            )
        finally:
            if deployment is not None and captures_started:
                try:
                    exported = await self.captures.stop_download_and_retime(
                        plan,
                        deployment,
                        run_dir=self.run_dir,
                        clock=clock,
                        after_capture=finalize_guests,
                    )
                    journal.record("captures.exported", count=len(exported))
                    audit = getattr(self.captures, "last_audit", None)
                    if audit is not None:
                        journal.record(
                            "captures.audited",
                            findings=len(audit.findings),
                            packets=audit.packets,
                        )
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    else:
                        primary_error.add_note(f"capture finalization also failed: {exc}")
            if not processes_stopped:
                try:
                    await self.guests.stop_all_processes()
                    journal.record("guest_processes.cleaned")
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    else:
                        primary_error.add_note(f"guest process cleanup also failed: {exc}")
            if deployment is not None:
                try:
                    await self.backend.stop(deployment)
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                    else:
                        primary_error.add_note(f"node shutdown also failed: {exc}")
                should_destroy = self.destroy_project and not (
                    primary_error is not None and self.keep_project_on_failure
                )
                if should_destroy:
                    try:
                        await self.backend.destroy(deployment)
                        journal.record("deployment.destroyed")
                    except BaseException as exc:
                        if primary_error is None:
                            primary_error = exc
                        else:
                            primary_error.add_note(f"project cleanup also failed: {exc}")
            if deployment is not None:
                audit = getattr(self.captures, "last_audit", None)
                write_internal_metadata(
                    self.run_dir,
                    plan=plan,
                    deployment=deployment,
                    extra_manifest={
                        "status": "failed" if primary_error is not None else "completed",
                        "captures": [item.name for item in exported],
                        "realism_audit": (
                            None
                            if audit is None
                            else {
                                "findings": [item.code for item in audit.findings],
                                "packets": audit.packets,
                            }
                        ),
                    },
                )
            if primary_error is None:
                journal.record("run.completed", captures=len(exported))
        if primary_error is not None:
            raise primary_error
        assert deployment is not None
        return RunResult(
            run_dir=self.run_dir,
            deployment=deployment,
            captures=tuple(exported),
        )
