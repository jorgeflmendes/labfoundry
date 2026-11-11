"""One Linux VM per scenario, with unprivileged Incus nodes behind UDP Ethernet tunnels."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import socket
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from labfoundry.compiler.plan import CompiledNode, ScenarioPlan
from labfoundry.errors import BackendError, GuestExecutionError
from labfoundry.guest import _incus_host
from labfoundry.guest.base import ExecResult, GuestCommand
from labfoundry.guest.config import (
    GuestInventory,
    IncusGuestProfile,
    VirtualBoxGuestProfile,
)
from labfoundry.guest.subprocess import run_host_command
from labfoundry.guest.virtualbox import VirtualBoxGuestExecutor


def _available_memory_mib() -> int:
    if sys.platform == "win32":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total",
                    "available",
                    "pagefile",
                    "available_pagefile",
                    "virtual",
                    "available_virtual",
                    "extended",
                )
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise BackendError("cannot read available host memory")
        return int(status.available) // 1048576
    if sys.platform.startswith("linux"):
        fields = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return int(fields["MemAvailable"].split()[0]) // 1024
    raise BackendError("scenario VMs require a Windows or Linux VirtualBox host")


class IncusEnvironment:
    """Own a linked VM clone and its host-only tunnel allocation."""

    def __init__(self, name: str, inventory: GuestInventory, *, base_dir: Path) -> None:
        self.name = name
        self.inventory = inventory
        self.profile = inventory.environments[name]
        control = inventory.profiles[self.profile.guest_profile]
        if not isinstance(control, VirtualBoxGuestProfile) or control.os != "linux":
            raise BackendError("Incus environment control requires Linux Guest Additions")
        self.control = control
        self.password = inventory.resolve_secret(control, base_dir=base_dir)
        self.vboxmanage = VirtualBoxGuestExecutor._resolve_vboxmanage(control.vboxmanage)
        self._resume_blocked = False

    @classmethod
    def for_plan(
        cls, plan: ScenarioPlan, inventory: GuestInventory, *, base_dir: Path
    ) -> IncusEnvironment | None:
        """Resolve the single environment selected by a plan's Incus profiles."""
        names = set()
        for node in plan.nodes:
            profile = inventory.profiles.get(node.guest_profile or "")
            if isinstance(profile, IncusGuestProfile):
                if node.expected_template_type != "incus":
                    raise BackendError(f"Incus node {node.name!r} requires template_type: incus")
                names.add(profile.environment)
            elif node.expected_template_type == "incus":
                raise BackendError(f"node {node.name!r} requires an Incus guest profile")
        if len(names) > 1:
            raise BackendError("all Incus nodes in a scenario must share one environment")
        return cls(names.pop(), inventory, base_dir=base_dir) if names else None

    async def _vbox(self, *args: str, timeout: float = 60) -> ExecResult:
        return await run_host_command((self.vboxmanage, *args), timeout=timeout)

    async def preflight(self, plan: ScenarioPlan) -> None:
        """Check host resources, host-only addressing and container interface mappings."""
        if self.profile.cpus > (os.cpu_count() or 1):
            raise BackendError("scenario VM CPUs exceed the host's logical CPU count")
        available = _available_memory_mib()
        required = self.profile.memory_mib + self.profile.reserve_memory_mib
        if available < required:
            raise BackendError(
                f"scenario VM needs {required} MiB free host RAM; {available} MiB available"
            )
        interfaces = (await self._vbox("list", "hostonlyifs")).stdout
        matching = [
            block
            for block in re.split(r"\n\s*\n", interfaces)
            if re.search(
                r"^Name:\s+" + re.escape(self.profile.host_only_adapter) + r"\s*$",
                block,
                re.MULTILINE,
            )
        ]
        if len(matching) != 1 or not re.search(
            r"^IPAddress:\s+" + re.escape(str(self.profile.host_address)) + r"\s*$",
            matching[0],
            re.MULTILINE,
        ):
            raise BackendError("configured host-only adapter/address was not found in VirtualBox")
        for node in plan.nodes:
            if node.expected_template_type != "incus":
                continue
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]*", node.template):
                raise BackendError("Incus templates must name a local image alias or fingerprint")
            profile = self.inventory.profiles[node.guest_profile or ""]
            assert isinstance(profile, IncusGuestProfile)
            if profile.cpus is not None and profile.cpus > self.profile.cpus:
                raise BackendError(f"node {node.name!r} requests more CPUs than its scenario VM")
            for interface in node.interfaces:
                name = profile.interface_map.get(interface.name, f"eth{interface.adapter}")
                if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", name):
                    raise BackendError(f"invalid Incus interface name for node {node.name!r}")
        if (
            sum(
                len(node.interfaces)
                for node in plan.nodes
                if node.expected_template_type == "incus"
            )
            > 30000
        ):
            raise BackendError("too many Incus tunnel interfaces for one scenario VM")

    def _host(self, state: dict[str, Any]) -> VirtualBoxGuestExecutor:
        return VirtualBoxGuestExecutor(state["vm_name"], self.control, password=self.password)

    async def _root(
        self, state: dict[str, Any], args: tuple[str, ...], *, timeout: float = 60
    ) -> ExecResult:
        if self.control.requires_sudo:
            args = ("/usr/bin/sudo", "-n", "--", *args)
        return await self._host(state).exec(GuestCommand(args[0], args[1:], timeout=timeout))

    async def prepare(self, plan: ScenarioPlan, project_id: str) -> dict[str, Any]:
        """Reserve an address and clone the configured immutable base snapshot."""
        if not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", project_id):
            raise BackendError("invalid project identity for scenario VM resources")
        directory = Path.home() / ".labfoundry" / "incus" / "leases"
        directory.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "name": self.name,
            "vm_name": f"lf-incus-{project_id}",
            "owner": project_id,
            "nodes": {},
            "port_leases": [],
        }
        for address in self.profile.guest_addresses:
            lease = directory / f"{address.ip}.json"
            try:
                with lease.open("x", encoding="utf-8") as handle:
                    json.dump({"owner": project_id, "vm_name": state["vm_name"]}, handle)
            except FileExistsError:
                continue
            state.update(lease=str(lease), guest_address=str(address))
            break
        else:
            raise BackendError(
                "no free Incus scenario address; running or retained projects hold the leases"
            )
        try:
            await self._vbox(
                "clonevm",
                self.profile.vmname,
                "--snapshot",
                self.profile.snapshot,
                "--name",
                state["vm_name"],
                "--options",
                "link",
                "--register",
                timeout=180,
            )
            state["vm_uuid"] = state["vm_name"]
            info = (await self._vbox("showvminfo", state["vm_name"], "--machinereadable")).stdout
            match = re.search(r'^UUID="([0-9a-f-]+)"\s*$', info, re.MULTILINE)
            if match is None:
                raise BackendError("cloned scenario VM did not report its UUID")
            state["vm_uuid"] = match.group(1)
            options = [
                "modifyvm",
                state["vm_uuid"],
                "--memory",
                str(self.profile.memory_mib),
                "--cpus",
                str(self.profile.cpus),
                "--nic1",
                "hostonly",
                "--hostonlyadapter1",
                self.profile.host_only_adapter,
                "--nicpromisc1",
                "deny",
            ]
            for nic in re.findall(r"^nic([0-9]+)=", info, re.MULTILINE):
                if nic != "1":
                    options.extend((f"--nic{nic}", "none"))
            if self.profile.paravirt_provider is not None:
                options.extend(("--paravirtprovider", self.profile.paravirt_provider))
            await self._vbox(*options)
            await self._host(state).disable_host_time_sync()
            ports: set[int] = set()
            for node in plan.nodes:
                if node.expected_template_type == "incus":
                    state["nodes"][node.name] = self._node(node, project_id, ports, state)
            lease = Path(state["lease"])
            lease.write_text(json.dumps(state), encoding="utf-8")
            return state
        except BaseException as exc:
            try:
                await self.destroy(state)
            except Exception as cleanup:
                exc.add_note(f"scenario VM cleanup failed: {cleanup}")
            raise

    def _node(
        self, node: CompiledNode, owner: str, ports: set[int], state: dict[str, Any]
    ) -> dict[str, Any]:
        profile = self.inventory.profiles[node.guest_profile or ""]
        assert isinstance(profile, IncusGuestProfile)
        digest = hashlib.sha256(f"{owner}:{node.name}".encode()).hexdigest()[:12]
        interfaces = []
        directory = Path(state["lease"]).parent / "ports"
        directory.mkdir(exist_ok=True)
        for index, interface in enumerate(sorted(node.interfaces, key=lambda item: item.adapter)):
            for _ in range(128):
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
                    reservation.bind(("0.0.0.0", 0))
                    port = reservation.getsockname()[1]
                    if port in ports:
                        continue
                    lease = directory / f"{port}.json"
                    try:
                        with lease.open("x", encoding="utf-8") as handle:
                            json.dump({"owner": owner}, handle)
                    except FileExistsError:
                        continue
                    state["port_leases"].append(str(lease))
                    break
            else:
                raise BackendError("cannot reserve a scenario UDP tunnel port")
            ports.add(port)
            suffix = hashlib.sha256(f"{owner}:{node.name}:{index}".encode()).hexdigest()[:10]
            interfaces.append(
                {
                    "adapter": interface.adapter,
                    "port": index,
                    "device": f"lf{index}",
                    "name": profile.interface_map.get(interface.name, f"eth{interface.adapter}"),
                    "bridge": f"lfb{suffix}",
                    "tap": f"lft{suffix}",
                    "mac": interface.mac,
                    "host_port": port,
                    "guest_port": 30000 + len(ports) - 1,
                }
            )
        return {
            "name": f"lf-{digest}",
            "image": node.template,
            "interfaces": interfaces,
            "memory_mib": profile.memory_mib,
            "cpus": profile.cpus,
            "processes": profile.processes,
            "nesting": profile.nesting,
        }

    def _configuration(self, state: dict[str, Any]) -> str:
        return f"/var/lib/labfoundry/{state['owner']}/configuration.json"

    async def _helper(
        self, state: dict[str, Any], action: str, node: str | None = None
    ) -> ExecResult:
        path = self._configuration(state)
        args: tuple[str, ...] = (
            "/usr/bin/python3",
            path.removesuffix("configuration.json") + "host.py",
            action,
            path,
        )
        if node is not None:
            args += ("--node", state["nodes"][node]["name"])
        return await self._root(state, args, timeout=max(180, self.profile.readiness_timeout))

    async def start(self, state: dict[str, Any]) -> None:
        """Boot the VM and connect its containers to their GNS3 Ethernet tunnels."""
        if self._resume_blocked:
            raise BackendError("scenario VM resume blocked after a failed coordinated restore")
        info = (await self._vbox("showvminfo", state["vm_uuid"], "--machinereadable")).stdout
        if 'VMState="running"' not in info:
            await self._vbox("startvm", state["vm_uuid"], "--type", "headless")
        deadline = asyncio.get_running_loop().time() + self.profile.readiness_timeout
        while True:
            try:
                await self._host(state).healthcheck()
                break
            except GuestExecutionError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise BackendError("scenario VM Guest Additions readiness timed out") from None
                await asyncio.sleep(1)
        interface = self.profile.management_interface
        await self._root(
            state,
            (
                "/bin/sh",
                "-c",
                "set -e; systemctl stop systemd-networkd.service NetworkManager.service "
                '>/dev/null 2>&1 || true; ip link set "$1" up; ip -4 address flush dev "$1"; '
                'ip address add "$2" dev "$1"; ip -4 route flush default; '
                "ip -6 route flush default; sysctl -q -w net.ipv4.ip_forward=0; "
                'timedatectl set-ntp false; install -d -m 0700 "$3"',
                "labfoundry",
                interface,
                state["guest_address"],
                str(PurePosixPath(self._configuration(state)).parent),
            ),
        )
        if "clock_offset_seconds" in state:
            epoch = time.time() + state["clock_offset_seconds"]
            await self._root(state, ("/bin/date", f"--set=@{epoch:.6f}"))
        configuration = {
            "host_address": str(self.profile.host_address),
            "storage_pool": self.profile.storage_pool,
            "startup_concurrency": self.profile.startup_concurrency,
            "nodes": list(state["nodes"].values()),
        }
        host = self._host(state)
        with tempfile.TemporaryDirectory(prefix="labfoundry-incus-") as raw:
            source = Path(raw) / "configuration.json"
            source.write_text(json.dumps(configuration), encoding="utf-8")
            for local, target in (
                (source, "configuration.json"),
                (Path(_incus_host.__file__), "host.py"),
            ):
                staging = f"/tmp/lf-incus-{state['owner']}-{target}"
                await host.upload(local, staging)
                await self._root(
                    state,
                    (
                        "/usr/bin/install",
                        "-m",
                        "0600",
                        "--",
                        staging,
                        str(PurePosixPath(self._configuration(state)).parent / target),
                    ),
                )
                await host.exec(GuestCommand("/usr/bin/rm", ("-f", "--", staging), timeout=30))
        result = await self._helper(state, "prepare")
        state.update(json.loads(result.stdout))

    async def stop(self, state: dict[str, Any], *, force: bool = False) -> None:
        """Record running containers and shut down the VM before snapshot or cleanup."""
        if not state.get("vm_uuid"):
            return
        info = (await self._vbox("showvminfo", state["vm_uuid"], "--machinereadable")).stdout
        if 'VMState="poweroff"' in info:
            return
        try:
            await self._helper(state, "quiesce")
            await self._root(state, ("/bin/systemctl", "poweroff"), timeout=15)
        except GuestExecutionError:
            info = (await self._vbox("showvminfo", state["vm_uuid"], "--machinereadable")).stdout
            if 'VMState="poweroff"' in info:
                return
            if not force:
                raise
            await self._vbox("controlvm", state["vm_uuid"], "acpipowerbutton")
        deadline = asyncio.get_running_loop().time() + self.profile.shutdown_timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                info = (
                    await self._vbox("showvminfo", state["vm_uuid"], "--machinereadable")
                ).stdout
            except GuestExecutionError as error:
                # VBox can lose the console object while its direct session closes.
                if "VBOX_E_INVALID_OBJECT_STATE" not in str(error):
                    raise
            else:
                if 'VMState="poweroff"' in info:
                    return
            await asyncio.sleep(1)
        if not force:
            raise BackendError("scenario VM did not shut down cleanly; snapshot was not taken")
        await self._vbox("controlvm", state["vm_uuid"], "poweroff")

    async def destroy(self, state: dict[str, Any]) -> None:
        """Delete the owned clone and release its address and tunnel leases."""
        if state.get("vm_uuid"):
            info = (await self._vbox("showvminfo", state["vm_uuid"], "--machinereadable")).stdout
            if f'name="lf-incus-{state["owner"]}"' not in info:
                raise BackendError("scenario VM ownership mismatch; refusing to delete it")
            await self.stop(state, force=True)
            await self._vbox("unregistervm", state["vm_uuid"], "--delete", timeout=180)
        for raw in state.get("port_leases", []):
            path = Path(raw)
            if json.loads(path.read_text())["owner"] != state["owner"]:
                raise BackendError("scenario tunnel lease owner changed")
            path.unlink()
        if state.get("lease"):
            path = Path(state["lease"])
            if json.loads(path.read_text())["owner"] != state["owner"]:
                raise BackendError("scenario lease owner changed; refusing to remove it")
            path.unlink()

    async def node_action(self, state: dict[str, Any], node: str, action: str) -> None:
        """Start, stop or restart a container without changing sibling nodes."""
        await self._helper(state, action, node)

    async def snapshot(self, state: dict[str, Any], action: str, snapshot_id: str) -> None:
        """Apply a GNS3 snapshot's identity to the scenario VM's disk snapshot."""
        name = f"lf-{snapshot_id}"
        if action != "delete":
            await self.stop(state)
        await self._vbox("snapshot", state["vm_uuid"], action, name, timeout=180)

    def block_resume(self) -> None:
        """Leave the environment stopped after an incomplete coordinated restore."""
        self._resume_blocked = True
