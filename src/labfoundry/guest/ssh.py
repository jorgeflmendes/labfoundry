"""Key-based OpenSSH guest executor."""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path, PurePosixPath

from labfoundry.guest.base import ExecResult, GuestCommand
from labfoundry.guest.config import SSHGuestProfile
from labfoundry.guest.subprocess import run_host_command


class SSHGuestExecutor:
    """Control one Linux guest through the system OpenSSH client using a private key."""

    os_family = "linux"

    def __init__(self, node: str, profile: SSHGuestProfile, *, base_dir: Path) -> None:
        self.profile = profile
        self.host = profile.host.format(node=node)
        identity = profile.identity_file
        self.identity_file = identity if identity.is_absolute() else base_dir / identity
        known_hosts = profile.known_hosts_file
        self.known_hosts_file = (
            None
            if known_hosts is None
            else (known_hosts if known_hosts.is_absolute() else base_dir / known_hosts)
        )

    def _ssh_base(self) -> list[str]:
        argv = [
            self.profile.ssh,
            "-p",
            str(self.profile.port),
            "-i",
            str(self.identity_file),
            "-o",
            "BatchMode=yes",
        ]
        if self.profile.strict_host_key_checking:
            argv.extend(["-o", "StrictHostKeyChecking=yes"])
        else:
            argv.extend(["-o", "StrictHostKeyChecking=no"])
        if self.known_hosts_file is not None:
            argv.extend(["-o", f"UserKnownHostsFile={self.known_hosts_file}"])
        argv.append(f"{self.profile.username}@{self.host}")
        return argv

    async def healthcheck(self) -> None:
        """Verify key-based SSH command execution."""
        await self.exec(GuestCommand(executable="/bin/true"))

    async def exec(self, command: GuestCommand) -> ExecResult:
        """Execute one command through the system OpenSSH client."""
        env = " ".join(
            f"{shlex.quote(name)}={shlex.quote(value)}" for name, value in command.env.items()
        )
        command_line = shlex.join((command.executable, *command.args))
        if env:
            command_line = f"env {env} {command_line}"
        if command.cwd:
            command_line = f"cd {shlex.quote(command.cwd)} && {command_line}"
        return await run_host_command(
            [*self._ssh_base(), command_line],
            timeout=command.timeout,
            check=command.check,
        )

    async def mkdir(self, path: str) -> None:
        """Create a remote directory recursively."""
        await run_host_command([*self._ssh_base(), f"mkdir -p -- {shlex.quote(path)}"])

    async def upload(self, source: Path, destination: str) -> None:
        """Upload one file with scp after creating its parent directory."""
        await self.mkdir(str(PurePosixPath(destination).parent))
        argv = [
            self.profile.scp,
            "-P",
            str(self.profile.port),
            "-i",
            str(self.identity_file),
        ]
        if not self.profile.strict_host_key_checking:
            argv.extend(["-o", "StrictHostKeyChecking=no"])
        if self.known_hosts_file is not None:
            argv.extend(["-o", f"UserKnownHostsFile={self.known_hosts_file}"])
        argv.extend([str(source), f"{self.profile.username}@{self.host}:{destination}"])
        await run_host_command(argv)

    async def download(self, source: str, destination: Path) -> None:
        """Download one file with scp using an atomic host-side rename."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=".labfoundry-download-", dir=destination.parent)
        os.close(fd)
        temporary = Path(raw)
        temporary.unlink(missing_ok=True)
        try:
            argv = [
                self.profile.scp,
                "-P",
                str(self.profile.port),
                "-i",
                str(self.identity_file),
            ]
            if not self.profile.strict_host_key_checking:
                argv.extend(["-o", "StrictHostKeyChecking=no"])
            if self.known_hosts_file is not None:
                argv.extend(["-o", f"UserKnownHostsFile={self.known_hosts_file}"])
            argv.extend([f"{self.profile.username}@{self.host}:{source}", str(temporary)])
            await run_host_command(argv)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
