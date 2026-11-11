"""Generic guest execution primitives independent of applications and protocols."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class GuestCommand:
    """Concrete executable invocation inside a guest operating system."""

    executable: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float | None = None
    check: bool = True


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Captured result of a synchronous guest command."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class ProcessHandle:
    """A long-running guest process tracked by LabFoundry."""

    node: str
    process_id: str
    pid: int


class GuestExecutor(Protocol):
    """Minimal universal transport used to control one running guest node."""

    @property
    def os_family(self) -> str:
        """Return the guest operating-system family."""
        ...

    async def healthcheck(self) -> None:
        """Verify that guest execution is available."""

    async def exec(self, command: GuestCommand) -> ExecResult:
        """Run one command and wait for completion."""

    async def upload(self, source: Path, destination: str) -> None:
        """Copy one regular file from the runner into the guest."""

    async def download(self, source: str, destination: Path) -> None:
        """Copy one regular file from the guest to the runner."""

    async def mkdir(self, path: str) -> None:
        """Create a guest directory including missing parent directories."""


@runtime_checkable
class HostClockControl(Protocol):
    """Optional executor capability for disabling host-driven clock correction."""

    async def disable_host_time_sync(self) -> None:
        """Stop the host from overwriting the guest's scenario clock."""


@runtime_checkable
class SharedClockControl(Protocol):
    """A clock shared by several nodes in one execution environment."""

    @property
    def clock_domain(self) -> str:
        """Identify the VM whose kernel owns this clock."""
        ...

    async def set_clock(self, value: datetime) -> None:
        """Synchronize the environment once for all its nodes."""
