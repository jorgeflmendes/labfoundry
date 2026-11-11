from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml
from pydantic import ValidationError

from labfoundry.cli.main import bootstrap_guest, bootstrap_linux_guest
from labfoundry.errors import GuestConfigurationError
from labfoundry.guest.bootstrap import (
    BootstrapUser,
    LinuxBootstrapSpec,
    WindowsBootstrapSpec,
    generate_password,
    load_bootstrap_spec,
    write_linux_seed,
    write_linux_seed_from_spec,
    write_windows_bundle,
)
from labfoundry.guest.config import VirtualBoxGuestProfile, load_guest_inventory


def test_linux_bootstrap_pins_apt_before_package_installation(tmp_path: Path) -> None:
    spec = LinuxBootstrapSpec(
        os="linux",
        hostname="historical",
        apt_snapshot="20250904T235959Z",
        users=(BootstrapUser(name="labfoundry", administrator=True, control=True),),
        packages=("python3",),
    )
    seed = write_linux_seed_from_spec(
        tmp_path / "seed",
        spec=spec,
        credentials={"labfoundry": "test-only"},
    )
    data = yaml.safe_load((seed.parent / "user-data").read_text())
    assert data["write_files"] == [
        {
            "path": "/etc/apt/apt.conf.d/50snapshot",
            "permissions": "0644",
            "content": 'APT::Snapshot "20250904T235959Z";\nAPT::Update::Error-Mode "any";\n',
        }
    ]
    assert "python3" in data["packages"]
    with pytest.raises(ValidationError):
        LinuxBootstrapSpec.model_validate({**spec.model_dump(), "apt_snapshot": "20250230T000000Z"})
    with pytest.raises(ValidationError, match="apt_snapshot requires package_update"):
        LinuxBootstrapSpec.model_validate({**spec.model_dump(), "package_update": False})


def test_linux_seed_contains_nocloud_files_and_cidata_volume(tmp_path: Path) -> None:
    seed = write_linux_seed(
        tmp_path / "seed",
        username="labfoundry",
        password="secret",
        hostname="labfoundry",
    )

    image = seed.with_suffix(".img").read_bytes()
    descriptor = seed.read_text(encoding="ascii")
    assert 'RW 8192 FLAT "seed.img" 0' in descriptor
    assert image[43:54] == b"CIDATA     "
    user_data = yaml.safe_load(
        (seed.parent / "user-data").read_text(encoding="utf-8").removeprefix("#cloud-config\n")
    )
    assert {"name": "labfoundry", "password": "secret", "type": "text"} in user_data["chpasswd"][
        "users"
    ]
    assert "user-".encode("utf-16le") in image
    assert "meta-".encode("utf-16le") in image


def test_windows_bundle_creates_unattended_account_and_installer_script(tmp_path: Path) -> None:
    unattended = write_windows_bundle(
        tmp_path / "windows", username="labfoundry", password="secret"
    )

    assert "labfoundry" in unattended.read_text(encoding="utf-8")
    assert "VBoxWindowsAdditions.exe" in unattended.with_name("bootstrap.ps1").read_text(
        encoding="utf-8"
    )


def test_generated_guest_password_is_shell_safe() -> None:
    password = generate_password()

    assert len(password) == 28
    assert password.replace("_", "").replace("-", "").isalnum()


def test_linux_bootstrap_writes_matching_seed_and_inventory(tmp_path: Path) -> None:
    template = tmp_path / "template.yaml"
    template.write_text(
        """
profiles:
  linux-vbox:
    transport: virtualbox
    os: linux
    username: unused
    password_env: UNUSED
    interface_map:
      office: enp0s3
""",
        encoding="utf-8",
    )
    output = tmp_path / "bootstrap"

    bootstrap_linux_guest(template, output, username="labfoundry", hostname="range")

    inventory = load_guest_inventory(output / "guest_inventory.yaml")
    profile = inventory.profiles["linux-vbox"]
    assert isinstance(profile, VirtualBoxGuestProfile)
    password = inventory.resolve_secret(profile, base_dir=output)
    assert password is not None
    user_data = yaml.safe_load(
        (output / "seed" / "user-data").read_text(encoding="utf-8").removeprefix("#cloud-config\n")
    )
    assert {"name": "labfoundry", "password": password, "type": "text"} in user_data["chpasswd"][
        "users"
    ]


