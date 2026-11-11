from __future__ import annotations

import asyncio
import subprocess

import pytest

from labfoundry.errors import GuestExecutionError
from labfoundry.guest.subprocess import run_host_command


class _Process:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode: int | None = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self, stdin=None):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_run_host_command_returns_decoded_result(monkeypatch) -> None:
    process = _Process(returncode=0, stdout=b"ok\xff", stderr=b"")

    async def create(*args, **kwargs):
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)

    result = await run_host_command(["tool", "arg"])

    assert result.exit_code == 0
    assert result.stdout.startswith("ok")


@pytest.mark.asyncio
async def test_run_host_command_wraps_nonzero_exit(monkeypatch) -> None:
    process = _Process(returncode=3, stderr=b"failure")

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)

    with pytest.raises(GuestExecutionError, match="exit code 3"):
        await run_host_command(["tool"])


@pytest.mark.asyncio
async def test_run_host_command_wraps_spawn_errors(monkeypatch) -> None:
    async def create(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)

    with pytest.raises(GuestExecutionError, match="cannot start"):
        await run_host_command(["tool"])


@pytest.mark.asyncio
async def test_run_host_command_kills_process_when_cancelled(monkeypatch) -> None:
    started = asyncio.Event()

    class BlockingProcess(_Process):
        def __init__(self) -> None:
            super().__init__()
            self.returncode = None
            self.waited = False

        async def communicate(self, stdin=None):
            started.set()
            await asyncio.Future()

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return self.returncode

    process = BlockingProcess()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr("asyncio.create_subprocess_exec", create)
    task = asyncio.create_task(run_host_command(["tool"]))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.waited is True
