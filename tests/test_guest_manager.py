from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.plan import CompiledInterface, CompiledNode, ScenarioPlan
from labfoundry.errors import GuestConfigurationError, GuestExecutionError
from labfoundry.guest.base import ExecResult
from labfoundry.guest.config import (
    GuestInventory,
    SSHGuestProfile,
    VirtualBoxGuestProfile,
)
from labfoundry.guest.factory import GuestExecutorFactory
from labfoundry.guest.manager import GuestManager


def _plan(*, windows: bool = False, dhcp: bool = False, forwarding: bool = False) -> ScenarioPlan:
    address = "dhcp" if dhcp else "10.10.10.20/24"
    interface = CompiledInterface(
        name="eth0",
        segment="users",
        address=address,
        gateway=None if dhcp else "10.10.10.1",
        dns=("1.1.1.1",),
        adapter=0,
        port=0,
    )
    node = CompiledNode(
        name="ws",
        group="ws",
        template="template",
        expected_template_type="virtualbox",
        interfaces=(interface,),
        guest_profile="guest",
        ipv4_forwarding=forwarding,
        x=0,
        y=0,
    )
    return ScenarioPlan(
        scenario_id="case",
        root_seed=1,
        scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        clock_speed=1.0,
        segments=(),
        nodes=(node,),
        links=(),
        events=(),
        traffic=(),
        captures=(),
        aliases={},
    )


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    windows: bool = False,
    dhcp: bool = False,
    forwarding: bool = False,
) -> GuestManager:
    monkeypatch.setenv("LF_TEST_PASSWORD", "secret")
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="windows" if windows else "linux",
        username="rf",
        password_env="LF_TEST_PASSWORD",
        interface_map={"eth0": "Ethernet" if windows else "enp0s3"},
        dhcp_command="udhcpc -i {interface}",
    )
    manager = GuestManager(
        _plan(windows=windows, dhcp=dhcp, forwarding=forwarding),
        GuestInventory(profiles={"guest": profile}),
        base_dir=Path("."),
    )
    manager._executors["ws"] = AsyncMock()
    return manager


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_control", [False, True])
async def test_manager_uses_injected_executor_factory(tmp_path: Path, clock_control: bool) -> None:
    plan = _plan()
    profile = VirtualBoxGuestProfile(os="linux", username="root")
    executor = SimpleNamespace(exec=AsyncMock(return_value=ExecResult(0, "custom", "")))
    if clock_control:
        executor.disable_host_time_sync = AsyncMock()
    factory = Mock(spec=GuestExecutorFactory)
    factory.create.return_value = executor
    manager = GuestManager(
        plan,
        GuestInventory(profiles={"guest": profile}),
        base_dir=tmp_path,
        executor_factory=factory,
    )
    deployment = DeploymentState(
        project_id="project",
        project_name="project",
        gns3_version="2.2.61",
    )

    manager.bind_deployment(deployment)
    await manager.prepare_clock_control()
    result = await manager.exec_argv("ws", ("custom-command",))

    factory.validate.assert_called_once_with(plan.nodes[0], profile)
    factory.create.assert_called_once_with(plan.nodes[0], profile, deployment)
    assert manager.executor("ws") is executor
    assert result.stdout == "custom"
    if clock_control:
        executor.disable_host_time_sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_guest_clocks_are_set_concurrently(monkeypatch) -> None:
    manager = _manager(monkeypatch)
    first = manager.plan.nodes[0]
    second = first.model_copy(update={"name": "second"})
    manager.plan = manager.plan.model_copy(update={"nodes": (first, second)})
    manager._nodes["second"] = second
    manager._executors["second"] = manager._executors[first.name]
    started = set()
    both_started = asyncio.Event()

    async def set_clock(node, script, *, timeout):
        started.add(node)
        if len(started) == 2:
            both_started.set()
        await both_started.wait()
        assert "date --set=@1735689600.000000" in script
        return ExecResult(0, "", "")

    manager._exec_linux_privileged_shell = AsyncMock(side_effect=set_clock)
    await asyncio.wait_for(manager.set_clock_all(manager.plan.scenario_start), timeout=1)
    assert started == {"ws", "second"}


