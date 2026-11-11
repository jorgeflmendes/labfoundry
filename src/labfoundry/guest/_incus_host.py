"""Scenario-VM setup helper. Copied into the VM and run with its system Python."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Any

_INIT_LOCK = Lock()


def _run(*argv: str, timeout: float = 120) -> str:
    result = subprocess.run(
        argv, check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise RuntimeError(f"{argv[0]} failed with exit code {result.returncode}: {detail}")
    return result.stdout


def _incus(*argv: str, timeout: float = 120) -> str:
    return _run("/usr/bin/incus", "--force-local", *argv, timeout=timeout)


def _state(directory: Path) -> dict[str, Any]:
    path = directory / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _save(directory: Path, state: dict[str, Any]) -> None:
    temporary = directory / "state.tmp"
    temporary.write_text(json.dumps(state))
    temporary.replace(directory / "state.json")


def _ubridge(directory: Path) -> socket.socket:
    port_file = directory / "ubridge.json"
    if port_file.exists():
        state = json.loads(port_file.read_text())
        try:
            if Path(f"/proc/{state['pid']}/exe").resolve().name == "ubridge":
                return socket.create_connection(("127.0.0.1", state["port"]), timeout=5)
        except (OSError, ValueError):
            pass
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    unit = f"labfoundry-ubridge-{directory.name}"
    executable = shutil.which("ubridge")
    if executable is None:
        raise RuntimeError("uBridge was not found in the scenario VM")
    _run(
        "systemd-run",
        "--unit",
        unit,
        "--collect",
        "--service-type=exec",
        "--property=UMask=0077",
        f"--property=StandardOutput=append:{directory / 'ubridge.log'}",
        "--property=StandardError=inherit",
        "--",
        executable,
        "-H",
        f"127.0.0.1:{port}",
    )
    pid = int(_run("systemctl", "show", unit, "--property=MainPID", "--value").strip())
    port_file.write_text(json.dumps({"pid": pid, "port": port, "unit": unit}))
    for _ in range(50):
        if not pid or not Path(f"/proc/{pid}").exists():
            raise RuntimeError("scenario uBridge exited; inspect its VM-local log")
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("scenario uBridge readiness timed out")


def _command(connection: socket.socket, command: str) -> None:
    connection.sendall(command.encode("ascii") + b"\n")
    response = bytearray()
    while len(response) < 65536:
        chunk = connection.recv(1)
        if not chunk:
            raise RuntimeError("scenario uBridge closed its control channel")
        response += chunk
        if chunk != b"\n":
            continue
        line = response.decode("utf-8", errors="replace").strip()
        if line.startswith("100-") or line.startswith("100 "):
            return
        if re.match(r"[2-9][0-9][0-9][- ]", line):
            raise RuntimeError(f"scenario uBridge rejected {command.split()[1]}: {line}")
        response.clear()
    raise RuntimeError("oversized scenario uBridge response")


def _network(config: dict[str, Any], directory: Path) -> None:
    if not any(node["interfaces"] for node in config["nodes"]):
        return
    with _ubridge(directory) as connection:
        for node in config["nodes"]:
            for interface in node["interfaces"]:
                bridge, tap = interface["bridge"], interface["tap"]
                if Path("/sys/class/net", bridge).exists():
                    continue
                _run(
                    "ip",
                    "link",
                    "add",
                    bridge,
                    "type",
                    "bridge",
                    "stp_state",
                    "0",
                    "mcast_snooping",
                    "0",
                )
                ipv6 = Path("/proc/sys/net/ipv6/conf", bridge, "disable_ipv6")
                if ipv6.exists():
                    ipv6.write_text("1")
                _run("ip", "link", "set", bridge, "up")
                _command(connection, f"bridge create {bridge}")
                _command(connection, f"bridge add_nio_tap {bridge} {tap}")
                _command(
                    connection,
                    f"bridge add_nio_udp {bridge} {interface['guest_port']} "
                    f"{config['host_address']} {interface['host_port']}",
                )
                ipv6 = Path("/proc/sys/net/ipv6/conf", tap, "disable_ipv6")
                if ipv6.exists():
                    ipv6.write_text("1")
                _run("ip", "link", "set", tap, "master", bridge)
                _run("ip", "link", "set", tap, "up")
                _command(connection, f"bridge start {bridge}")


def _fingerprints(config: dict[str, Any]) -> dict[str, str]:
    images = json.loads(_incus("image", "list", "--format=json"))
    resolved = {}
    for node in config["nodes"]:
        reference = node["image"]
        matches = [
            image
            for image in images
            if image.get("type") == "container"
            and (
                (len(reference) >= 12 and image["fingerprint"].startswith(reference))
                or any(alias["name"] == reference for alias in image.get("aliases", []))
            )
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"local container image {reference!r} resolved to {len(matches)} entries"
            )
        resolved[node["name"]] = matches[0]["fingerprint"]
    return resolved


def _container(
    node: dict[str, Any], config: dict[str, Any], fingerprint: str, existing: set[str]
) -> None:
    name = node["name"]
    if name not in existing:
        settings = {
            "security.privileged": "false",
            "security.idmap.isolated": "true",
            "security.guestapi": "false",
            "security.nesting": str(node["nesting"]).lower(),
            "boot.autostart": "false",
        }
        for field, key in (
            ("memory_mib", "limits.memory"),
            ("cpus", "limits.cpu"),
            ("processes", "limits.processes"),
        ):
            if node.get(field) is not None:
                settings[key] = str(node[field]) + ("MiB" if field == "memory_mib" else "")
        argv = ["init", fingerprint, name, "--no-profiles", "--storage", config["storage_pool"]]
        for key, value in settings.items():
            argv.extend(("--config", f"{key}={value}"))
        # Incus 6.0 can allocate invalid isolated idmaps when init calls overlap.
        with _INIT_LOCK:
            _incus(*argv)
        for interface in node["interfaces"]:
            nic = [
                "config",
                "device",
                "add",
                name,
                interface["device"],
                "nic",
                "nictype=bridged",
                f"parent={interface['bridge']}",
                f"name={interface['name']}",
            ]
            if interface.get("mac"):
                nic.append(f"hwaddr={interface['mac']}")
            _incus(*nic)
    actual = json.loads(_incus("query", f"/1.0/instances/{name}"))
    settings = actual["expanded_config"]
    if (
        settings.get("security.privileged", "false") != "false"
        or settings.get("security.idmap.isolated") != "true"
        or settings.get("raw.lxc")
        or settings.get("raw.idmap")
        or settings.get("security.guestapi") != "false"
        or settings.get("volatile.base_image") != fingerprint
    ):
        raise RuntimeError(f"container {name!r} has unsafe isolation settings")


def _prepare(config: dict[str, Any], directory: Path) -> None:
    version = _incus("--version").strip()
    if not re.match(r"^[0-9]+\.", version) or int(version.split(".", 1)[0]) < 6:
        raise RuntimeError("scenario environments require Incus 6.0 or newer")
    state = _state(directory)
    existing = {item["name"] for item in json.loads(_incus("list", "--format=json"))}
    if not state and existing:
        raise RuntimeError("the scenario VM snapshot must not contain existing instances")
    fingerprints = state.get("fingerprints") or _fingerprints(config)
    _network(config, directory)
    with ThreadPoolExecutor(max_workers=config["startup_concurrency"]) as pool:
        list(
            pool.map(
                lambda node: _container(node, config, fingerprints[node["name"]], existing),
                config["nodes"],
            )
        )
    desired = state.get("running", [node["name"] for node in config["nodes"]])
    current = {item["name"]: item["status"] for item in json.loads(_incus("list", "--format=json"))}
    with ThreadPoolExecutor(max_workers=config["startup_concurrency"]) as pool:
        list(
            pool.map(
                _start,
                [name for name in desired if current.get(name) != "Running"],
            )
        )
    _save(directory, {"running": desired, "fingerprints": fingerprints})
    print(
        json.dumps(
            {
                "fingerprints": fingerprints,
                "kernel": platform.release(),
                "incus_version": version,
            }
        ),
        flush=True,
    )


def _start(name: str) -> None:
    try:
        _incus("start", name)
    except RuntimeError as error:
        try:
            log = _incus("info", name, "--show-log")
        except RuntimeError:
            raise error from None
        raise RuntimeError(f"{error}\n{log[-8000:]}") from error


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "quiesce", "start", "stop", "restart"))
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--node")
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(args.configuration.read_text())
    directory = args.configuration.parent
    if args.action == "prepare":
        _prepare(config, directory)
        return
    names = {node["name"] for node in config["nodes"]}
    running = [
        item["name"]
        for item in json.loads(_incus("list", "--format=json"))
        if item["name"] in names and item["status"] == "Running"
    ]
    if args.action == "quiesce":
        state = _state(directory)
        state["running"] = running
        _save(directory, state)
        with ThreadPoolExecutor(max_workers=config["startup_concurrency"]) as pool:
            list(pool.map(lambda name: _incus("stop", name, "--timeout=30"), running))
        return
    if args.node not in names:
        raise RuntimeError("node does not belong to this scenario")
    _incus(args.action, args.node, *(("--force",) if args.action != "start" else ()))
    current = set(running)
    if args.action == "stop":
        current.discard(args.node)
    else:
        current.add(args.node)
    state = _state(directory)
    state["running"] = sorted(current)
    _save(directory, state)


if __name__ == "__main__":
    _main()
