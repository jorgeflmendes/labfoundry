"""Safe asynchronous subprocess helpers for guest transports."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Sequence

from labfoundry.errors import GuestExecutionError
from labfoundry.guest.base import ExecResult


async def run_host_command(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    check: bool = True,
    stdin: bytes | None = None,
) -> ExecResult:
    """Run a host-side CLI without invoking a shell and capture its output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise GuestExecutionError(f"cannot start host command {argv[0]!r}: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise GuestExecutionError(f"host command {argv[0]!r} exceeded timeout {timeout}s") from exc
    result = ExecResult(
        exit_code=process.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )
    if check and result.exit_code != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
        raise GuestExecutionError(
            f"host command {argv[0]!r} failed with exit code {result.exit_code}: {detail}"
        )
    return result
