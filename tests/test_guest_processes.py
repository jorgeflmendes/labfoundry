from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from labfoundry.compiler.plan import CompiledNode, ScenarioPlan
from labfoundry.errors import GuestExecutionError
from labfoundry.guest.base import ExecResult, ProcessHandle
from labfoundry.guest.config import GuestInventory, VirtualBoxGuestProfile
from labfoundry.guest.manager import GuestManager


def _manager() -> GuestManager:
    plan = ScenarioPlan(
        scenario_id="case",
        root_seed=1,
        scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        clock_speed=1.0,
        segments=(),
        nodes=(
            CompiledNode(
                name="ws",
                group="ws",
                template="template",
                expected_template_type="virtualbox",
                guest_profile="vbox",
                interfaces=(),
                x=0,
                y=0,
            ),
        ),
        links=(),
        events=(),
        traffic=(),
        captures=(),
        aliases={},
    )
    inventory = GuestInventory(
        profiles={
            "vbox": VirtualBoxGuestProfile(
                transport="virtualbox",
                os="linux",
                username="rf",
                password_env="LF_TEST_PASSWORD",
            )
        }
    )
    import os

    os.environ["LF_TEST_PASSWORD"] = "secret"
    manager = GuestManager(plan, inventory, base_dir=__import__("pathlib").Path("."))
    return manager


@pytest.mark.asyncio
async def test_process_running_checks_tracked_pid(monkeypatch) -> None:
    manager = _manager()
    manager._processes[("ws", "job")] = ProcessHandle("ws", "job", 123)
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecResult(0, "", "")
    )

    assert await manager.process_running("ws", "job") is True
    manager.exec_shell.assert_awaited_once()
    assert "grep -q '^[^Z]'" in manager.exec_shell.await_args.args[1]

    manager.exec_shell.return_value = ExecResult(1, "", "")
    assert await manager.process_running("ws", "job") is False


@pytest.mark.asyncio
async def test_start_readiness_waits_for_guest_probe() -> None:
    manager = _manager()
    manager._processes[("ws", "server")] = ProcessHandle("ws", "server", 123)
    manager.process_running = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ExecResult(1, "", "not listening"), ExecResult(0, "", "")]
    )

    await manager.wait_process_ready("ws", "server", "probe", timeout=1, interval=0.001)

    assert manager.exec_shell.await_count == 2


@pytest.mark.asyncio
async def test_start_readiness_reports_process_exit() -> None:
    manager = _manager()
    manager._processes[("ws", "server")] = ProcessHandle("ws", "server", 123)
    manager.process_running = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with pytest.raises(GuestExecutionError, match="exited before it was ready"):
        await manager.wait_process_ready("ws", "server", "probe", timeout=1, interval=0.001)


@pytest.mark.asyncio
async def test_start_readiness_reports_probe_failure_at_deadline() -> None:
    manager = _manager()
    manager._processes[("ws", "server")] = ProcessHandle("ws", "server", 123)
    manager.process_running = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        side_effect=GuestExecutionError("probe transport timed out")
    )

    with pytest.raises(GuestExecutionError, match="last probe failed: probe transport timed out"):
        await manager.wait_process_ready("ws", "server", "probe", timeout=0.01, interval=0.001)


@pytest.mark.asyncio
async def test_start_process_rejects_process_that_exits_immediately(monkeypatch) -> None:
    manager = _manager()
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ExecResult(0, "123\n", ""), ExecResult(1, "", "")]
    )

    with pytest.raises(GuestExecutionError, match="exited immediately"):
        await manager.start_process("ws", "job", "exit 1")

    assert ("ws", "job") not in manager._processes


@pytest.mark.asyncio
async def test_wait_process_forgets_handle_when_process_exits(monkeypatch) -> None:
    manager = _manager()
    manager._processes[("ws", "job")] = ProcessHandle("ws", "job", 123)
    manager.process_running = AsyncMock(side_effect=[True, False])  # type: ignore[method-assign]

    await manager.wait_process("ws", "job", timeout=1.0, interval=0.001)

    assert ("ws", "job") not in manager._processes


@pytest.mark.asyncio
async def test_wait_process_times_out(monkeypatch) -> None:
    manager = _manager()
    manager._processes[("ws", "job")] = ProcessHandle("ws", "job", 123)
    manager.process_running = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with pytest.raises(GuestExecutionError, match="did not exit"):
        await manager.wait_process("ws", "job", timeout=0.01, interval=0.001)