@pytest.mark.asyncio
async def test_clock_failure_cancels_other_updates(monkeypatch) -> None:
    manager = _manager(monkeypatch)
    first = manager.plan.nodes[0]
    second = first.model_copy(update={"name": "second"})
    manager.plan = manager.plan.model_copy(update={"nodes": (first, second)})
    manager._nodes["second"] = second
    manager._executors["second"] = manager._executors[first.name]
    pending = asyncio.Event()
    cancelled = asyncio.Event()

    async def set_clock(node, script, *, timeout):
        if node == "ws":
            await pending.wait()
            raise GuestExecutionError("clock update failed")
        pending.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    manager._exec_linux_privileged_shell = AsyncMock(side_effect=set_clock)
    with pytest.raises(ExceptionGroup) as error:
        await asyncio.wait_for(manager.set_clock_all(manager.plan.scenario_start), timeout=1)
    assert isinstance(error.value.exceptions[0], GuestExecutionError)
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate", ["machine-id", "SSH host key", None])
async def test_clone_identity_uniqueness(monkeypatch, duplicate) -> None:
    manager = _manager(monkeypatch)
    first = manager.plan.nodes[0]
    manager.plan = manager.plan.model_copy(
        update={
            "nodes": (first, first.model_copy(update={"name": "second"})),
        }
    )
    manager._nodes["second"] = manager.plan.nodes[1]
    machine = "a" * 32 if duplicate == "machine-id" else "b" * 32
    key = "AAAA" if duplicate == "SSH host key" else "BBBB"
    manager.exec_shell = AsyncMock(
        side_effect=[
            ExecResult(0, "a" * 32 + "\nssh-ed25519 AAAA first\n", ""),
            ExecResult(0, machine + f"\nssh-ed25519 {key} second\n", ""),
        ]
    )
    if duplicate:
        with pytest.raises(GuestConfigurationError, match=f"share a {duplicate}"):
            await manager.check_identity_uniqueness()
    else:
        await manager.check_identity_uniqueness()
    assert manager.exec_shell.await_count == 2


