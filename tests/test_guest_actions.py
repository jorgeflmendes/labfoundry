from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from labfoundry.compiler.plan import (
    CompiledEvent,
    CompiledEventCondition,
    CompiledProvisionAction,
    CompiledTrafficAction,
)
from labfoundry.errors import GuestExecutionError
from labfoundry.guest.base import ExecResult
from labfoundry.runtime.actions import GuestActionDispatcher, ReadyParams


@pytest.mark.asyncio
async def test_download_refreshes_only_opted_in_artifacts(tmp_path) -> None:
    content = b"initial"

    async def download(_node, _source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)

    guests = SimpleNamespace(
        plan=SimpleNamespace(root_seed=1, scenario_id="case", scenario_start=datetime.now(UTC)),
        node_ip=lambda _: "10.0.0.1",
        download=AsyncMock(side_effect=download),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    for index, refresh in enumerate((False, True)):
        await dispatcher.handle_event(
            CompiledEvent(
                sequence=index,
                id=f"download-{index}",
                timestamp=datetime.now(UTC),
                **{"from": "ws"},
                action="guest.download",
                parameters={
                    "source": "/tmp/tls.keys",
                    "destination": f"{index}.keys",
                    "publish": True,
                    "refresh_at_end": refresh,
                },
            )
        )
    content = b"initial\nfinal"
    await dispatcher.finalize_downloads()
    staged = tmp_path / "run/internal/staged-evidence"
    assert (staged / "0.keys").read_bytes() == b"initial"
    assert (staged / "1.keys").read_bytes() == content


@pytest.mark.asyncio
async def test_failed_run_collects_refresh_artifacts_before_their_events(tmp_path) -> None:
    guests = SimpleNamespace(download=AsyncMock())
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    events = tuple(
        CompiledEvent(
            sequence=index,
            id=f"download-{index}",
            timestamp=datetime.now(UTC),
            **{"from": "ws"},
            action="guest.download",
            when=CompiledEventCondition(event="prerequisite", exit_code=0) if index == 2 else None,
            parameters={
                "source": f"/tmp/{index}.keys",
                "destination": f"{index}.keys",
                "publish": True,
                "refresh_at_end": index != 0,
            },
        )
        for index in range(3)
    )
    dispatcher.prepare_events(events)
    await dispatcher.finalize_downloads(best_effort=True)
    guests.download.assert_awaited_once_with(
        "ws",
        "/tmp/1.keys",
        tmp_path / "run/internal/staged-evidence/1.keys",
    )


@pytest.mark.asyncio
async def test_failed_run_continues_after_missing_refresh_artifact(tmp_path) -> None:
    guests = SimpleNamespace(download=AsyncMock(side_effect=[GuestExecutionError("missing"), None]))
    journal = SimpleNamespace(record=Mock())
    dispatcher = GuestActionDispatcher(
        guests,
        source_root=tmp_path,
        run_dir=tmp_path / "run",
        journal=journal,
    )
    dispatcher.prepare_events(
        tuple(
            CompiledEvent(
                sequence=index,
                id=f"download-{index}",
                timestamp=datetime.now(UTC),
                **{"from": "ws"},
                action="guest.download",
                parameters={
                    "source": f"/tmp/{index}.keys",
                    "destination": f"{index}.keys",
                    "refresh_at_end": True,
                },
            )
            for index in range(2)
        )
    )
    await dispatcher.finalize_downloads(best_effort=True)
    assert guests.download.await_count == 2
    journal.record.assert_called_once_with(
        "guest.download.finalization_failed",
        node="ws",
        source="/tmp/0.keys",
        error="GuestExecutionError",
    )
    guests.download.side_effect = GuestExecutionError("missing")
    with pytest.raises(GuestExecutionError, match="missing"):
        await dispatcher.finalize_downloads()


@pytest.mark.asyncio
async def test_guest_copy_transfers_between_guests_without_publishing(tmp_path) -> None:
    async def download(_node, _source, destination):
        destination.write_bytes(b"certificate")

    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: {"proxy": "10.0.0.1", "client": "10.0.0.2"}[node],
        has_profile=lambda node: node in {"proxy", "client"},
        download=AsyncMock(side_effect=download),
        upload=AsyncMock(),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    action = CompiledProvisionAction(
        sequence=0,
        node="proxy",
        to="client",
        action="guest.copy",
        parameters={"source": "/tmp/ca.crt", "destination": "/tmp/ca.crt"},
    )

    dispatcher.validate_provision(action)
    await dispatcher.handle_provision(action)

    guests.download.assert_awaited_once()
    guests.upload.assert_awaited_once()
    assert guests.upload.await_args.args[0] == "client"
    assert guests.upload.await_args.args[2] == "/tmp/ca.crt"
    assert not list(tmp_path.rglob("*ca.crt"))


@pytest.mark.asyncio
async def test_guest_exec_injects_procedural_context(tmp_path) -> None:
    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=42,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: {"ws": "10.0.0.10", "server": "10.0.0.20"}[node],
        exec_shell=AsyncMock(),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    event = CompiledEvent(
        sequence=0,
        id="e1",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.exec",
        **{"from": "ws"},
        to="server",
        parameters={"script": "curl http://$LABFOUNDRY_TARGET_IP", "timeout": "5s"},
    )

    await dispatcher.handle_event(event)

    kwargs = guests.exec_shell.await_args.kwargs
    assert kwargs["env"]["LABFOUNDRY_SEED"] == "42"
    assert kwargs["env"]["LABFOUNDRY_NODE_IP"] == "10.0.0.10"
    assert kwargs["env"]["LABFOUNDRY_TARGET_IP"] == "10.0.0.20"
    assert kwargs["timeout"] == 5.0


def test_upload_escape_is_rejected_during_preflight(tmp_path) -> None:
    from labfoundry.errors import GuestExecutionError

    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        has_profile=lambda node: node == "ws",
    )
    dispatcher = GuestActionDispatcher(
        guests, source_root=tmp_path / "scenario", run_dir=tmp_path / "run"
    )
    (tmp_path / "scenario").mkdir()
    event = CompiledEvent(
        sequence=0,
        id="escape",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.upload",
        **{"from": "ws"},
        parameters={"source": "../secret.txt", "destination": "/tmp/secret.txt"},
    )

    with pytest.raises(GuestExecutionError, match="escapes scenario directory"):
        dispatcher.validate_event(event)


