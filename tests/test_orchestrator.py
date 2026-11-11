from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.plan import ScenarioPlan
from labfoundry.errors import GuestExecutionError
from labfoundry.runtime.clock import ScenarioClock
from labfoundry.runtime.orchestrator import ScenarioRunner, _normalize_runtime_error


def _plan() -> ScenarioPlan:
    return ScenarioPlan(
        scenario_id="case",
        root_seed=7,
        scenario_start=datetime.now(UTC),
        clock_speed=1000.0,
        duration_seconds=0.01,
        segments=(),
        nodes=(),
        links=(),
        events=(),
        traffic=(),
        captures=(),
        aliases={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_runner_executes_lifecycle_and_destroys_project(
    tmp_path: Path, cleanup_failure
) -> None:
    deployment = DeploymentState(
        project_id="project",
        project_name="lf-case-7",
        gns3_version="2.2.61",
    )
    backend = SimpleNamespace(
        client=Mock(),
        deploy=AsyncMock(return_value=deployment),
        start=AsyncMock(),
        stop=AsyncMock(),
        destroy=AsyncMock(),
    )
    captures = SimpleNamespace(start_all=AsyncMock(), stop_download_and_retime=AsyncMock())
    captures.stop_download_and_retime.return_value = []
    guests = SimpleNamespace(
        bind_deployment=Mock(),
        healthcheck_all=AsyncMock(),
        configure_network=AsyncMock(),
        check_identity_uniqueness=AsyncMock(),
        stop_all_processes=AsyncMock(
            side_effect=GuestExecutionError("cleanup failed") if cleanup_failure else None
        ),
        has_profile=Mock(return_value=False),
        plan=_plan(),
    )
    run_dir = tmp_path / "run"
    runner = ScenarioRunner(
        backend=backend,
        captures=captures,
        guests=guests,
        source_root=tmp_path,
        run_dir=run_dir,
    )

    if cleanup_failure:
        with pytest.raises(GuestExecutionError, match="cleanup failed"):
            await runner.run(guests.plan)
        backend.stop.assert_awaited_once()
        backend.destroy.assert_awaited_once()
        return

    result = await runner.run(guests.plan)

    assert result.run_dir == run_dir
    backend.deploy.assert_awaited_once()
    backend.start.assert_awaited_once()
    guests.bind_deployment.assert_called_once_with(deployment)
    guests.healthcheck_all.assert_awaited_once()
    guests.configure_network.assert_awaited_once()
    backend.stop.assert_awaited_once()
    backend.destroy.assert_awaited_once()
    assert (run_dir / "manifest.json").exists()


@pytest.mark.asyncio
async def test_runner_provisions_before_capture_start(tmp_path: Path) -> None:
    from labfoundry.compiler.plan import CompiledProvisionAction

    plan = _plan().model_copy(
        update={
            "sync_guest_clocks": True,
            "provision": (
                CompiledProvisionAction(
                    sequence=0,
                    node="ws",
                    action="guest.exec",
                    parameters={"script": "echo ready"},
                ),
            ),
        }
    )
    deployment = DeploymentState(
        project_id="project",
        project_name="lf-case-7",
        gns3_version="2.2.61",
    )
    order: list[str] = []
    backend = SimpleNamespace(
        client=Mock(),
        deploy=AsyncMock(return_value=deployment),
        start=AsyncMock(),
        stop=AsyncMock(),
        destroy=AsyncMock(),
    )
    captures = SimpleNamespace(
        start_all=AsyncMock(side_effect=lambda *args, **kwargs: order.append("capture")),
        stop_download_and_retime=AsyncMock(return_value=[]),
    )
    guests = SimpleNamespace(
        bind_deployment=Mock(),
        healthcheck_all=AsyncMock(),
        configure_network=AsyncMock(),
        check_identity_uniqueness=AsyncMock(),
        prepare_clock_control=AsyncMock(),
        set_clock_all=AsyncMock(side_effect=lambda *args: order.append("clock")),
        stop_all_processes=AsyncMock(),
        has_profile=Mock(return_value=True),
        exec_shell=AsyncMock(side_effect=lambda *args, **kwargs: order.append("provision")),
        node_ip=Mock(return_value="10.0.0.10"),
        plan=plan,
    )
    run_dir = tmp_path / "run"
    runner = ScenarioRunner(
        backend=backend,
        captures=captures,
        guests=guests,
        source_root=tmp_path,
        run_dir=run_dir,
    )

    await runner.run(plan)

    assert order == ["clock", "provision", "clock", "capture"]
    assert guests.set_clock_all.await_count == 2


def test_single_taskgroup_failure_is_unwrapped() -> None:
    failure = ValueError("boom")

    assert _normalize_runtime_error(ExceptionGroup("task group", [failure])) is failure


@pytest.mark.asyncio
async def test_runner_keeps_clock_segments_when_runtime_fails(tmp_path: Path) -> None:
    plan = _plan().model_copy(
        update={
            "duration_seconds": None,
            "warp_gaps": True,
            "settle_delay_seconds": 0.0,
            "captures": (Mock(timestamp_policy="retime"),),
        }
    )
    deployment = DeploymentState(
        project_id="project",
        project_name="lf-case-7",
        gns3_version="2.2.61",
    )
    backend = SimpleNamespace(
        client=Mock(),
        deploy=AsyncMock(return_value=deployment),
        start=AsyncMock(),
        stop=AsyncMock(),
        destroy=AsyncMock(),
    )
    captures = SimpleNamespace(
        start_all=AsyncMock(),
        stop_download_and_retime=AsyncMock(return_value=[]),
    )
    guests = SimpleNamespace(
        bind_deployment=Mock(),
        healthcheck_all=AsyncMock(),
        configure_network=AsyncMock(),
        check_identity_uniqueness=AsyncMock(),
        stop_all_processes=AsyncMock(),
        has_profile=Mock(return_value=False),
        plan=plan,
    )
    runner = ScenarioRunner(
        backend=backend,
        captures=captures,
        guests=guests,
        source_root=tmp_path,
        run_dir=tmp_path / "run",
    )

    async def fail_after_warp(
        _plan: ScenarioPlan,
        _dispatcher: object,
        _infrastructure: object,
        clock: ScenarioClock,
    ) -> None:
        clock.start_runtime()
        await clock.sleep_until(plan.scenario_start + timedelta(seconds=10))
        raise ValueError("runtime failed")

    runner._run_runtime = fail_after_warp  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="runtime failed"):
        await runner.run(plan)

    events = [
        json.loads(line) for line in (tmp_path / "run/internal/run.jsonl").read_text().splitlines()
    ]
    assert (
        next(event for event in events if event["event"] == "run.failed")["detail"]
        == "runtime failed"
    )
    clock = captures.stop_download_and_retime.await_args.kwargs["clock"]
    assert isinstance(clock, ScenarioClock)
    assert len(clock.segments) == 2