def test_node_ip_returns_static_and_none_for_dhcp(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _manager(monkeypatch).node_ip("ws") == "10.10.10.20"
    assert _manager(monkeypatch, dhcp=True).node_ip("ws") is None


def test_ssh_management_address_cannot_use_managed_interface(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("test", encoding="utf-8")
    profile = SSHGuestProfile(
        username="root",
        host="10.10.10.20",
        identity_file=identity,
        strict_host_key_checking=False,
        interface_map={"eth0": "eth0"},
    )

    with pytest.raises(GuestConfigurationError, match="out-of-band management address"):
        GuestManager(
            _plan(),
            GuestInventory(profiles={"guest": profile}),
            base_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_healthcheck_waits_for_guest_additions(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch)
    executor = manager._executors["ws"]
    executor.healthcheck.side_effect = [GuestExecutionError("not ready"), None]

    await manager.healthcheck_all(timeout=1.0, interval=0.01)

    assert executor.healthcheck.await_count == 2


@pytest.mark.asyncio
async def test_healthcheck_does_not_retry_invalid_credentials(monkeypatch):
    manager = _manager(monkeypatch)
    executor = manager._executors["ws"]
    executor.healthcheck.side_effect = GuestConfigurationError("rejected login")

    with pytest.raises(GuestConfigurationError, match="rejected login"):
        await manager.healthcheck_all(timeout=1.0, interval=0.01)
    assert executor.healthcheck.await_count == 1


@pytest.mark.asyncio
async def test_healthcheck_cancels_other_guests_after_configuration_error(monkeypatch):
    manager = _manager(monkeypatch)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def rejected():
        await started.wait()
        raise GuestConfigurationError("rejected login")

    manager._executors["ws"].healthcheck.side_effect = rejected
    manager._executors["other"] = AsyncMock()
    manager._executors["other"].healthcheck.side_effect = pending

    with pytest.raises(GuestConfigurationError, match="rejected login"):
        await manager.healthcheck_all(timeout=1.0, interval=0.01)

    assert cancelled.is_set()
    assert manager._executors["other"].healthcheck.await_count == 1


@pytest.mark.asyncio
async def test_healthcheck_timeout_is_reported_as_guest_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(monkeypatch)
    executor = manager._executors["ws"]
    executor.healthcheck.side_effect = GuestExecutionError("not ready")

    with pytest.raises(GuestExecutionError, match="guest 'ws' did not become ready"):
        await manager.healthcheck_all(timeout=0.01, interval=0.01)


@pytest.mark.asyncio
async def test_linux_upload_stages_before_privileged_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manager = _manager(monkeypatch)
    source = tmp_path / "payload.txt"
    source.write_text("evidence", encoding="utf-8")
    manager.exec_argv = AsyncMock(return_value=ExecResult(0, "", ""))  # type: ignore[method-assign]

    await manager.upload("ws", source, "/opt/labfoundry/payload.txt")

    executor = manager._executors["ws"]
    uploaded_to = executor.upload.await_args.args[1]
    assert uploaded_to.startswith("/tmp/.labfoundry-upload-")
    argv_calls = [call.args[1] for call in manager.exec_argv.await_args_list]
    assert (
        "/usr/bin/sudo",
        "-n",
        "--",
        "/usr/bin/install",
        "-D",
        "-m",
        "0644",
        "--",
        uploaded_to,
        "/opt/labfoundry/payload.txt",
    ) in argv_calls
    assert ("/usr/bin/sudo", "-n", "--", "/usr/bin/rm", "-f", "--", uploaded_to) in argv_calls


@pytest.mark.asyncio
async def test_linux_network_configuration_uses_compiled_addressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(monkeypatch, forwarding=True)
    manager.exec_argv = AsyncMock(return_value=ExecResult(0, "", ""))  # type: ignore[method-assign]
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecResult(0, "", "")
    )

    await manager.configure_network()

    argv_calls = [call.args[1] for call in manager.exec_argv.await_args_list]
    sudo = ("/usr/bin/sudo", "-n", "--")
    assert (*sudo, "/usr/bin/hostnamectl", "set-hostname", "ws") in argv_calls
    assert (*sudo, "/usr/sbin/ip", "link", "set", "dev", "enp0s3", "up") in argv_calls
    assert (*sudo, "/usr/sbin/ip", "addr", "flush", "dev", "enp0s3") in argv_calls
    assert (
        *sudo,
        "/usr/sbin/ip",
        "addr",
        "add",
        "10.10.10.20/24",
        "dev",
        "enp0s3",
    ) in argv_calls
    assert (
        *sudo,
        "/usr/sbin/ip",
        "route",
        "replace",
        "default",
        "via",
        "10.10.10.1",
        "dev",
        "enp0s3",
    ) in argv_calls
    assert (
        *sudo,
        "/usr/sbin/sysctl",
        "-w",
        "net.ipv4.ip_forward=1",
    ) in argv_calls
    privileged_scripts = [
        call.args[1][-1]
        for call in manager.exec_argv.await_args_list
        if call.args[1][:4] == ("/usr/bin/sudo", "-n", "/bin/sh", "-c")
    ]
    hosts_script = next(script for script in privileged_scripts if "/etc/hosts" in script)
    assert "getent -s files hosts ws" in hosts_script
    assert "127.0.1.1" in hosts_script
    resolver_script = next(script for script in privileged_scripts if "/etc/resolv.conf" in script)
    assert "resolvectl dns enp0s3 1.1.1.1" in resolver_script
    assert "resolvectl domain enp0s3 '~.'" in resolver_script
    assert "nameserver 1.1.1.1\n" in resolver_script
    assert r"1.1.1.1\n" not in resolver_script


@pytest.mark.asyncio
async def test_linux_dhcp_uses_profile_command(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _manager(monkeypatch, dhcp=True)
    manager.exec_argv = AsyncMock(return_value=ExecResult(0, "", ""))  # type: ignore[method-assign]
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecResult(0, "", "")
    )

    await manager.configure_network()

    argv_calls = [call.args[1] for call in manager.exec_argv.await_args_list]
    assert (
        "/usr/bin/sudo",
        "-n",
        "/bin/sh",
        "-c",
        "udhcpc -i enp0s3",
    ) in argv_calls


@pytest.mark.asyncio
async def test_windows_network_configuration_uses_netsh_and_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(monkeypatch, windows=True, forwarding=True)
    manager.exec_shell = AsyncMock(  # type: ignore[method-assign]
        return_value=ExecResult(0, "", "")
    )

    await manager.configure_network()

    scripts = [call.args[1] for call in manager.exec_shell.await_args_list]
    assert any("source=static" in script and "10.10.10.20" in script for script in scripts)
    assert any("dnsservers" in script and "1.1.1.1" in script for script in scripts)
    assert any("Set-NetIPInterface" in script for script in scripts)


@pytest.mark.asyncio
async def test_missing_interface_mapping_fails_before_guest_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LF_TEST_PASSWORD", "secret")
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="linux",
        username="rf",
        password_env="LF_TEST_PASSWORD",
    )
    manager = GuestManager(
        _plan(),
        GuestInventory(profiles={"guest": profile}),
        base_dir=Path("."),
    )
    manager._executors["ws"] = AsyncMock()

    with pytest.raises(GuestConfigurationError, match="lacks interface_map"):
        await manager.configure_network()


@pytest.mark.asyncio
async def test_time_warp_checks_pending_connections_in_all_network_namespaces(monkeypatch) -> None:
    manager = _manager(monkeypatch)
    manager._exec_linux_privileged_shell = AsyncMock(return_value=ExecResult(0, "", ""))
    assert await manager.settle_pending_connections()
    script = manager._exec_linux_privileged_shell.await_args.args[1]
    assert "ss -H -t state syn-sent" in script
    assert "ip netns list" in script
    assert 'ip netns exec "$namespace" ss -H -t state syn-sent' in script
    assert "ss -K" not in script


@pytest.mark.asyncio
async def test_time_warp_reports_unsettled_tcp_connections(monkeypatch) -> None:
    manager = _manager(monkeypatch)
    manager._exec_linux_privileged_shell = AsyncMock(return_value=ExecResult(0, "pending", ""))
    assert not await manager.settle_pending_connections(timeout=0)


@pytest.mark.asyncio
async def test_pending_connections_retries_session_start_failure(monkeypatch) -> None:
    manager = _manager(monkeypatch)
    manager._exec_linux_privileged_shell = AsyncMock(
        side_effect=[
            GuestExecutionError("Error starting guest session (current status is: starting)"),
            ExecResult(0, "", ""),
        ]
    )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    assert await manager.settle_pending_connections()
    assert manager._exec_linux_privileged_shell.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message, attempts",
    [
        ("Error starting guest session (current status is: starting)", 3),
        ("the specified user was not able to logon", 1),
    ],
)
async def test_pending_connections_session_failure_is_bounded(
    monkeypatch,
    message,
    attempts,
) -> None:
    manager = _manager(monkeypatch)
    manager._exec_linux_privileged_shell = AsyncMock(side_effect=GuestExecutionError(message))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    with pytest.raises(ExceptionGroup) as error:
        await manager.settle_pending_connections()
    assert "pending connections on 'ws'" in str(error.value.exceptions[0])
    assert manager._exec_linux_privileged_shell.await_count == attempts