@pytest.mark.parametrize(
    ("action", "parameters", "message"),
    [
        ("guest.exec", {"script": "true", "timeout": 0}, "timeout must be greater than zero"),
        ("guest.exec", {"script": "true", "timeout": "0s"}, "timeout must be greater than zero"),
        (
            "guest.exec",
            {"script": "true", "retry": {"delay": -1}},
            "retry delay cannot be negative",
        ),
        ("guest.wait", {"id": "worker", "timeout": 0}, "wait timeout must be greater than zero"),
        (
            "guest.wait",
            {"id": "worker", "interval": "0s"},
            "wait interval must be greater than zero",
        ),
    ],
)
def test_invalid_action_timing_is_rejected_during_preflight(
    tmp_path, action, parameters, message
) -> None:
    from pydantic import ValidationError

    dispatcher = GuestActionDispatcher(
        SimpleNamespace(has_profile=lambda _: True), source_root=tmp_path, run_dir=tmp_path / "run"
    )
    event = CompiledEvent(
        sequence=0,
        id="invalid-timing",
        timestamp=datetime.now(UTC),
        action=action,
        **{"from": "ws"},
        parameters=parameters,
    )

    with pytest.raises(ValidationError, match=message):
        dispatcher.validate_event(event)


def test_upload_can_use_separate_asset_directory(tmp_path) -> None:
    from labfoundry.errors import GuestExecutionError

    assets = tmp_path / "assets"
    assets.mkdir()
    bundle = assets / "pages.zip"
    bundle.write_bytes(b"fixture")
    dispatcher = GuestActionDispatcher(
        SimpleNamespace(),
        source_root=tmp_path / "scenario",
        asset_root=assets,
        run_dir=tmp_path / "run",
    )

    assert dispatcher._safe_source("@assets/pages.zip") == bundle
    with pytest.raises(GuestExecutionError, match="escapes asset directory"):
        dispatcher._safe_source("@assets/../outside.zip")


def test_upload_bundled_helpers_without_allowing_package_traversal(tmp_path) -> None:
    from labfoundry.errors import GuestExecutionError

    dispatcher = GuestActionDispatcher(
        SimpleNamespace(),
        source_root=tmp_path,
        run_dir=tmp_path / "run",
    )
    for name in ("browser_timing.py", "response_timing.py"):
        assert dispatcher._safe_source(f"@helpers/{name}").is_file()
    for name in ("../config.py", "virtualbox.py", "/etc/passwd", "missing.py"):
        with pytest.raises(GuestExecutionError, match="unknown guest helper"):
            dispatcher._safe_source(f"@helpers/{name}")


@pytest.mark.asyncio
async def test_guest_exec_retries_transient_guest_execution_error(tmp_path) -> None:
    from labfoundry.errors import GuestExecutionError

    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        exec_shell=AsyncMock(
            side_effect=[GuestExecutionError("transient"), SimpleNamespace(exit_code=0)]
        ),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    event = CompiledEvent(
        sequence=0,
        id="retry",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.exec",
        **{"from": "ws"},
        parameters={
            "script": "curl https://example.test",
            "retry": {"attempts": 2, "delay": 0},
        },
    )

    await dispatcher.handle_event(event)

    assert guests.exec_shell.await_count == 2


