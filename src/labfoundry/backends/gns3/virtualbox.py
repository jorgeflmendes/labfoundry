"""VirtualBox node validation and local GNS3 lifecycle operations."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from labfoundry.backends.gns3.models import GNS3Node, GNS3Template
from labfoundry.compiler.plan import CompiledNode
from labfoundry.errors import BackendError

if TYPE_CHECKING:
    from labfoundry.backends.gns3.backend import DeploymentState

_VBOX_VM_LINE = re.compile(r'^"(?P<name>.+)" \{(?P<uuid>[^}]+)\}$')
_VBOX_CFG_FILE = re.compile(r'^CfgFile="(?P<path>.+)"$', re.MULTILINE)


def _windows_vboxmanage() -> str:
    install_path = os.environ.get("VBOX_MSI_INSTALL_PATH")
    if install_path:
        return str(Path(install_path) / "VBoxManage.exe")
    return "VBoxManage.exe"


def _recover_windows_orphaned_virtualbox_nodes(project_id: str, names: set[str]) -> None:
    if not names:
        return
    vboxmanage = _windows_vboxmanage()
    result = subprocess.run(
        [vboxmanage, "list", "vms"],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    current_marker = f"\\projects\\{project_id}\\"
    for line in result.stdout.splitlines():
        match = _VBOX_VM_LINE.match(line)
        if match is None or match["name"] not in names:
            continue
        details = subprocess.run(
            [vboxmanage, "showvminfo", match["uuid"], "--machinereadable"],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        cfg_match = _VBOX_CFG_FILE.search(details.stdout)
        if cfg_match is None:
            continue
        cfg_file = Path(cfg_match["path"].replace("\\\\", "\\"))
        normalized = str(cfg_file).replace("/", "\\").lower()
        if current_marker.lower() in normalized:
            continue
        if "\\gns3\\projects\\" not in normalized or cfg_file.exists():
            raise BackendError(
                f"VirtualBox already has a VM named {match['name']!r}; "
                "rename or unregister it before running the scenario"
            )
        subprocess.run(
            [vboxmanage, "unregistervm", match["uuid"]],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            text=True,
            timeout=15.0,
        )


def _assert_windows_virtualbox_node_ownership(project_id: str, names: set[str]) -> None:
    if not names:
        return
    vboxmanage = _windows_vboxmanage()
    result = subprocess.run(
        [vboxmanage, "list", "vms"],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    registrations = {
        match["name"]: match["uuid"]
        for line in result.stdout.splitlines()
        if (match := _VBOX_VM_LINE.match(line)) is not None and match["name"] in names
    }
    current_marker = f"\\projects\\{project_id}\\".lower()
    for name in names:
        uuid = registrations.get(name)
        if uuid is None:
            raise BackendError(f"VirtualBox did not register the started VM {name!r}")
        details = subprocess.run(
            [vboxmanage, "showvminfo", uuid, "--machinereadable"],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        cfg_match = _VBOX_CFG_FILE.search(details.stdout)
        cfg_file = "" if cfg_match is None else cfg_match["path"].replace("\\\\", "\\")
        normalized = cfg_file.replace("/", "\\").lower()
        if current_marker not in normalized:
            raise BackendError(
                f"VirtualBox resolved VM {name!r} outside the current GNS3 project; "
                f"registered configuration is {cfg_file!r}"
            )


def _unregister_windows_virtualbox_nodes_for_project(project_id: str, names: set[str]) -> None:
    if not names:
        return
    vboxmanage = _windows_vboxmanage()
    result = subprocess.run(
        [vboxmanage, "list", "vms"],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    current_marker = f"\\projects\\{project_id}\\".lower()
    for line in result.stdout.splitlines():
        match = _VBOX_VM_LINE.match(line)
        if match is None or match["name"] not in names:
            continue
        details = subprocess.run(
            [vboxmanage, "showvminfo", match["uuid"], "--machinereadable"],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            text=True,
            timeout=15.0,
        )
        cfg_match = _VBOX_CFG_FILE.search(details.stdout)
        if cfg_match is None:
            continue
        cfg_file = Path(cfg_match["path"].replace("\\\\", "\\"))
        normalized = str(cfg_file).replace("/", "\\").lower()
        if current_marker not in normalized:
            continue
        subprocess.run(
            [vboxmanage, "unregistervm", match["uuid"]],
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True,
            text=True,
            timeout=15.0,
        )


class VirtualBoxNodeLifecycle:
    """Adapt VirtualBox templates and protect local Windows VM registrations."""

    async def prepare_node(self, node: GNS3Node, template: GNS3Template) -> GNS3Node:
        """Keep the VirtualBox settings provisioned by GNS3."""
        return node

    def validate_template(self, node: CompiledNode, template: GNS3Template) -> None:
        """Require enough VirtualBox adapters for every declared interface."""
        if template.adapters is None:
            raise BackendError(
                f"VirtualBox template {node.template!r} did not report its adapter count"
            )
        invalid = sorted(
            {
                interface.adapter
                for interface in node.interfaces
                if interface.adapter >= template.adapters
            }
        )
        if invalid:
            raise BackendError(
                f"VirtualBox template {node.template!r} exposes {template.adapters} "
                f"adapters but scenario node {node.name!r} requests {invalid!r}"
            )

    def runtime_id(self, node: GNS3Node, template: GNS3Template) -> str:
        """Resolve a linked clone name or the registered base VM name."""
        linked_clone = bool(
            node.properties.get(
                "linked_clone",
                template.linked_clone if template.linked_clone is not None else False,
            )
        )
        vmname = node.properties.get("vmname") or template.vmname
        if linked_clone:
            return node.name
        if isinstance(vmname, str) and vmname:
            return vmname
        raise BackendError(f"VirtualBox node {node.name!r} did not expose a usable vmname")

    def start_priority(self, compute_id: str) -> int:
        """Serialize local Windows VM launches after the other nodes."""
        # Concurrent VBoxHeadless launches can fail hardening on Windows.
        return 1 if sys.platform == "win32" and compute_id == "local" else 0

    async def before_start(self, deployment: DeploymentState, compute_id: str) -> None:
        """Remove stale registrations without touching VMs belonging to another project."""
        if not self.start_priority(compute_id):
            return
        names = {
            name
            for name, template in deployment.template_metadata.items()
            if template.get("template_type") == "virtualbox"
        }
        try:
            _recover_windows_orphaned_virtualbox_nodes(deployment.project_id, names)
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise BackendError(f"failed to inspect VirtualBox registrations: {exc}") from exc

    async def after_start(self, deployment: DeploymentState, compute_id: str) -> None:
        """Verify that started VMs belong to the current project."""
        if not self.start_priority(compute_id):
            return
        names = {
            name
            for name, template in deployment.template_metadata.items()
            if template.get("template_type") == "virtualbox"
        }
        try:
            _assert_windows_virtualbox_node_ownership(deployment.project_id, names)
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise BackendError(f"failed to verify started VirtualBox nodes: {exc}") from exc

    async def after_destroy(self, deployment: DeploymentState, compute_id: str) -> None:
        """Unregister only this project's VirtualBox runtime VMs after deletion."""
        if not self.start_priority(compute_id):
            return
        names = {
            runtime_id
            for name, runtime_id in deployment.guest_runtime_ids.items()
            if deployment.template_metadata.get(name, {}).get("template_type") == "virtualbox"
        }
        try:
            _unregister_windows_virtualbox_nodes_for_project(deployment.project_id, names)
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise BackendError(f"failed to remove VirtualBox registrations: {exc}") from exc
