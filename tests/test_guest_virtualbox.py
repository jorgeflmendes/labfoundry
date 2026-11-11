from __future__ import annotations

import os
from pathlib import Path

import pytest

from labfoundry.guest.base import GuestCommand
from labfoundry.guest.config import VirtualBoxGuestProfile
from labfoundry.guest.virtualbox import VirtualBoxGuestExecutor


@pytest.mark.asyncio
async def test_virtualbox_login_rejection_is_not_a_boot_retry(monkeypatch):
    from labfoundry.errors import GuestConfigurationError, GuestExecutionError

    async def reject(*_args, **_kwargs):
        raise GuestExecutionError("The specified user was not able to logon on guest")

    monkeypatch.setattr("labfoundry.guest.virtualbox.run_host_command", reject)
    executor = VirtualBoxGuestExecutor(
        "ws", VirtualBoxGuestProfile(os="linux", username="rf"), password=None
    )
    with pytest.raises(GuestConfigurationError, match="rejected login"):
        await executor.healthcheck()


def test_virtualbox_resolves_default_command_from_install_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "VBoxManage.exe"
    executable.touch()
    monkeypatch.setattr("labfoundry.guest.virtualbox.shutil.which", lambda _: None)
    monkeypatch.setenv("VBOX_MSI_INSTALL_PATH", str(tmp_path))
    profile = VirtualBoxGuestProfile(os="linux", username="labfoundry")

    executor = VirtualBoxGuestExecutor("ws", profile, password="secret")

    assert executor.vboxmanage == str(executable)


@pytest.mark.asyncio
async def test_virtualbox_can_disable_host_time_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(argv, **kwargs):
        calls.append(list(argv))
        from labfoundry.guest.base import ExecResult

        return ExecResult(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr("labfoundry.guest.virtualbox.run_host_command", fake_run)
    executor = VirtualBoxGuestExecutor(
        "ws", VirtualBoxGuestProfile(os="linux", username="rf"), password=None
    )

    await executor.disable_host_time_sync()

    assert calls == [
        [
            executor.vboxmanage,
            "setextradata",
            "ws",
            "VBoxInternal/Devices/VMMDev/0/Config/GetHostTimeDisabled",
            "1",
        ]
    ]


@pytest.mark.asyncio
async def test_virtualbox_healthcheck_has_a_bounded_guestcontrol_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_run(_argv, **kwargs):
        calls.append(kwargs)
        from labfoundry.guest.base import ExecResult

        return ExecResult(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr("labfoundry.guest.virtualbox.run_host_command", fake_run)
    executor = VirtualBoxGuestExecutor(
        "ws", VirtualBoxGuestProfile(os="linux", username="rf"), password=None
    )

    await executor.healthcheck()

    assert calls == [{"timeout": 40.0, "check": True}]


@pytest.mark.asyncio
async def test_virtualbox_guestcontrol_run_uses_passwordfile_and_no_duplicate_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    password_path: Path | None = None

    async def fake_run(argv, **kwargs):
        nonlocal seen, password_path
        seen = list(argv)
        token = next(item for item in seen if item.startswith("--passwordfile="))
        password_path = Path(token.split("=", 1)[1])
        assert password_path.exists()
        assert password_path.read_text(encoding="utf-8") == "secret\n"
        if os.name != "nt":
            assert password_path.stat().st_mode & 0o077 == 0
        from labfoundry.guest.base import ExecResult

        return ExecResult(exit_code=0, stdout="ok", stderr="")

    monkeypatch.setattr("labfoundry.guest.virtualbox.run_host_command", fake_run)
    profile = VirtualBoxGuestProfile(
        os="linux",
        username="labfoundry",
        password_env="IGNORED",
    )
    executor = VirtualBoxGuestExecutor("ws-01", profile, password="secret")
    result = await executor.exec(
        GuestCommand(executable="/usr/bin/curl", args=("-fsS", "https://example.com"))
    )

    assert result.stdout == "ok"
    separator = seen.index("--")
    assert seen[separator + 1 :] == ["-fsS", "https://example.com"]
    assert "--exe=/usr/bin/curl" in seen
    assert password_path is not None and not password_path.exists()


@pytest.mark.asyncio
async def test_virtualbox_upload_uses_exact_destination_basename(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    async def fake_run(argv, **kwargs):
        calls.append(list(argv))
        from labfoundry.guest.base import ExecResult

        return ExecResult(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr("labfoundry.guest.virtualbox.run_host_command", fake_run)
    source = tmp_path / "original.txt"
    source.write_text("payload", encoding="utf-8")
    profile = VirtualBoxGuestProfile(os="linux", username="rf")
    executor = VirtualBoxGuestExecutor("ws", profile, password=None)

    await executor.upload(source, "/opt/scenario/renamed.txt")

    copy_call = next(call for call in calls if "copyto" in call)
    assert copy_call[-2:] == [str(source.resolve()), "/opt/scenario/renamed.txt"]


@pytest.mark.asyncio
async def test_virtualbox_download_atomically_moves_returned_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_run(argv, **kwargs):
        if "copyfrom" in argv:
            Path(argv[-1]).write_text("guest-data", encoding="utf-8")
        from labfoundry.guest.base import ExecResult

        return ExecResult(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr("labfoundry.guest.virtualbox.run_host_command", fake_run)
    profile = VirtualBoxGuestProfile(os="linux", username="rf")
    executor = VirtualBoxGuestExecutor("ws", profile, password=None)
    destination = tmp_path / "downloads" / "activity.log"

    await executor.download("/tmp/activity.log", destination)

    assert destination.read_text(encoding="utf-8") == "guest-data"