@pytest.mark.asyncio
async def test_guest_wait_delegates_to_process_wait(tmp_path) -> None:
    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        wait_process=AsyncMock(),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    event = CompiledEvent(
        sequence=0,
        id="wait",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.wait",
        **{"from": "ws"},
        parameters={"id": "browser", "timeout": "5s", "interval": "250ms"},
    )

    await dispatcher.handle_event(event)

    guests.wait_process.assert_awaited_once_with("ws", "browser", timeout=5.0, interval=0.25)


@pytest.mark.asyncio
async def test_guest_start_waits_for_readiness(tmp_path) -> None:
    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        start_process=AsyncMock(),
        wait_process_ready=AsyncMock(),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    event = CompiledEvent(
        sequence=0,
        id="start",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.start",
        **{"from": "ws"},
        parameters={
            "id": "server",
            "script": "python3 server.py",
            "ready": {"script": "ss -ltn | grep -q ':8080 '", "timeout": "5s"},
        },
    )

    await dispatcher.handle_event(event)

    guests.start_process.assert_awaited_once()
    guests.wait_process_ready.assert_awaited_once_with(
        "ws", "server", "ss -ltn | grep -q ':8080 '", timeout=5.0, interval=0.5
    )


def test_guest_start_rejects_invalid_readiness_timing() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="readiness timeout must be greater than zero"):
        ReadyParams.model_validate({"script": "true", "timeout": "0s"})


@pytest.mark.asyncio
async def test_guest_status_raises_when_process_state_differs(tmp_path) -> None:
    from labfoundry.errors import GuestExecutionError

    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        process_running=AsyncMock(return_value=False),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    event = CompiledEvent(
        sequence=0,
        id="status",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.status",
        **{"from": "ws"},
        parameters={"id": "browser", "running": True},
    )

    with pytest.raises(GuestExecutionError, match="expected True"):
        await dispatcher.handle_event(event)


@pytest.mark.asyncio
async def test_event_condition_uses_prior_guest_command_result(tmp_path) -> None:
    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        exec_shell=AsyncMock(return_value=ExecResult(0, "service ready", "")),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    probe = CompiledEvent(
        sequence=0,
        id="probe",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.exec",
        **{"from": "ws"},
        parameters={"script": "check", "check": False},
    )
    follow_up = CompiledEvent(
        sequence=1,
        id="follow-up",
        timestamp=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
        action="guest.exec",
        **{"from": "ws"},
        parameters={"script": "continue"},
        when=CompiledEventCondition(event="probe", exit_code=0, stdout_matches="ready"),
    )

    dispatcher.prepare_events((probe, follow_up))
    await dispatcher.handle_event(probe)
    await dispatcher.handle_event(follow_up)

    assert guests.exec_shell.await_count == 2


@pytest.mark.asyncio
async def test_failed_event_condition_skips_follow_up(tmp_path) -> None:
    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        exec_shell=AsyncMock(return_value=ExecResult(1, "", "not ready")),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    probe = CompiledEvent(
        sequence=0,
        id="probe",
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        action="guest.exec",
        **{"from": "ws"},
        parameters={"script": "check", "check": False},
    )
    follow_up = CompiledEvent(
        sequence=1,
        id="follow-up",
        timestamp=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
        action="guest.exec",
        **{"from": "ws"},
        parameters={"script": "continue"},
        when=CompiledEventCondition(event="probe", exit_code=0),
    )

    dispatcher.prepare_events((probe, follow_up))
    await dispatcher.handle_event(probe)
    await dispatcher.handle_event(follow_up)

    assert guests.exec_shell.await_count == 1


@pytest.mark.asyncio
async def test_recurring_actions_support_generic_guest_primitives(tmp_path) -> None:
    guests = SimpleNamespace(
        plan=SimpleNamespace(
            root_seed=1,
            scenario_id="case",
            scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        node_ip=lambda node: "10.0.0.10",
        has_profile=lambda node: node == "ws",
        mkdir=AsyncMock(),
    )
    dispatcher = GuestActionDispatcher(guests, source_root=tmp_path, run_dir=tmp_path / "run")
    action = CompiledTrafficAction(
        assignment="profile",
        node="ws",
        action="guest.mkdir",
        distribution="fixed",
        interval_seconds=1.0,
        parameters={"path": "/tmp/traffic"},
        seed=1,
    )

    dispatcher.validate_traffic(action)
    await dispatcher.handle_traffic(action)

    guests.mkdir.assert_awaited_once_with("ws", "/tmp/traffic")
