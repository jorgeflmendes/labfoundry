"""Validation and construction of guest transport adapters."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Protocol

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.plan import CompiledNode
from labfoundry.errors import GuestConfigurationError
from labfoundry.guest.base import GuestExecutor
from labfoundry.guest.config import (
    GuestInventory,
    GuestProfile,
    IncusGuestProfile,
    SSHGuestProfile,
    VirtualBoxGuestProfile,
)
from labfoundry.guest.incus import IncusGuestExecutor
from labfoundry.guest.ssh import SSHGuestExecutor
from labfoundry.guest.virtualbox import VirtualBoxGuestExecutor


class GuestExecutorFactory(Protocol):
    """Validate profiles and bind executors to deployed nodes."""

    def validate(self, node: CompiledNode, profile: GuestProfile) -> None:
        """Check transport prerequisites without creating infrastructure."""

    def create(
        self, node: CompiledNode, profile: GuestProfile, deployment: DeploymentState
    ) -> GuestExecutor:
        """Construct an executor using the deployment's runtime identifiers."""
        ...


class DefaultGuestExecutorFactory:
    """Construct the built-in guest transport adapters."""

    def __init__(self, inventory: GuestInventory, *, base_dir: Path) -> None:
        self.inventory = inventory
        self.base_dir = base_dir

    def validate(self, node: CompiledNode, profile: GuestProfile) -> None:
        """Validate credentials, SSH key paths and out-of-band management addressing."""
        if isinstance(profile, VirtualBoxGuestProfile):
            self.inventory.resolve_secret(profile, base_dir=self.base_dir)
        elif isinstance(profile, IncusGuestProfile):
            if node.expected_template_type != "incus":
                raise GuestConfigurationError("Incus profiles require template_type: incus")
            environment = self.inventory.environments[profile.environment]
            self.validate(node, self.inventory.profiles[environment.guest_profile])
        elif isinstance(profile, SSHGuestProfile):
            identity = profile.identity_file
            identity = identity if identity.is_absolute() else self.base_dir / identity
            if not identity.is_file():
                raise GuestConfigurationError(
                    f"SSH identity file does not exist for profile {node.guest_profile!r}: "
                    f"{identity}"
                )
            if profile.known_hosts_file is not None:
                known_hosts = profile.known_hosts_file
                known_hosts = (
                    known_hosts if known_hosts.is_absolute() else self.base_dir / known_hosts
                )
                if not known_hosts.is_file():
                    raise GuestConfigurationError(
                        f"SSH known-hosts file does not exist for profile "
                        f"{node.guest_profile!r}: {known_hosts}"
                    )
            try:
                management_ip = ipaddress.ip_address(profile.host.replace("{node}", node.name))
            except ValueError:
                management_ip = None
            configured_ips = {
                ipaddress.ip_interface(interface.address).ip
                for interface in node.interfaces
                if interface.address != "dhcp" and interface.name in profile.interface_map
            }
            if management_ip in configured_ips:
                raise GuestConfigurationError(
                    f"SSH host for node {node.name!r} uses an interface managed by "
                    "LabFoundry; use an out-of-band management address"
                )

    def create(
        self, node: CompiledNode, profile: GuestProfile, deployment: DeploymentState
    ) -> GuestExecutor:
        """Bind a built-in transport to one deployed guest."""
        executor: GuestExecutor
        if isinstance(profile, VirtualBoxGuestProfile):
            password = self.inventory.resolve_secret(profile, base_dir=self.base_dir)
            vm_name = deployment.guest_runtime_ids.get(node.name, node.name)
            executor = VirtualBoxGuestExecutor(vm_name, profile, password=password)
        elif isinstance(profile, SSHGuestProfile):
            executor = SSHGuestExecutor(node.name, profile, base_dir=self.base_dir)
        elif isinstance(profile, IncusGuestProfile):
            state = deployment.environment
            if state.get("name") != profile.environment or node.name not in state.get("nodes", {}):
                raise GuestConfigurationError(f"Incus environment missing for {node.name!r}")
            environment = self.inventory.environments[profile.environment]
            control = self.inventory.profiles[environment.guest_profile]
            assert isinstance(control, VirtualBoxGuestProfile)
            host = VirtualBoxGuestExecutor(
                state["vm_name"],
                control,
                password=self.inventory.resolve_secret(control, base_dir=self.base_dir),
            )
            executor = IncusGuestExecutor(
                state["nodes"][node.name]["name"],
                profile,
                host=host,
                vm_name=state["vm_name"],
                host_sudo=control.requires_sudo,
                host_username=control.username,
                environment_state=state,
            )
        else:  # pragma: no cover - discriminated union makes this unreachable
            raise GuestConfigurationError(f"unsupported guest profile for {node.name!r}")
        return executor
