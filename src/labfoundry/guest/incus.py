"""Out-of-band control of system containers in a scenario VM."""

from __future__ import annotations

import asyncio
import shlex
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from labfoundry.errors import GuestExecutionError
from labfoundry.guest.base import ExecResult, GuestCommand, GuestExecutor
from labfoundry.guest.config import IncusGuestProfile


class IncusGuestExecutor:
    """Execute through the VM's local Incus socket without a management NIC in the node."""

    os_family = "linux"

    def __init__(
        self,
        container: str,
        profile: IncusGuestProfile,
        *,
        host: GuestExecutor,
        vm_name: str,
        host_sudo: bool,
        host_username: str,
        environment_state: dict[str, Any] | None = None,
    ) -> None:
        self.container = container
        self.profile = profile
        self.host = host
        self.clock_domain = vm_name
        self.host_sudo = host_sudo
        self.host_username = host_username
        self._environment_state = environment_state

    async def _control(
        self, args: tuple[str, ...], *, timeout: float = 30, check: bool = True
    ) -> ExecResult:
        argv = ("/usr/bin/incus", "--force-local", *args)
        if self.host_sudo:
            argv = ("/usr/bin/sudo", "-n", "--", *argv)
        return await self.host.exec(
            GuestCommand(
                argv[0],
                argv[1:],
                timeout=timeout,
                check=check,
            )
        )

    async def healthcheck(self) -> None:
        """Require a running container reachable through the VM's local Incus socket."""
        script = "test -s /etc/machine-id && systemctl is-active --quiet dbus.socket"
        await self._control(("exec", self.container, "-T", "-n", "--", "/bin/sh", "-c", script))

    async def set_clock(self, value: datetime) -> None:
        """Set the shared VM clock without granting time privileges to containers."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("guest clock value must be timezone-aware")
        script = (
            "timedatectl set-ntp false >/dev/null 2>&1 || true; "
            f"date --set=@{value.timestamp():.6f} >/dev/null"
        )
        argv: tuple[str, ...] = ("/bin/sh", "-c", script)
        if self.host_sudo:
            argv = ("/usr/bin/sudo", "-n", "--", *argv)
        before = time.time()
        await self.host.exec(GuestCommand(argv[0], argv[1:], timeout=30))
        if self._environment_state is not None:
            self._environment_state["clock_offset_seconds"] = (
                value.timestamp() - (before + time.time()) / 2
            )

    async def exec(self, command: GuestCommand) -> ExecResult:
        """Execute argv and terminate its container process group on timeout or cancellation."""
        marker = f"/run/labfoundry-exec-{uuid4().hex}"
        # Cancellation can arrive before Incus starts its wrapper.
        wrapper = (
            "umask 077; set -C; marker=$1; shift; "
            'test ! -e "$marker.cancel" || exit 124; '
            'printf "%s" "$$" > "$marker" || exit; '
            "trap '/usr/bin/rm -f -- \"$marker\"' EXIT; "
            'test ! -e "$marker.cancel" || exit 124; "$@"'
        )
        argv = ["exec", self.container, "-T", "-n"]
        if command.cwd is not None:
            argv.extend(("--cwd", command.cwd))
        for key, value in command.env.items():
            argv.extend(("--env", f"{key}={value}"))
        invocation = [command.executable, *command.args]
        if self.profile.username != "root":
            invocation = ["/usr/sbin/runuser", "-u", self.profile.username, "--", *invocation]
        argv.extend(
            ("--", "/usr/bin/setsid", "/bin/sh", "-c", wrapper, "labfoundry", marker, *invocation)
        )
        timeout = command.timeout if command.timeout is not None else self.profile.command_timeout
        try:
            async with asyncio.timeout(timeout):
                result = await self._control(tuple(argv), timeout=timeout + 5, check=False)
        except (TimeoutError, GuestExecutionError, asyncio.CancelledError) as exc:
            try:
                await asyncio.shield(self._kill(marker))
            except Exception as cleanup:
                exc.add_note(f"Incus command cleanup failed: {cleanup}")
            if isinstance(exc, TimeoutError):
                raise GuestExecutionError(f"Incus command exceeded timeout {timeout:g}s") from exc
            raise
        if command.check and result.exit_code:
            raise GuestExecutionError(
                f"Incus command failed with exit code {result.exit_code}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    async def _kill(self, marker: str) -> None:
        script = (
            'umask 077; : > "$1.cancel"; test -f "$1" || exit 0; pid=$(cat -- "$1"); '
            'case "$pid" in "") exit 0;; *[!0-9]*) exit 1;; esac; '
            'test "$pid" -gt 1 || exit 1; '
            '/bin/kill -KILL -- "-$pid" 2>/dev/null || true; rm -f -- "$1"'
        )
        await self._control(
            (
                "exec",
                self.container,
                "-T",
                "-n",
                "--",
                "/bin/sh",
                "-c",
                script,
                "labfoundry",
                marker,
            )
        )

    async def mkdir(self, path: str) -> None:
        """Create a directory as the configured container user."""
        await self.exec(GuestCommand("/usr/bin/mkdir", ("-p", "--", path)))

    @asynccontextmanager
    async def _staging(self) -> AsyncIterator[str]:
        result = await self.host.exec(
            GuestCommand(
                "/usr/bin/mktemp", ("-d", "/tmp/labfoundry-incus-XXXXXXXXXXXX"), timeout=30
            )
        )
        directory = result.stdout.strip()
        if not directory.startswith("/tmp/labfoundry-incus-") or "/" in directory[5:]:
            raise GuestExecutionError("invalid Incus staging directory")
        try:
            yield directory + "/payload"
        finally:
            with suppress(Exception):
                argv: tuple[str, ...] = ("/usr/bin/rm", "-f", "--", directory + "/payload")
                if self.host_sudo:
                    argv = ("/usr/bin/sudo", "-n", "--", *argv)
                await self.host.exec(GuestCommand(argv[0], argv[1:], timeout=30))
                await self.host.exec(GuestCommand("/usr/bin/rmdir", ("--", directory), timeout=30))

    def _path(self, path: str) -> str:
        if not path.startswith("/") or "\x00" in path:
            raise GuestExecutionError("Incus file paths must be absolute")
        return self.container + path

    async def upload(self, source: Path, destination: str) -> None:
        """Transfer a file through private VM staging into the container."""
        target = self._path(destination)
        async with self._staging() as staging:
            await self.host.upload(source, staging)
            await self._control(
                ("file", "push", "--create-dirs", "--", staging, target), timeout=300
            )

    async def download(self, source: str, destination: Path) -> None:
        """Download through private VM staging and the host transport's atomic copy."""
        target = self._path(source)
        async with self._staging() as staging:
            await self._control(("file", "pull", "--", target, staging), timeout=300)
            script = (
                f"chown -- {shlex.quote(self.host_username)} {shlex.quote(staging)} && "
                f"chmod 0600 -- {shlex.quote(staging)}"
            )
            argv: tuple[str, ...] = ("/bin/sh", "-c", script)
            if self.host_sudo:
                argv = ("/usr/bin/sudo", "-n", "--", *argv)
            await self.host.exec(GuestCommand(argv[0], argv[1:], timeout=30))
            await self.host.download(staging, destination)
