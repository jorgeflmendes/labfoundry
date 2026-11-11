"""Guest profile resolution, generic shell execution, processes and network provisioning."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import shlex
import uuid
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.plan import CompiledInterface, ScenarioPlan
from labfoundry.errors import GuestConfigurationError, GuestExecutionError
from labfoundry.guest.base import (
    ExecResult,
    GuestCommand,
    GuestExecutor,
    HostClockControl,
    ProcessHandle,
    SharedClockControl,
)
from labfoundry.guest.config import GuestInventory, GuestProfile
from labfoundry.guest.factory import DefaultGuestExecutorFactory, GuestExecutorFactory


class GuestManager:
    """Resolve per-node guest transports and expose general-purpose execution primitives."""

    def __init__(
        self,
        plan: ScenarioPlan,
        inventory: GuestInventory,
        *,
        base_dir: Path,
        executor_factory: GuestExecutorFactory | None = None,
    ) -> None:
        self.plan = plan
        self.inventory = inventory
        self.base_dir = base_dir
        self.executor_factory = (
            DefaultGuestExecutorFactory(inventory, base_dir=base_dir)
            if executor_factory is None
            else executor_factory
        )
        self._nodes = {node.name: node for node in plan.nodes}
        self._executors: dict[str, GuestExecutor] = {}
        self._processes: dict[tuple[str, str], ProcessHandle] = {}
        self._deployment: DeploymentState | None = None
        self.validate_profiles()

    def validate_profiles(self) -> None:
        """Validate profile references and secrets without requiring a deployed project."""
        for node in self.plan.nodes:
            if node.guest_profile is None:
                continue
            profile = self.inventory.profiles.get(node.guest_profile)
            if profile is None:
                raise GuestConfigurationError(
                    f"node {node.name!r} references missing guest profile {node.guest_profile!r}"
                )
            self.executor_factory.validate(node, profile)

    def has_profile(self, node: str) -> bool:
        """Return whether one compiled node has a configured guest profile."""
        compiled = self._nodes.get(node)
        return compiled is not None and compiled.guest_profile is not None

    async def set_clock_all(self, value: datetime) -> None:
        """Set guest wall clocks concurrently to limit inter-node skew."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("guest clock value must be timezone-aware")
        epoch = value.timestamp()
        domains: set[str] = set()

        async def synchronize(node: str) -> None:
            executor = self.executor(node)
            if isinstance(executor, SharedClockControl):
                if executor.clock_domain not in domains:
                    domains.add(executor.clock_domain)
                    await executor.set_clock(value)
                return
            profile = self.profile(node)
            if profile.os == "windows":
                milliseconds = round(epoch * 1000)
                await self.exec_shell(
                    node,
                    "$value = [DateTimeOffset]::FromUnixTimeMilliseconds("
                    f"{milliseconds}).LocalDateTime; Set-Date -Date $value",
                    timeout=30.0,
                )
            else:
                await self._exec_linux_privileged_shell(
                    node,
                    "timedatectl set-ntp false >/dev/null 2>&1 || true; "
                    f"date --set=@{epoch:.6f} >/dev/null",
                    timeout=30.0,
                )

        async with asyncio.TaskGroup() as group:
            for node in self.plan.nodes:
                if node.guest_profile is not None:
                    group.create_task(synchronize(node.name))

    async def settle_pending_connections(self, *, timeout: float = 130.0) -> bool:
        """Wait for Linux SYN-SENT sockets to clear before a time warp.

        Windows guests are not inspected. Return false if connections remain at the
        deadline; transport failures propagate instead of counting as settled sockets.
        """

        # SYN-SENT sockets cannot be destroyed through SOCK_DESTROY on all kernels.
        async def pending(node: str) -> bool:
            if self.profile(node).os == "windows":
                return False
            attempt = 0
            while True:
                try:
                    result = await self._exec_linux_privileged_shell(
                        node,
                        "ss -H -t state syn-sent || exit; "
                        "namespaces=$(ip netns list) || exit; "
                        "printf '%s\n' \"$namespaces\" | while read -r namespace rest; do "
                        '[ -n "$namespace" ] || continue; '
                        'ip netns exec "$namespace" ss -H -t state syn-sent || exit; done',
                        timeout=30.0,
                    )
                    return bool(result.stdout.strip())
                except GuestExecutionError as exc:
                    if attempt == 2 or (
                        "Error starting guest session (current status is: starting)" not in str(exc)
                    ):
                        raise GuestExecutionError(
                            f"cannot inspect pending connections on {node!r}: {exc}"
                        ) from exc
                    attempt += 1
                    await asyncio.sleep(1.0)

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(pending(node.name))
                    for node in self.plan.nodes
                    if node.guest_profile is not None
                ]
            if not any(task.result() for task in tasks):
                return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(1.0, remaining))

    async def check_identity_uniqueness(self) -> None:
        """Reject Linux VM clones sharing machine IDs or SSH host public keys."""
        seen: dict[tuple[str, str], str] = {}
        for node in self.plan.nodes:
            if node.guest_profile is None:
                continue
            profile = self.profile(node.name)
            if not profile.check_identity or profile.os != "linux":
                continue
            result = await self.exec_shell(
                node.name,
                "cat /etc/machine-id; "
                "for key in /etc/ssh/ssh_host_*_key.pub; do "
                'if test -f "$key"; then cat "$key"; fi; done',
                timeout=30.0,
            )
            lines = result.stdout.splitlines()
            machine_id = lines[0].strip().lower() if lines else ""
            if len(machine_id) != 32 or any(char not in "0123456789abcdef" for char in machine_id):
                raise GuestConfigurationError(f"node {node.name!r} has no valid machine-id")
            identities = [("machine-id", machine_id)]
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    digest = hashlib.sha256(" ".join(parts[:2]).encode()).hexdigest()
                    identities.append(("SSH host key", digest))
            for identity in identities:
                other = seen.setdefault(identity, node.name)
                if other != node.name:
                    raise GuestConfigurationError(
                        f"nodes {other!r} and {node.name!r} share a {identity[0]}; "
                        "provision distinct clone identities before capturing"
                    )

    def bind_deployment(self, deployment: DeploymentState) -> None:
        """Bind deployment-specific identifiers and construct concrete executors."""
        self._deployment = deployment
        self._executors.clear()
        for node in self.plan.nodes:
            if node.guest_profile is None:
                continue
            profile = self.inventory.profiles.get(node.guest_profile)
            if profile is None:
                raise GuestConfigurationError(
                    f"node {node.name!r} references missing guest profile {node.guest_profile!r}"
                )
            self._executors[node.name] = self.executor_factory.create(node, profile, deployment)

    async def prepare_clock_control(self) -> None:
        """Disable hypervisor clock correction before synchronized guests are started."""
        for executor in self._executors.values():
            if isinstance(executor, HostClockControl):
                await executor.disable_host_time_sync()

    def node_ip(self, node: str, *, peer: str | None = None) -> str | None:
        """Return a node IPv4 address, preferring a segment shared with the peer."""
        try:
            compiled = self._nodes[node]
        except KeyError as exc:
            raise GuestConfigurationError(f"unknown compiled node {node!r}") from exc

        if peer is not None and peer in self._nodes:
            peer_segments = {iface.segment for iface in self._nodes[peer].interfaces}
            for interface in compiled.interfaces:
                if interface.segment in peer_segments and interface.address != "dhcp":
                    return str(ipaddress.ip_interface(interface.address).ip)

        for interface in compiled.interfaces:
            if interface.address != "dhcp":
                return str(ipaddress.ip_interface(interface.address).ip)
        return None

    def executor(self, node: str) -> GuestExecutor:
        """Return the configured executor for one node or fail before an action is attempted."""
        if not self.has_profile(node):
            raise GuestConfigurationError(
                f"node {node!r} has no guest_profile and cannot execute guest actions"
            )
        try:
            return self._executors[node]
        except KeyError as exc:
            raise GuestConfigurationError(
                f"guest executors are not bound to a deployment for node {node!r}"
            ) from exc

    def profile(self, node: str) -> GuestProfile:
        """Return the validated guest profile bound to one compiled node."""
        compiled = self._nodes[node]
        if compiled.guest_profile is None:
            raise GuestConfigurationError(f"node {node!r} has no guest_profile")
        return self.inventory.profiles[compiled.guest_profile]

    async def healthcheck_all(self, *, timeout: float = 600.0, interval: float = 3.0) -> None:
        """Wait for every configured guest transport to become ready."""
        await self.healthcheck_nodes(self._executors, timeout=timeout, interval=interval)

    async def healthcheck_nodes(
        self,
        nodes: Iterable[str],
        *,
        timeout: float = 600.0,
        interval: float = 3.0,
    ) -> None:
        """Wait for selected configured guest transports to become ready."""
        if timeout <= 0:
            raise ValueError("guest healthcheck timeout must be greater than zero")
        if interval <= 0:
            raise ValueError("guest healthcheck interval must be greater than zero")

        async def wait_for_guest(node: str, executor: GuestExecutor) -> None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                try:
                    await executor.healthcheck()
                    return
                except GuestExecutionError as exc:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        raise GuestExecutionError(
                            f"guest {node!r} did not become ready within {timeout:g}s: {exc}"
                        ) from exc
                    await asyncio.sleep(min(interval, remaining))

        tasks = [
            asyncio.create_task(wait_for_guest(node, self._executors[node]))
            for node in nodes
            if node in self._executors
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _shell_command(
        self,
        node: str,
        script: str,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> GuestCommand:
        profile = self.profile(node)
        if profile.os == "windows":
            executable = profile.shell or (
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
            )
            args: tuple[str, ...] = ("-NoProfile", "-NonInteractive", "-Command", script)
        else:
            executable = profile.shell or "/bin/sh"
            args = ("-c", script)
        return GuestCommand(
            executable=executable,
            args=args,
            env=env or {},
            cwd=cwd,
            timeout=timeout,
            check=check,
        )

    async def exec_argv(
        self,
        node: str,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> ExecResult:
        """Execute an argv vector without a guest shell."""
        if not argv:
            raise GuestExecutionError("argv must contain at least one element")
        return await self.executor(node).exec(
            GuestCommand(
                executable=argv[0],
                args=argv[1:],
                env=env or {},
                cwd=cwd,
                timeout=timeout,
                check=check,
            )
        )

    async def exec_shell(
        self,
        node: str,
        script: str,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        check: bool = True,
    ) -> ExecResult:
        """Execute a shell or PowerShell script inside one guest."""
        return await self.executor(node).exec(
            self._shell_command(node, script, env=env, cwd=cwd, timeout=timeout, check=check)
        )

    async def upload(self, node: str, source: Path, destination: str) -> None:
        """Upload one regular file to a guest node."""
        if self.profile(node).os != "linux" or not self._linux_uses_sudo(node):
            await self.executor(node).upload(source, destination)
            return
        staging = f"/tmp/.labfoundry-upload-{uuid.uuid4().hex}"
        try:
            await self.executor(node).upload(source, staging)
            await self._exec_linux_privileged_argv(
                node,
                ("/usr/bin/install", "-D", "-m", "0644", "--", staging, destination),
                timeout=60.0,
            )
        finally:
            with suppress(Exception):
                await self._exec_linux_privileged_argv(
                    node,
                    ("/usr/bin/rm", "-f", "--", staging),
                    timeout=30.0,
                )

    async def download(self, node: str, source: str, destination: Path) -> None:
        """Download one regular file from a guest node."""
        await self.executor(node).download(source, destination)

    async def mkdir(self, node: str, path: str) -> None:
        """Create a guest directory and missing parents."""
        if self.profile(node).os == "linux" and self._linux_uses_sudo(node):
            await self._exec_linux_privileged_argv(
                node, ("/usr/bin/mkdir", "-p", "--", path), timeout=30.0
            )
            return
        await self.executor(node).mkdir(path)

    async def start_process(
        self,
        node: str,
        process_id: str,
        script: str,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> ProcessHandle:
        """Start a detached process and track its PID until stopped or scenario cleanup."""
        key = (node, process_id)
        if key in self._processes:
            raise GuestExecutionError(f"process {process_id!r} already exists on node {node!r}")
        profile = self.profile(node)
        if profile.os == "windows":
            child_parts: list[str] = []
            for name, value in (env or {}).items():
                escaped_name = name.replace("'", "''")
                escaped_value = value.replace("'", "''")
                child_parts.append(
                    "[Environment]::SetEnvironmentVariable("
                    f"'{escaped_name}','{escaped_value}','Process')"
                )
            if cwd:
                escaped_cwd = cwd.replace("'", "''")
                child_parts.append(f"Set-Location -LiteralPath '{escaped_cwd}'")
            child_parts.append(script)
            child_script = "; ".join(child_parts)
            encoded = base64.b64encode(child_script.encode("utf-16-le")).decode("ascii")
            wrapper = (
                "$p = Start-Process -FilePath 'powershell.exe' "
                f"-ArgumentList @('-NoProfile','-NonInteractive','-EncodedCommand','{encoded}') "
                "-PassThru -WindowStyle Hidden; $p.Id"
            )
        else:
            command = script
            if cwd:
                command = f"cd {shlex.quote(cwd)} && {command}"
            prefix = ""
            if env:
                prefix = (
                    "env "
                    + " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in env.items())
                    + " "
                )
            wrapper = (
                f"{prefix}nohup setsid /bin/sh -c {shlex.quote(command)} >/dev/null 2>&1 & echo $!"
            )
        result = await self.exec_shell(node, wrapper, timeout=30.0)
        try:
            pid = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise GuestExecutionError(
                f"could not parse PID for process {process_id!r} on {node!r}: {result.stdout!r}"
            ) from exc
        handle = ProcessHandle(node=node, process_id=process_id, pid=pid)
        self._processes[key] = handle
        await asyncio.sleep(0.1)
        if not await self.process_running(node, process_id):
            self._processes.pop(key, None)
            raise GuestExecutionError(f"process {process_id!r} exited immediately on node {node!r}")
        return handle

    async def process_running(self, node: str, process_id: str) -> bool:
        """Return whether one tracked detached process is still alive in the guest."""
        key = (node, process_id)
        try:
            handle = self._processes[key]
        except KeyError as exc:
            raise GuestExecutionError(f"unknown process {process_id!r} on node {node!r}") from exc
        profile = self.profile(node)
        if profile.os == "windows":
            script = (
                f"if (Get-Process -Id {handle.pid} -ErrorAction SilentlyContinue) "
                "{ exit 0 } else { exit 1 }"
            )
        else:
            script = f"ps -o stat= -p {handle.pid} | grep -q '^[^Z]'"
        result = await self.exec_shell(node, script, timeout=15.0, check=False)
        return result.exit_code == 0

    async def wait_process_ready(
        self,
        node: str,
        process_id: str,
        script: str,
        *,
        timeout: float,
        interval: float,
    ) -> None:
        """Wait for a guest-side service probe while checking its process remains alive."""
        if timeout <= 0:
            raise GuestExecutionError("readiness timeout must be greater than zero")
        if interval <= 0:
            raise GuestExecutionError("readiness interval must be greater than zero")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_result: ExecResult | None = None
        last_error: GuestExecutionError | None = None
        while True:
            if not await self.process_running(node, process_id):
                raise GuestExecutionError(
                    f"process {process_id!r} on {node!r} exited before it was ready"
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                detail = ""
                if last_result is not None:
                    output = (last_result.stderr or last_result.stdout).strip()
                    detail = f"; last probe exited {last_result.exit_code}: {output[-500:]}"
                elif last_error is not None:
                    detail = f"; last probe failed: {last_error}"
                raise GuestExecutionError(
                    f"process {process_id!r} on {node!r} was not ready within {timeout}s{detail}"
                )
            try:
                last_result = await self.exec_shell(
                    node, script, timeout=min(remaining, 10.0), check=False
                )
            except GuestExecutionError as exc:
                last_result = None
                last_error = exc
            else:
                last_error = None
                if last_result.exit_code == 0:
                    return
            await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))

    async def wait_process(
        self,
        node: str,
        process_id: str,
        *,
        timeout: float,
        interval: float = 0.5,
    ) -> None:
        """Wait until one tracked detached process exits, then forget its runtime handle."""
        if timeout <= 0:
            raise GuestExecutionError("process wait timeout must be greater than zero")
        if interval <= 0:
            raise GuestExecutionError("process wait interval must be greater than zero")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while await self.process_running(node, process_id):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise GuestExecutionError(
                    f"process {process_id!r} on node {node!r} did not exit within {timeout}s"
                )
            await asyncio.sleep(min(interval, remaining))
        self._processes.pop((node, process_id), None)

    async def stop_process(self, node: str, process_id: str) -> None:
        """Stop one tracked detached process."""
        key = (node, process_id)
        try:
            handle = self._processes.pop(key)
        except KeyError as exc:
            raise GuestExecutionError(f"unknown process {process_id!r} on node {node!r}") from exc
        profile = self.profile(node)
        if profile.os == "windows":
            script = f"taskkill.exe /PID {handle.pid} /T /F | Out-Null"
        else:
            script = f"kill -- -{handle.pid} 2>/dev/null || kill {handle.pid} 2>/dev/null || true"
        await self.exec_shell(node, script, timeout=30.0, check=False)

    async def stop_all_processes(self) -> None:
        """Best-effort cleanup of all detached processes started by this manager."""
        keys = list(self._processes)
        for node, process_id in reversed(keys):
            with suppress(Exception):
                await self.stop_process(node, process_id)

    def forget_processes(self, nodes: Iterable[str]) -> None:
        """Forget process handles invalidated by a guest shutdown."""
        stopped = set(nodes)
        for key in list(self._processes):
            if key[0] in stopped:
                self._processes.pop(key)

    async def refresh_nodes(self, nodes: Iterable[str]) -> None:
        """Wait for restarted guests and reapply networking without rerunning provision actions."""
        selected = tuple(nodes)
        await self.healthcheck_nodes(selected)
        await self.configure_network(selected)

    async def configure_network(self, nodes: Iterable[str] | None = None) -> None:
        """Apply compiled IPv4 configuration to guests that have a guest profile."""
        selected = None if nodes is None else set(nodes)
        for node in self.plan.nodes:
            if selected is not None and node.name not in selected:
                continue
            if node.name not in self._executors:
                continue
            profile = self.profile(node.name)
            if profile.os == "linux" and profile.manage_hostname:
                hostname = node.name.lower().replace("_", "-")[:63].strip("-")
                await self._exec_linux_privileged_argv(
                    node.name,
                    ("/usr/bin/hostnamectl", "set-hostname", hostname),
                    timeout=30.0,
                )
                quoted_hostname = shlex.quote(hostname)
                await self._exec_linux_privileged_shell(
                    node.name,
                    f"getent -s files hosts {quoted_hostname} >/dev/null || "
                    f"printf '127.0.1.1 %s\\n' {quoted_hostname} >> /etc/hosts",
                    timeout=30.0,
                )
            for interface in node.interfaces:
                guest_name = profile.interface_map.get(interface.name)
                if guest_name is None:
                    raise GuestConfigurationError(
                        f"guest profile {node.guest_profile!r} lacks interface_map entry "
                        f"for {node.name}.{interface.name}"
                    )
                if profile.os == "windows":
                    await self._configure_windows_interface(node.name, guest_name, interface)
                else:
                    await self._configure_linux_interface(node.name, guest_name, interface)
            if node.ipv4_forwarding:
                if profile.os == "windows":
                    await self.exec_shell(
                        node.name,
                        "Set-NetIPInterface -AddressFamily IPv4 -Forwarding Enabled",
                        timeout=30.0,
                    )
                else:
                    await self._exec_linux_privileged_argv(
                        node.name,
                        ("/usr/sbin/sysctl", "-w", "net.ipv4.ip_forward=1"),
                        timeout=30.0,
                    )

    def _linux_uses_sudo(self, node: str) -> bool:
        return self.profile(node).requires_sudo

    async def _exec_linux_privileged_argv(
        self, node: str, argv: tuple[str, ...], *, timeout: float
    ) -> ExecResult:
        if self._linux_uses_sudo(node):
            argv = ("/usr/bin/sudo", "-n", "--", *argv)
        return await self.exec_argv(node, argv, timeout=timeout)

    async def _exec_linux_privileged_shell(
        self, node: str, script: str, *, timeout: float
    ) -> ExecResult:
        if self._linux_uses_sudo(node):
            return await self.exec_argv(
                node,
                ("/usr/bin/sudo", "-n", "/bin/sh", "-c", script),
                timeout=timeout,
            )
        return await self.exec_shell(node, script, timeout=timeout)

    async def _configure_linux_interface(
        self, node: str, name: str, interface: CompiledInterface
    ) -> None:
        profile = self.profile(node)
        if profile.network_control == "exclusive":
            quoted_name = shlex.quote(name)
            await self._exec_linux_privileged_shell(
                node,
                "systemctl stop NetworkManager.service systemd-networkd.service "
                "systemd-networkd.socket >/dev/null 2>&1 || true; "
                "if command -v nmcli >/dev/null 2>&1; then "
                f"nmcli device set {quoted_name} managed no >/dev/null 2>&1 || true; fi; "
                "if command -v networkctl >/dev/null 2>&1; then "
                f"networkctl down {quoted_name} >/dev/null 2>&1 || true; fi; "
                f"pkill -f '[d]hclient.*{quoted_name}' >/dev/null 2>&1 || true; "
                f"ip addr flush dev {quoted_name}",
                timeout=30.0,
            )
        if interface.mac:
            await self._exec_linux_privileged_argv(
                node, ("/usr/sbin/ip", "link", "set", "dev", name, "down"), timeout=30.0
            )
            await self._exec_linux_privileged_argv(
                node,
                ("/usr/sbin/ip", "link", "set", "dev", name, "address", interface.mac),
                timeout=30.0,
            )
        await self._exec_linux_privileged_argv(
            node, ("/usr/sbin/ip", "link", "set", "dev", name, "up"), timeout=30.0
        )
        if interface.address == "dhcp":
            hostname = node.lower().replace("_", "-")[:63].strip("-")
            command = profile.dhcp_command.replace("{interface}", shlex.quote(name)).replace(
                "{hostname}", shlex.quote(hostname)
            )
            await self._exec_linux_privileged_shell(node, command, timeout=60.0)
        else:
            await self._exec_linux_privileged_argv(
                node, ("/usr/sbin/ip", "addr", "flush", "dev", name), timeout=30.0
            )
            await self._exec_linux_privileged_argv(
                node,
                ("/usr/sbin/ip", "addr", "add", interface.address, "dev", name),
                timeout=30.0,
            )
            if interface.gateway:
                await self._exec_linux_privileged_argv(
                    node,
                    (
                        "/usr/sbin/ip",
                        "route",
                        "replace",
                        "default",
                        "via",
                        interface.gateway,
                        "dev",
                        name,
                    ),
                    timeout=30.0,
                )
        if interface.dns:
            content = "".join(f"nameserver {server}\n" for server in interface.dns)
            servers = " ".join(shlex.quote(server) for server in interface.dns)
            await self._exec_linux_privileged_shell(
                node,
                "if command -v resolvectl >/dev/null 2>&1 && "
                "systemctl is-active --quiet systemd-resolved.service; then "
                f"resolvectl dns {shlex.quote(name)} {servers} && "
                f"resolvectl domain {shlex.quote(name)} '~.'; "
                f"else printf %s {shlex.quote(content)} > /etc/resolv.conf; fi",
                timeout=30.0,
            )

    async def _configure_windows_interface(
        self, node: str, name: str, interface: CompiledInterface
    ) -> None:
        quoted_name = name.replace('"', '\\"')
        if interface.address == "dhcp":
            script = (
                f'netsh interface ipv4 set address name="{quoted_name}" source=dhcp; '
                f'netsh interface ipv4 set dnsservers name="{quoted_name}" source=dhcp'
            )
            await self.exec_shell(node, script, timeout=60.0)
            if interface.dns:
                first, *rest = interface.dns
                await self.exec_shell(
                    node,
                    f'netsh interface ipv4 set dnsservers name="{quoted_name}" '
                    f"source=static address={first} validate=no",
                    timeout=30.0,
                )
                for index, server in enumerate(rest, start=2):
                    await self.exec_shell(
                        node,
                        f'netsh interface ipv4 add dnsservers name="{quoted_name}" '
                        f"address={server} index={index} validate=no",
                        timeout=30.0,
                    )
            return
        parsed = ipaddress.ip_interface(interface.address)
        mask = parsed.network.netmask
        gateway = interface.gateway or "none"
        script = (
            f'netsh interface ipv4 set address name="{quoted_name}" source=static '
            f"address={parsed.ip} mask={mask} gateway={gateway}"
        )
        await self.exec_shell(node, script, timeout=60.0)
        if interface.dns:
            first, *rest = interface.dns
            await self.exec_shell(
                node,
                f'netsh interface ipv4 set dnsservers name="{quoted_name}" '
                f"source=static address={first} validate=no",
                timeout=30.0,
            )
            for index, server in enumerate(rest, start=2):
                await self.exec_shell(
                    node,
                    f'netsh interface ipv4 add dnsservers name="{quoted_name}" '
                    f"address={server} index={index} validate=no",
                    timeout=30.0,
                )
