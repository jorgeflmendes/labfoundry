"""Host-side diagnostics for local GNS3 UDP tunnels."""

from __future__ import annotations

import json
import subprocess
from typing import Any

_GNS3_UDP_PROCESS_NAMES = {
    "dynamips",
    "ubridge",
    "vboxheadless",
}


def _find_udp_binding_conflicts(
    endpoints: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    by_port: dict[int, list[dict[str, Any]]] = {}
    for endpoint in endpoints:
        by_port.setdefault(int(endpoint["local_port"]), []).append(endpoint)

    conflicts: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for port, bindings in by_port.items():
        for wildcard in bindings:
            wildcard_address = str(wildcard["local_address"])
            if wildcard_address not in {"0.0.0.0", "::"}:
                continue
            ipv6 = wildcard_address == "::"
            for specific in bindings:
                specific_address = str(specific["local_address"])
                if specific_address in {"0.0.0.0", "::"}:
                    continue
                if (":" in specific_address) != ipv6:
                    continue
                if int(specific["process_id"]) == int(wildcard["process_id"]):
                    continue
                process_names = {
                    str(wildcard["process_name"]).lower().removesuffix(".exe"),
                    str(specific["process_name"]).lower().removesuffix(".exe"),
                }
                if process_names.isdisjoint(_GNS3_UDP_PROCESS_NAMES):
                    continue
                conflicts.append((port, wildcard, specific))
    return conflicts


def _windows_udp_binding_conflicts() -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    script = r"""
$ErrorActionPreference = 'Stop'
@(
    Get-NetUDPEndpoint | ForEach-Object {
        $process = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        [PSCustomObject]@{
            local_address = [string]$_.LocalAddress
            local_port = [int]$_.LocalPort
            process_id = [int]$_.OwningProcess
            process_name = if ($process) { [string]$process.ProcessName } else { '<exited>' }
        }
    }
) | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        capture_output=True,
        text=True,
        timeout=15.0,
    )
    payload = json.loads(result.stdout.lstrip("\ufeff") or "[]")
    if not isinstance(payload, list):
        payload = [payload]
    return _find_udp_binding_conflicts(payload)


def _udp_conflict_key(
    conflict: tuple[int, dict[str, Any], dict[str, Any]],
) -> tuple[int, str, int, str, int]:
    port, wildcard, specific = conflict
    return (
        port,
        str(wildcard["local_address"]),
        int(wildcard["process_id"]),
        str(specific["local_address"]),
        int(specific["process_id"]),
    )
