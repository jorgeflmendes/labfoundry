"""Opt-in integration tests against OpenSSH running in a Docker fixture."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from labfoundry.guest.base import GuestCommand
from labfoundry.guest.config import SSHGuestProfile
from labfoundry.guest.ssh import SSHGuestExecutor

pytestmark = pytest.mark.integration


def _require_enabled() -> None:
    if os.environ.get("LABFOUNDRY_GUEST_INTEGRATION") != "1":
        pytest.skip("set LABFOUNDRY_GUEST_INTEGRATION=1 to run the real SSH test")
    subprocess.run(
        ["docker", "info"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def ssh_container(tmp_path: Path) -> Iterator[tuple[str, int, Path, Path]]:
    _require_enabled()
    identity = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(identity)],
        check=True,
        timeout=30,
    )
    host_identity = tmp_path / "ssh_host_ed25519_key"
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(host_identity),
        ],
        check=True,
        timeout=30,
    )
    name = f"lf-ssh-it-{uuid.uuid4().hex[:12]}"
    mount = f"{tmp_path.as_posix()}:/keys:ro"
    setup = " && ".join(
        [
            "apk add --no-cache openssh-server",
            "cp /keys/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key",
            "chmod 600 /etc/ssh/ssh_host_ed25519_key",
            "echo 'KexAlgorithms curve25519-sha256' >> /etc/ssh/sshd_config",
            "mkdir -p /root/.ssh",
            "cp /keys/id_ed25519.pub /root/.ssh/authorized_keys",
            "chmod 700 /root/.ssh",
            "chmod 600 /root/.ssh/authorized_keys",
            "exec /usr/sbin/sshd -D -e",
        ]
    )
    _docker(
        "run",
        "--detach",
        "--rm",
        "--name",
        name,
        "--publish",
        "127.0.0.1::22",
        "--volume",
        mount,
        "alpine:3.22",
        "/bin/sh",
        "-c",
        setup,
    )
    try:
        mapping = _docker("port", name, "22/tcp").stdout.strip()
        port = int(mapping.rsplit(":", 1)[1])
        known_hosts = tmp_path / "known_hosts"
        for _ in range(120):
            scan = subprocess.run(
                ["ssh-keyscan", "-t", "ed25519", "-p", str(port), "127.0.0.1"],
                capture_output=True,
                timeout=5,
            )
            if scan.stdout:
                known_hosts.write_bytes(scan.stdout)
                break
            time.sleep(0.5)
        else:
            completed = _docker("logs", name)
            logs = completed.stdout + completed.stderr
            pytest.fail(f"OpenSSH container did not become ready:\n{logs}")

        yield name, port, identity, known_hosts
    finally:
        subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            text=True,
            timeout=30,
        )


@pytest.mark.asyncio
async def test_real_ssh_exec_and_file_transfer(
    ssh_container: tuple[str, int, Path, Path], tmp_path: Path
) -> None:
    _, port, identity, known_hosts = ssh_container
    profile = SSHGuestProfile(
        username="root",
        host="127.0.0.1",
        port=port,
        identity_file=identity,
        known_hosts_file=known_hosts,
    )
    executor = SSHGuestExecutor("node", profile, base_dir=tmp_path)

    await executor.healthcheck()
    await executor.mkdir("/tmp/labfoundry")
    result = await executor.exec(
        GuestCommand(
            executable="/bin/sh",
            args=("-c", 'printf "%s:%s" "$LABFOUNDRY_TOKEN" "$PWD"'),
            env={"LABFOUNDRY_TOKEN": "ssh-real"},
            cwd="/tmp/labfoundry",
        )
    )
    assert result.stdout == "ssh-real:/tmp/labfoundry"

    source = tmp_path / "ssh-upload.txt"
    source.write_text("ssh transfer\n", encoding="utf-8")
    await executor.upload(source, "/tmp/labfoundry/upload.txt")
    downloaded = tmp_path / "ssh-download.txt"
    await executor.download("/tmp/labfoundry/upload.txt", downloaded)
    assert downloaded.read_bytes() == source.read_bytes()
