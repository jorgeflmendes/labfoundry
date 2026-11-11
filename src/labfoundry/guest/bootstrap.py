"""First-boot assets for reusable guest templates."""

from __future__ import annotations

import secrets
import string
import struct
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from xml.sax.saxutils import escape

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from labfoundry.errors import GuestConfigurationError


class _BootstrapModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BootstrapUser(_BootstrapModel):
    """Account created in a prepared guest image."""

    name: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    password: Literal["generated", "locked"] = "generated"
    groups: tuple[str, ...] = ()
    administrator: bool = False
    control: bool = False
    shell: str | None = None

    @field_validator("groups")
    @classmethod
    def unique_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject duplicate or malformed operating-system group names."""
        if len(set(value)) != len(value):
            raise ValueError("user groups must be unique")
        if any(not group or "\n" in group or "," in group for group in value):
            raise ValueError("user groups must be non-empty names without commas")
        return value


class _GuestBootstrapSpec(_BootstrapModel):
    version: Literal["0.1"] = "0.1"
    hostname: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")
    users: tuple[BootstrapUser, ...] = Field(min_length=1)
    packages: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    guest_additions: bool = True

    @model_validator(mode="after")
    def valid_accounts(self) -> _GuestBootstrapSpec:
        names = [user.name.casefold() for user in self.users]
        if len(set(names)) != len(names):
            raise ValueError("bootstrap user names must be unique")
        controls = [user for user in self.users if user.control]
        if len(controls) != 1:
            raise ValueError("exactly one bootstrap user must set control: true")
        if controls[0].password != "generated":
            raise ValueError("the control user's password must be generated")
        for collection, label in (
            (self.packages, "packages"),
            (self.services, "services"),
            (self.commands, "commands"),
        ):
            if len(set(collection)) != len(collection):
                raise ValueError(f"bootstrap {label} must be unique")
            if any(not value or "\x00" in value for value in collection):
                raise ValueError(f"bootstrap {label} must not contain empty values")
        return self

    @property
    def control_user(self) -> BootstrapUser:
        return next(user for user in self.users if user.control)


class LinuxBootstrapSpec(_GuestBootstrapSpec):
    """Declarative first-boot configuration for a cloud-init Linux image."""

    os: Literal["linux"]
    package_update: bool = True
    apt_snapshot: str | None = Field(default=None, pattern=r"^\d{8}T\d{6}Z$")

    @field_validator("apt_snapshot")
    @classmethod
    def valid_snapshot_date(cls, value: str | None) -> str | None:
        """Reject snapshot IDs with invalid calendar dates."""
        if value is not None:
            datetime.strptime(value, "%Y%m%dT%H%M%SZ")
        return value

    @model_validator(mode="after")
    def snapshot_updates_package_indexes(self) -> LinuxBootstrapSpec:
        """Do not install from cached indexes when selecting a snapshot."""
        if self.apt_snapshot is not None and not self.package_update:
            raise ValueError("apt_snapshot requires package_update: true")
        return self


class WindowsBootstrapSpec(_GuestBootstrapSpec):
    """Declarative unattended configuration for a Windows image."""

    os: Literal["windows"]
    hostname: str = Field(min_length=1, max_length=15, pattern=r"^[A-Za-z0-9][A-Za-z0-9-]*$")

    @model_validator(mode="after")
    def administrative_control_user(self) -> WindowsBootstrapSpec:
        """Require elevation for Windows first-logon provisioning."""
        if not self.control_user.administrator:
            raise ValueError("the Windows control user must be an administrator")
        return self


GuestBootstrapSpec = Annotated[LinuxBootstrapSpec | WindowsBootstrapSpec, Field(discriminator="os")]
_BOOTSTRAP_ADAPTER: TypeAdapter[GuestBootstrapSpec] = TypeAdapter(GuestBootstrapSpec)


def load_bootstrap_spec(path: Path) -> GuestBootstrapSpec:
    """Load a strict guest-image bootstrap specification."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GuestConfigurationError(f"cannot read guest bootstrap {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GuestConfigurationError("guest bootstrap root must be a mapping")
    try:
        return _BOOTSTRAP_ADAPTER.validate_python(raw)
    except Exception as exc:
        raise GuestConfigurationError(f"invalid guest bootstrap {path}: {exc}") from exc


def generate_credentials(spec: GuestBootstrapSpec) -> dict[str, str]:
    """Generate credentials for the unlocked accounts in a bootstrap specification."""
    return {user.name: generate_password() for user in spec.users if user.password == "generated"}


def generate_password(length: int = 28) -> str:
    """Return a shell-safe password for a dedicated guest-control account."""
    alphabet = string.ascii_letters + string.digits + "_-"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def write_linux_seed(directory: Path, *, username: str, password: str, hostname: str) -> Path:
    """Write a NoCloud VFAT seed that creates and prepares a Linux guest account."""
    spec = LinuxBootstrapSpec(
        os="linux",
        hostname=hostname,
        users=(BootstrapUser(name=username, administrator=True, control=True),),
        packages=("arp-scan", "medusa", "nmap", "openssh-server", "openssl", "python3", "sudo"),
        services=("ssh",),
    )
    return write_linux_seed_from_spec(directory, spec=spec, credentials={username: password})


def write_linux_seed_from_spec(
    directory: Path, *, spec: LinuxBootstrapSpec, credentials: dict[str, str]
) -> Path:
    """Write a NoCloud seed from a validated Linux bootstrap specification."""
    _validate_credentials(spec, credentials)
    directory.mkdir(parents=True, exist_ok=False)
    users: list[str | dict[str, object]] = ["default"]
    for user in spec.users:
        entry: dict[str, object] = {
            "name": user.name,
            "lock_passwd": user.password == "locked",
        }
        if user.groups:
            entry["groups"] = ",".join(user.groups)
        if user.shell is not None:
            entry["shell"] = user.shell
        if user.administrator:
            entry["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
        users.append(entry)
    packages = list(spec.packages)
    if spec.guest_additions and "virtualbox-guest-utils" not in packages:
        packages.append("virtualbox-guest-utils")
    commands = [f"systemctl enable --now {service}" for service in spec.services]
    if spec.guest_additions and "vboxservice" not in spec.services:
        commands.append("systemctl enable --now vboxservice")
    commands.extend(spec.commands)
    user_data = {
        "users": users,
        "chpasswd": {
            "users": [
                {"name": name, "password": password, "type": "text"}
                for name, password in credentials.items()
            ],
            "expire": False,
        },
        "package_update": spec.package_update,
        "packages": packages,
        "runcmd": commands,
    }
    if spec.apt_snapshot is not None:
        user_data["write_files"] = [
            {
                "path": "/etc/apt/apt.conf.d/50snapshot",
                "permissions": "0644",
                "content": (
                    f'APT::Snapshot "{spec.apt_snapshot}";\nAPT::Update::Error-Mode "any";\n'
                ),
            }
        ]
    meta_data = {"instance-id": f"labfoundry-{uuid.uuid4()}", "local-hostname": spec.hostname}
    user_data_path = directory / "user-data"
    meta_data_path = directory / "meta-data"
    user_data_path.write_text(
        "#cloud-config\n" + yaml.safe_dump(user_data, sort_keys=False), encoding="utf-8"
    )
    meta_data_path.write_text(yaml.safe_dump(meta_data, sort_keys=False), encoding="utf-8")
    image = directory / "seed.img"
    image.write_bytes(
        _vfat_image(
            {"user-data": user_data_path.read_bytes(), "meta-data": meta_data_path.read_bytes()}
        )
    )
    descriptor = directory / "seed.vmdk"
    descriptor.write_text(_vmdk_descriptor(image.name), encoding="ascii")
    return descriptor


def write_windows_bundle(directory: Path, *, username: str, password: str) -> Path:
    """Write unattended-install assets for a new Windows template."""
    spec = WindowsBootstrapSpec(
        os="windows",
        hostname="labfoundry",
        users=(BootstrapUser(name=username, administrator=True, control=True),),
    )
    return write_windows_bundle_from_spec(directory, spec=spec, credentials={username: password})


def write_windows_bundle_from_spec(
    directory: Path, *, spec: WindowsBootstrapSpec, credentials: dict[str, str]
) -> Path:
    """Write unattended-install assets from a Windows bootstrap specification."""
    _validate_credentials(spec, credentials)
    directory.mkdir(parents=True, exist_ok=False)
    accounts = []
    for user in spec.users:
        group = "Administrators" if user.administrator else "Users"
        password = credentials[user.name] if user.password == "generated" else generate_password()
        accounts.append(
            '<LocalAccount wcm:action="add">'
            f"<Name>{escape(user.name)}</Name><Group>{group}</Group>"
            f"<Password><Value>{escape(password)}</Value><PlainText>true</PlainText></Password>"
            "</LocalAccount>"
        )
    control_name = escape(spec.control_user.name)
    control_password = escape(credentials[spec.control_user.name])
    bootstrap_command = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass "
        "-File C:\\Windows\\Setup\\Scripts\\LabFoundry.ps1"
    )
    unattended = f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend"
 xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64"
     publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>{escape(spec.hostname)}</ComputerName>
      <UserAccounts><LocalAccounts>{"".join(accounts)}</LocalAccounts></UserAccounts>
      <AutoLogon><Enabled>true</Enabled><LogonCount>1</LogonCount>
        <Username>{control_name}</Username>
        <Password><Value>{control_password}</Value><PlainText>true</PlainText></Password>
      </AutoLogon>
      <FirstLogonCommands><SynchronousCommand wcm:action="add">
        <Order>1</Order><Description>Prepare LabFoundry guest</Description>
        <CommandLine>{bootstrap_command}</CommandLine>
      </SynchronousCommand></FirstLogonCommands>
      <OOBE><HideEULAPage>true</HideEULAPage><ProtectYourPC>3</ProtectYourPC></OOBE>
    </component>
  </settings>
</unattend>
"""
    path = directory / "Autounattend.xml"
    script = "$ErrorActionPreference = 'Stop'\n"
    if spec.guest_additions:
        script += r"""$media = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 5'
$installer = $media | ForEach-Object {
    $candidate = Join-Path $_.DeviceID 'VBoxWindowsAdditions.exe'
    if (Test-Path $candidate) { Get-Item $candidate }
} | Select-Object -First 1
if ($installer) {
    $process = Start-Process -FilePath $installer.FullName -ArgumentList '/S' -Wait -PassThru
    if ($process.ExitCode -notin 0, 3010) {
        throw "VirtualBox Guest Additions installer failed with exit code $($process.ExitCode)"
    }
} else {
    throw 'VirtualBox Guest Additions media was not found'
}
"""
    if spec.packages:
        script += (
            "if (-not (Get-Command winget -ErrorAction SilentlyContinue)) "
            "{ throw 'winget was not found' }\n"
        )
    for package in spec.packages:
        script += (
            f"& winget install --id '{_powershell_quote(package)}' --exact --silent "
            "--disable-interactivity --accept-package-agreements --accept-source-agreements\n"
            f"if ($LASTEXITCODE -ne 0) {{ throw 'winget failed for "
            f"{_powershell_quote(package)}' }}\n"
        )
    for service in spec.services:
        quoted = _powershell_quote(service)
        script += f"Set-Service -Name '{quoted}' -StartupType Automatic\nStart-Service '{quoted}'\n"
    if spec.commands:
        script += "\n".join(spec.commands) + "\n"
    for user in spec.users:
        if user.password == "locked":
            script += f"Disable-LocalUser -Name '{_powershell_quote(user.name)}'\n"
    path.write_text(unattended, encoding="utf-8")
    (directory / "bootstrap.ps1").write_text(script, encoding="utf-8")
    copied_script = directory / "$OEM$" / "$$" / "Setup" / "Scripts" / "LabFoundry.ps1"
    copied_script.parent.mkdir(parents=True)
    copied_script.write_text(script, encoding="utf-8")
    return path


def _powershell_quote(value: str) -> str:
    return value.replace("'", "''")


def _validate_credentials(spec: GuestBootstrapSpec, credentials: dict[str, str]) -> None:
    expected = {user.name for user in spec.users if user.password == "generated"}
    if set(credentials) != expected or any(not value for value in credentials.values()):
        raise GuestConfigurationError("bootstrap credentials do not match generated-password users")


def _vfat_image(files: dict[str, bytes]) -> bytes:
    """Build a small CIDATA FAT16 image with long names accepted by NoCloud."""
    sector_size = 512
    sectors = 8192
    sectors_per_fat = 32
    root_entries = 512
    fat_count = 2
    reserved_sectors = 1
    root_sectors = root_entries * 32 // sector_size
    data_sector = reserved_sectors + fat_count * sectors_per_fat + root_sectors
    image = bytearray(sectors * sector_size)
    boot = memoryview(image)[:sector_size]
    boot[:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", boot, 11, sector_size)
    boot[13] = 1
    struct.pack_into("<H", boot, 14, reserved_sectors)
    boot[16] = fat_count
    struct.pack_into("<H", boot, 17, root_entries)
    struct.pack_into("<H", boot, 19, sectors)
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, sectors_per_fat)
    struct.pack_into("<H", boot, 24, 32)
    struct.pack_into("<H", boot, 26, 64)
    boot[36] = 0x80
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, secrets.randbits(32))
    boot[43:54] = b"CIDATA     "
    boot[54:62] = b"FAT16   "
    boot[510:512] = b"\x55\xaa"

    fat = bytearray(sectors_per_fat * sector_size)
    struct.pack_into("<H", fat, 0, 0xFFF8)
    struct.pack_into("<H", fat, 2, 0xFFFF)
    root_offset = (reserved_sectors + fat_count * sectors_per_fat) * sector_size
    root = memoryview(image)[root_offset : root_offset + root_entries * 32]
    root[:32] = _volume_label()
    root_index = 1
    next_cluster = 2
    for name, content in files.items():
        clusters = max(1, (len(content) + sector_size - 1) // sector_size)
        cluster = next_cluster
        for value in range(cluster, cluster + clusters):
            following = 0xFFFF if value == cluster + clusters - 1 else value + 1
            struct.pack_into("<H", fat, value * 2, following)
        root[root_index * 32 : (root_index + 1) * 32] = _long_name_entry(name, _short_name(name))
        root_index += 1
        root[root_index * 32 : (root_index + 1) * 32] = _short_entry(
            _short_name(name), cluster, len(content)
        )
        root_index += 1
        offset = (data_sector + cluster - 2) * sector_size
        image[offset : offset + len(content)] = content
        next_cluster += clusters
    for index in range(fat_count):
        start = (reserved_sectors + index * sectors_per_fat) * sector_size
        image[start : start + len(fat)] = fat
    return bytes(image)


def _volume_label() -> bytes:
    value = bytearray(32)
    value[:11] = b"CIDATA     "
    value[11] = 0x08
    return bytes(value)


def _short_name(name: str) -> bytes:
    stem = "".join(character for character in name.upper() if character.isalnum())[:8]
    return stem.ljust(8).encode() + b"   "


def _long_name_entry(name: str, short_name: bytes) -> bytes:
    values = [ord(character) for character in name] + [0]
    values.extend([0xFFFF] * (13 - len(values)))
    value = bytearray(32)
    value[0] = 0x41
    value[11] = 0x0F
    value[13] = _short_name_checksum(short_name)
    for index, offset in enumerate((1, 14, 28)):
        count = (5, 6, 2)[index]
        for position in range(count):
            struct.pack_into("<H", value, offset + position * 2, values.pop(0))
    return bytes(value)


def _short_entry(name: bytes, cluster: int, size: int) -> bytes:
    value = bytearray(32)
    value[:11] = name
    value[11] = 0x20
    struct.pack_into("<H", value, 26, cluster)
    struct.pack_into("<I", value, 28, size)
    return bytes(value)


def _short_name_checksum(name: bytes) -> int:
    result = 0
    for character in name:
        result = ((result & 1) << 7) + (result >> 1) + character
        result &= 0xFF
    return result


def _vmdk_descriptor(image_name: str) -> str:
    return f'''# Disk DescriptorFile
version=1
CID={secrets.token_hex(4)}
parentCID=ffffffff
createType="monolithicFlat"

# Extent description
RW 8192 FLAT "{image_name}" 0

# The Disk Data Base
ddb.virtualHWVersion = "4"
'''
