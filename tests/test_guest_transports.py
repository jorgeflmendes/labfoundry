from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from labfoundry.guest.base import ExecResult, GuestCommand
from labfoundry.guest.config import SSHGuestProfile
from labfoundry.guest.ssh import SSHGuestExecutor


@pytest.mark.asyncio
async def test_ssh_exec_uses_key_known_hosts_env_and_cwd(monkeypatch, tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("private", encoding="utf-8")
    known_hosts.write_text("host key", encoding="utf-8")
    profile = SSHGuestProfile(
        transport="ssh",
        os="linux",
        host="{node}.lab",
        username="rf",
        identity_file=identity,
        known_hosts_file=known_hosts,
    )
    executor = SSHGuestExecutor("ws01", profile, base_dir=tmp_path)
    run = AsyncMock(return_value=ExecResult(0, "ok", ""))
    monkeypatch.setattr("labfoundry.guest.ssh.run_host_command", run)

    result = await executor.exec(
        GuestCommand(
            executable="python3",
            args=("script.py",),
            env={"LF_SEED": "7"},
            cwd="/opt/scenario",
        )
    )

    assert result.stdout == "ok"
    argv = run.await_args.args[0]
    assert "BatchMode=yes" in argv
    assert f"UserKnownHostsFile={known_hosts}" in argv
    assert argv[-2] == "rf@ws01.lab"
    assert argv[-1] == "cd /opt/scenario && env LF_SEED=7 python3 script.py"


@pytest.mark.asyncio
async def test_ssh_upload_creates_parent_then_calls_scp(monkeypatch, tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("private", encoding="utf-8")
    profile = SSHGuestProfile(
        transport="ssh",
        os="linux",
        host="host.test",
        username="rf",
        identity_file=identity,
        strict_host_key_checking=False,
    )
    executor = SSHGuestExecutor("ws", profile, base_dir=tmp_path)
    run = AsyncMock(return_value=ExecResult(0, "", ""))
    monkeypatch.setattr("labfoundry.guest.ssh.run_host_command", run)
    source = tmp_path / "script.py"
    source.write_text("print('ok')", encoding="utf-8")

    await executor.upload(source, "/opt/scenario/script.py")

    assert run.await_count == 2
    assert "mkdir -p" in run.await_args_list[0].args[0][-1]
    assert run.await_args_list[1].args[0][-1].endswith(":/opt/scenario/script.py")
