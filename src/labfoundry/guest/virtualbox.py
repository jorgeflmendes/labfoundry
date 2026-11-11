"""VirtualBox Guest Additions executor implemented through ``VBoxManage guestcontrol``."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from labfoundry.errors import GuestConfigurationError, GuestExecutionError
from labfoundry.guest.base import ExecResult, GuestCommand
from labfoundry.guest.config import VirtualBoxGuestProfile
from labfoundry.guest.subprocess import run_host_command


class VirtualBoxGuestExecutor:
    """Control one GNS3 VirtualBox node out-of-band via VirtualBox Guest Additions."""

    def __init__(
        self,
        vm_name: str,
        profile: VirtualBoxGuestProfile,
        *,
        password: str | None,
    ) -> None:
        self.vm_name = vm_name
        self.profile = profile
        self.password = password
        self.os_family = profile.os
        self.vboxmanage = self._resolve_vboxmanage(profile.vboxmanage)

    @staticmethod
    def _resolve_vboxmanage(command: str) -> str:
        resolved = shutil.which(command)
        if resolved is not None:
            return resolved
        install_path = os.environ.get("VBOX_MSI_INSTALL_PATH")
        if install_path:
            names = (command, f"{command}.exe") if Path(command).suffix == "" else (command,)
            for name in names:
                candidate = Path(install_path) / name
                if candidate.is_file():
                    return str(candidate)
        return command

    @contextmanager
    def _password_args(self) -> Iterator[list[str]]:
        if self.password is None:
            yield []
            return
        fd, raw_path = tempfile.mkstemp(prefix="labfoundry-vbox-password-")
        path = Path(raw_path)
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(self.password)
                handle.write("\n")
            yield [f"--passwordfile={path}"]
        finally:
            path.unlink(missing_ok=True)

    def _credentials(self, password_args: list[str]) -> list[str]:
        result = [f"--username={self.profile.username}", *password_args]
        if self.profile.domain:
            result.append(f"--domain={self.profile.domain}")
        return result

    async def healthcheck(self) -> None:
        """Verify Guest Additions execution with a harmless guest command."""
        if self.os_family == "windows":
            command = GuestCommand(
                executable=r"C:\Windows\System32\cmd.exe",
                args=("/c", "exit", "0"),
                timeout=10.0,
            )
        else:
            command = GuestCommand(executable="/bin/true", timeout=10.0)
        try:
            await self.exec(command)
        except GuestExecutionError as exc:
            if "the specified user was not able to logon" in str(exc).lower():
                raise GuestConfigurationError(
                    f"VirtualBox guest {self.vm_name!r} rejected login for "
                    f"{self.profile.username!r}; check the guest inventory credentials"
                ) from None
            raise

    async def disable_host_time_sync(self) -> None:
        """Prevent Guest Additions from replacing scenario time with host time."""
        await run_host_command(
            (
                self.vboxmanage,
                "setextradata",
                self.vm_name,
                "VBoxInternal/Devices/VMMDev/0/Config/GetHostTimeDisabled",
                "1",
            ),
            timeout=30.0,
        )

    async def exec(self, command: GuestCommand) -> ExecResult:
        """Run a command using the documented VirtualBox guestcontrol ``run`` subcommand."""
        with self._password_args() as password_args:
            argv = [
                self.vboxmanage,
                "--nologo",
                "guestcontrol",
                self.vm_name,
                "run",
                f"--exe={command.executable}",
                *self._credentials(password_args),
                "--wait-stdout",
                "--wait-stderr",
            ]
            if command.timeout is not None:
                argv.append(f"--timeout={max(1, int(command.timeout * 1000))}")
            if command.cwd is not None:
                argv.append(f"--cwd={command.cwd}")
            argv.extend(f"--putenv={name}={value}" for name, value in command.env.items())
            argv.append("--")
            argv.extend(command.args)
            return await run_host_command(
                argv,
                timeout=(command.timeout + 30.0) if command.timeout is not None else None,
                check=command.check,
            )

    async def mkdir(self, path: str) -> None:
        """Create a guest directory using ``VBoxManage guestcontrol mkdir --parents``."""
        with self._password_args() as password_args:
            await run_host_command(
                [
                    self.vboxmanage,
                    "--nologo",
                    "guestcontrol",
                    self.vm_name,
                    "mkdir",
                    *self._credentials(password_args),
                    "--parents",
                    path,
                ]
            )

    def _guest_path(self, value: str) -> PurePosixPath | PureWindowsPath:
        return PureWindowsPath(value) if self.os_family == "windows" else PurePosixPath(value)

    async def upload(self, source: Path, destination: str) -> None:
        """Copy a regular host file to an exact guest destination without loading it in memory."""
        source = source.resolve()
        if not source.is_file() or source.is_symlink():
            raise GuestExecutionError(f"upload source must be a regular non-symlink file: {source}")
        guest_path = self._guest_path(destination)
        parent = str(guest_path.parent)
        await self.mkdir(parent)
        with self._password_args() as password_args:
            await run_host_command(
                [
                    self.vboxmanage,
                    "--nologo",
                    "guestcontrol",
                    self.vm_name,
                    "copyto",
                    *self._credentials(password_args),
                    str(source),
                    destination,
                ]
            )

    async def download(self, source: str, destination: Path) -> None:
        """Copy a regular guest file to an exact host destination atomically."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="labfoundry-download-", dir=destination.parent
        ) as raw_dir:
            staging = Path(raw_dir) / destination.name
            with self._password_args() as password_args:
                await run_host_command(
                    [
                        self.vboxmanage,
                        "--nologo",
                        "guestcontrol",
                        self.vm_name,
                        "copyfrom",
                        *self._credentials(password_args),
                        source,
                        str(staging),
                    ]
                )
            if not staging.is_file():
                raise GuestExecutionError(
                    f"VirtualBox copyfrom completed without producing {destination.name!r}"
                )
            os.replace(staging, destination)
