"""Out-of-band guest executor inventory.

Secrets deliberately live outside the scenario DSL so compiled plans and evidence
metadata never need to contain guest passwords or private-key material.
"""

from __future__ import annotations

import os
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from pathlib import Path
from typing import Annotated, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from labfoundry.errors import GuestConfigurationError
from labfoundry.schema.common import Identifier


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CommonProfile(_StrictModel):
    manage_hostname: ClassVar[bool] = False
    check_identity: ClassVar[bool] = False

    os: Literal["linux", "windows"]
    shell: str | None = None
    interface_map: dict[Identifier, str] = Field(default_factory=dict)
    dhcp_command: str = Field(default="dhclient -1 {interface}", min_length=1)
    network_control: Literal["coexist", "exclusive"] = "coexist"

    @property
    def requires_sudo(self) -> bool:
        """Return whether privileged Linux operations need non-interactive sudo."""
        return False


class _UserProfile(_CommonProfile):
    manage_hostname: ClassVar[bool] = True
    username: str = Field(min_length=1)

    @property
    def requires_sudo(self) -> bool:
        """Use sudo when the transport authenticates as a non-root user."""
        return self.username != "root"


class VirtualBoxGuestProfile(_UserProfile):
    """Guest Additions credentials and logical-to-OS interface mappings.

    Password-file paths are relative to the inventory directory. Each logical interface
    must map to the intended guest device; the runtime does not discover these mappings.
    """

    check_identity: ClassVar[bool] = True

    transport: Literal["virtualbox"] = "virtualbox"
    password_env: str | None = None
    password_file: Path | None = None
    domain: str | None = None
    vboxmanage: str = "VBoxManage"

    @model_validator(mode="after")
    def one_password_source(self) -> VirtualBoxGuestProfile:
        """Reject ambiguous VirtualBox password sources."""
        if self.password_env is not None and self.password_file is not None:
            raise ValueError("use only one of password_env or password_file")
        return self


class SSHGuestProfile(_UserProfile):
    """OpenSSH transport configuration using public-key authentication."""

    transport: Literal["ssh"] = "ssh"
    os: Literal["linux"] = "linux"
    host: str = Field(min_length=1, description="Host name; may contain {node}.")
    port: int = Field(default=22, ge=1, le=65535)
    identity_file: Path
    known_hosts_file: Path | None = None
    strict_host_key_checking: bool = True
    ssh: str = "ssh"
    scp: str = "scp"


class IncusGuestProfile(_UserProfile):
    """An unprivileged system container in a scenario's Linux VM."""

    transport: Literal["incus"] = "incus"
    os: Literal["linux"] = "linux"
    username: str = Field(default="root", min_length=1)
    check_identity: ClassVar[bool] = True
    environment: Identifier
    memory_mib: int | None = Field(default=None, ge=64)
    cpus: int | None = Field(default=None, ge=1)
    processes: int | None = Field(default=None, ge=64)
    nesting: bool = False
    command_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)


class IncusEnvironmentProfile(_StrictModel):
    """A linked VirtualBox clone dedicated to one scenario."""

    vmname: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    guest_profile: Identifier
    host_only_adapter: str = Field(min_length=1)
    host_address: IPv4Address
    guest_addresses: tuple[IPv4Interface, ...] = Field(min_length=1)
    management_interface: str = Field(default="enp0s3", pattern=r"^[a-zA-Z0-9_.-]{1,15}$")
    memory_mib: int = Field(ge=512)
    cpus: int = Field(ge=1, le=32)
    paravirt_provider: Literal["default", "none", "minimal", "hyperv", "kvm"] | None = None
    storage_pool: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_-]+$")
    startup_concurrency: int = Field(default=4, ge=1, le=16)
    readiness_timeout: float = Field(default=180, gt=0, allow_inf_nan=False)
    shutdown_timeout: float = Field(default=60, gt=0, allow_inf_nan=False)
    reserve_memory_mib: int = Field(default=1024, ge=0)

    @model_validator(mode="after")
    def private_network(self) -> IncusEnvironmentProfile:
        """Keep tunnel endpoints on an explicitly configured host-only network."""
        seen: set[IPv4Address] = set()
        networks = tuple(
            IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        for address in self.guest_addresses:
            if (
                not any(address.network.subnet_of(network) for network in networks)
                or address.ip.is_loopback
                or address.ip.is_link_local
                or self.host_address not in address.network
                or address.ip
                in (address.network.network_address, address.network.broadcast_address)
                or address.ip == self.host_address
                or address.ip in seen
            ):
                raise ValueError("guest_addresses must be unique private host-only addresses")
            seen.add(address.ip)
        return self


GuestProfile = Annotated[
    VirtualBoxGuestProfile | SSHGuestProfile | IncusGuestProfile,
    Field(discriminator="transport"),
]


class GuestInventory(_StrictModel):
    """Named guest profiles referenced by scenario node groups."""

    profiles: dict[Identifier, GuestProfile]
    environments: dict[Identifier, IncusEnvironmentProfile] = Field(default_factory=dict)

    @model_validator(mode="after")
    def environment_profiles(self) -> GuestInventory:
        """Resolve container environments and their out-of-band control profiles."""
        for name, environment in self.environments.items():
            control = self.profiles.get(environment.guest_profile)
            if not isinstance(control, VirtualBoxGuestProfile) or control.os != "linux":
                raise ValueError(f"environment {name!r} needs a Linux VirtualBox guest_profile")
        for profile in self.profiles.values():
            if (
                isinstance(profile, IncusGuestProfile)
                and profile.environment not in self.environments
            ):
                raise ValueError(f"unknown Incus environment {profile.environment!r}")
        return self

    def resolve_secret(self, profile: VirtualBoxGuestProfile, *, base_dir: Path) -> str | None:
        """Resolve a VirtualBox password from the environment or a file relative to base_dir."""
        if profile.password_env is not None:
            value = os.environ.get(profile.password_env)
            if value is None or value == "":
                raise GuestConfigurationError(
                    f"environment variable {profile.password_env!r} is missing or empty"
                )
            return value
        if profile.password_file is None:
            return None
        path = profile.password_file
        if not path.is_absolute():
            path = base_dir / path
        try:
            value = path.read_text(encoding="utf-8-sig").rstrip("\r\n")
        except OSError as exc:
            raise GuestConfigurationError(f"cannot read guest password file {path}: {exc}") from exc
        if not value:
            raise GuestConfigurationError(f"guest password file {path} is empty")
        return value


def load_guest_inventory(path: Path) -> GuestInventory:
    """Load and validate a guest inventory, resolving relative file paths at use time."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GuestConfigurationError(f"cannot read guest inventory {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GuestConfigurationError("guest inventory root must be a mapping")
    if not isinstance(raw.get("profiles"), dict):
        raise GuestConfigurationError("guest inventory must contain a profiles mapping")
    try:
        return GuestInventory.model_validate(raw)
    except ValidationError as exc:
        raise GuestConfigurationError(f"invalid guest inventory {path}: {exc}") from exc