def test_declarative_linux_bootstrap_configures_users_packages_and_commands(
    tmp_path: Path,
) -> None:
    template = tmp_path / "inventory.yaml"
    template.write_text(
        """
profiles:
  linux-vbox:
    transport: virtualbox
    os: linux
    username: unused
    password_env: UNUSED
  windows-vbox:
    transport: virtualbox
    os: windows
    username: keep-windows
    password_env: KEEP_WINDOWS_PASSWORD
""",
        encoding="utf-8",
    )
    spec = tmp_path / "bootstrap.yaml"
    spec.write_text(
        """
version: "0.1"
os: linux
hostname: evidence-client
users:
  - name: operator
    password: generated
    groups: [sudo, wireshark]
    administrator: true
    control: true
    shell: /bin/bash
  - name: analyst
    password: generated
  - name: service
    password: locked
packages: [tshark, curl]
services: [ssh]
commands:
  - install -d -m 0750 /opt/evidence
guest_additions: true
""",
        encoding="utf-8",
    )
    output = tmp_path / "bundle"

    bootstrap_guest(spec, template, output)

    inventory = load_guest_inventory(output / "guest_inventory.yaml")
    profile = inventory.profiles["linux-vbox"]
    assert isinstance(profile, VirtualBoxGuestProfile)
    assert profile.username == "operator"
    assert profile.password_env is None
    assert profile.password_file == Path("credentials/operator.password")
    windows_profile = inventory.profiles["windows-vbox"]
    assert isinstance(windows_profile, VirtualBoxGuestProfile)
    assert windows_profile.username == "keep-windows"
    assert windows_profile.password_env == "KEEP_WINDOWS_PASSWORD"
    assert windows_profile.password_file is None
    assert (output / "credentials" / "analyst.password").is_file()
    assert not (output / "credentials" / "service.password").exists()
    user_data = yaml.safe_load(
        (output / "seed" / "user-data").read_text(encoding="utf-8").removeprefix("#cloud-config\n")
    )
    assert user_data["packages"] == ["tshark", "curl", "virtualbox-guest-utils"]
    assert user_data["runcmd"] == [
        "systemctl enable --now ssh",
        "systemctl enable --now vboxservice",
        "install -d -m 0750 /opt/evidence",
    ]
    assert [user["name"] for user in user_data["users"][1:]] == [
        "operator",
        "analyst",
        "service",
    ]


def test_declarative_windows_bootstrap_runs_packages_services_and_commands(
    tmp_path: Path,
) -> None:
    template = tmp_path / "inventory.yaml"
    template.write_text(
        """
profiles:
  windows-vbox:
    transport: virtualbox
    os: windows
    username: unused
    password_env: UNUSED
""",
        encoding="utf-8",
    )
    spec = tmp_path / "bootstrap.yaml"
    spec.write_text(
        """
version: "0.1"
os: windows
hostname: EVIDENCE-PC
users:
  - name: operator
    administrator: true
    control: true
  - name: analyst
packages: [WiresharkFoundation.Wireshark]
services: [sshd]
commands:
  - New-Item -ItemType Directory C:\\Evidence
""",
        encoding="utf-8",
    )
    output = tmp_path / "bundle"

    bootstrap_guest(spec, template, output)

    unattended = (output / "unattend" / "Autounattend.xml").read_text(encoding="utf-8")
    script = (output / "unattend" / "bootstrap.ps1").read_text(encoding="utf-8")
    ElementTree.fromstring(unattended)
    assert "<ComputerName>EVIDENCE-PC</ComputerName>" in unattended
    assert "<Name>operator</Name><Group>Administrators</Group>" in unattended
    assert "<Name>analyst</Name><Group>Users</Group>" in unattended
    assert "-File C:\\Windows\\Setup\\Scripts\\LabFoundry.ps1" in unattended
    assert "winget install --id 'WiresharkFoundation.Wireshark'" in script
    assert "Set-Service -Name 'sshd' -StartupType Automatic" in script
    assert "New-Item -ItemType Directory C:\\Evidence" in script
    copied_script = output / "unattend" / "$OEM$" / "$$" / "Setup" / "Scripts" / "LabFoundry.ps1"
    assert copied_script.read_text(encoding="utf-8") == script


def test_bootstrap_spec_requires_one_generated_control_user(tmp_path: Path) -> None:
    path = tmp_path / "bootstrap.yaml"
    path.write_text(
        """
version: "0.1"
os: linux
hostname: guest
users:
  - name: analyst
""",
        encoding="utf-8",
    )

    with pytest.raises(GuestConfigurationError, match="exactly one"):
        load_bootstrap_spec(path)

    with pytest.raises(ValidationError, match="control user's password"):
        LinuxBootstrapSpec(
            os="linux",
            hostname="guest",
            users=(BootstrapUser(name="operator", password="locked", control=True),),
        )

    with pytest.raises(ValidationError, match="must be an administrator"):
        WindowsBootstrapSpec(
            os="windows",
            hostname="guest",
            users=(BootstrapUser(name="operator", control=True),),
        )


@pytest.mark.parametrize(
    ("guest_os", "expected_type"),
    [("linux", LinuxBootstrapSpec), ("windows", WindowsBootstrapSpec)],
)
def test_guest_bootstrap_spec_loads_from_yaml(
    tmp_path: Path,
    guest_os: str,
    expected_type: type[LinuxBootstrapSpec] | type[WindowsBootstrapSpec],
) -> None:
    path = tmp_path / "bootstrap.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "os": guest_os,
                "hostname": "guest",
                "users": [{"name": "operator", "administrator": True, "control": True}],
            }
        ),
        encoding="utf-8",
    )

    spec = load_bootstrap_spec(path)

    assert isinstance(spec, expected_type)
    assert spec.hostname == "guest"
    assert spec.control_user.name == "operator"
