from __future__ import annotations

from pathlib import Path

import pytest

from labfoundry.cli.main import _write_virtualbox_inventory
from labfoundry.errors import GuestConfigurationError
from labfoundry.guest.config import (
    GuestInventory,
    VirtualBoxGuestProfile,
    load_guest_inventory,
)


def test_virtualbox_profile_rejects_two_password_sources() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="only one"):
        VirtualBoxGuestProfile(
            transport="virtualbox",
            os="linux",
            username="rf",
            password_env="LF_PASSWORD",
            password_file=Path("secret.txt"),
        )


def test_linux_profile_dhcp_uses_the_compiled_hostname() -> None:
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="linux",
        username="rf",
        password_env="LF_PASSWORD",
    )

    assert profile.dhcp_command == "dhclient -1 {interface}"


def test_inventory_resolves_environment_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="linux",
        username="rf",
        password_env="LF_PASSWORD",
    )
    inventory = GuestInventory(profiles={"guest": profile})
    monkeypatch.setenv("LF_PASSWORD", "secret")

    assert inventory.resolve_secret(profile, base_dir=Path(".")) == "secret"


def test_inventory_rejects_missing_environment_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="linux",
        username="rf",
        password_env="LF_MISSING_PASSWORD",
    )
    inventory = GuestInventory(profiles={"guest": profile})
    monkeypatch.delenv("LF_MISSING_PASSWORD", raising=False)

    with pytest.raises(GuestConfigurationError, match="missing or empty"):
        inventory.resolve_secret(profile, base_dir=Path("."))


def test_inventory_resolves_relative_password_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="linux",
        username="rf",
        password_file=Path("secret.txt"),
    )
    inventory = GuestInventory(profiles={"guest": profile})

    assert inventory.resolve_secret(profile, base_dir=tmp_path) == "secret"


def test_inventory_resolves_utf8_bom_password_file(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8-sig")
    profile = VirtualBoxGuestProfile(
        transport="virtualbox",
        os="linux",
        username="rf",
        password_file=Path("secret.txt"),
    )
    inventory = GuestInventory(profiles={"guest": profile})

    assert inventory.resolve_secret(profile, base_dir=tmp_path) == "secret"


def test_load_guest_inventory_rejects_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "inventory.yaml"
    path.write_text("- invalid\n", encoding="utf-8")

    with pytest.raises(GuestConfigurationError, match="root must be a mapping"):
        load_guest_inventory(path)


def test_load_guest_inventory_rejects_direct_docker_transport(tmp_path: Path) -> None:
    path = tmp_path / "inventory.yaml"
    path.write_text(
        """
profiles:
  docker:
    transport: docker
    os: linux
    container: lf-{project_id}-{node}
""",
        encoding="utf-8",
    )

    with pytest.raises(GuestConfigurationError, match="union_tag_invalid"):
        load_guest_inventory(path)


@pytest.mark.parametrize(
    "content",
    [
        "profiles: {}\nprofilse: {}\n",
        "profiles:\n  1: {transport: virtualbox, os: linux, username: root}\n",
        "profiles:\n  guest: {transport: virtualbox, os: linux, username: root, unknown: value}\n",
    ],
)
def test_load_guest_inventory_rejects_unknown_fields_and_invalid_names(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "inventory.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(GuestConfigurationError, match="invalid guest inventory"):
        load_guest_inventory(path)


def test_configure_virtualbox_inventory_writes_a_password_file(tmp_path: Path) -> None:
    template = tmp_path / "template.yaml"
    template.write_text(
        """
profiles:
  linux-vbox:
    transport: virtualbox
    os: linux
    username: template-user
    password_env: UNUSED
    interface_map:
      office: enp0s3
""",
        encoding="utf-8",
    )
    output = tmp_path / "private" / "inventory.yaml"
    password_file = output.with_suffix(".password")

    _write_virtualbox_inventory(
        template,
        output,
        password_file,
        username="guest-user",
        password="secret",
    )

    inventory = load_guest_inventory(output)
    profile = inventory.profiles["linux-vbox"]
    assert isinstance(profile, VirtualBoxGuestProfile)
    assert profile.username == "guest-user"
    assert profile.password_env is None
    assert inventory.resolve_secret(profile, base_dir=output.parent) == "secret"
