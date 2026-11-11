"""Built-in generic scenario actions.

The core deliberately exposes execution/file/process primitives rather than
application-specific actions.  A scenario can therefore drive curl, browsers,
Git, mail clients, custom Python, or any other tool already present in a guest.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labfoundry.compiler.plan import CompiledEvent, CompiledProvisionAction, CompiledTrafficAction
from labfoundry.errors import GuestExecutionError
from labfoundry.guest.base import ExecResult
from labfoundry.guest.manager import GuestManager
from labfoundry.runtime.journal import RunJournal
from labfoundry.util.duration import parse_duration


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryParams(_Params):
    """Retry policy for transient guest command failures."""

    attempts: int = Field(default=1, ge=1, le=100)
    delay: str | float = 0.0
    backoff: float = Field(default=1.0, ge=1.0, le=100.0)

    def delay_seconds(self) -> float:
        """Return the initial retry delay in seconds."""
        value = (
            float(self.delay)
            if isinstance(self.delay, (int, float))
            else parse_duration(self.delay).total_seconds()
        )
        if value < 0:
            raise ValueError("retry delay cannot be negative")
        return value


class ExecParams(_Params):
    """Parameters accepted by ``guest.exec``."""

    argv: tuple[str, ...] | None = None
    script: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    timeout: str | float | None = None
    check: bool = True
    retry: RetryParams = Field(default_factory=RetryParams)

    @model_validator(mode="after")
    def one_invocation(self) -> ExecParams:
        """Require one invocation and validate its timeout and retry delay."""
        if (self.argv is None) == (self.script is None):
            raise ValueError("guest.exec requires exactly one of argv or script")
        if self.argv is not None and not self.argv:
            raise ValueError("guest.exec argv cannot be empty")
        self.timeout_seconds()
        self.retry.delay_seconds()
        return self

    def timeout_seconds(self) -> float | None:
        """Return a positive timeout in seconds."""
        if self.timeout is None:
            return None
        value = (
            float(self.timeout)
            if isinstance(self.timeout, (int, float))
            else parse_duration(self.timeout).total_seconds()
        )
        if value <= 0:
            raise ValueError("timeout must be greater than zero")
        return value


class ReadyParams(_Params):
    """Guest command that succeeds once a detached service is ready."""

    script: str = Field(min_length=1)
    timeout: str | float = "30s"
    interval: str | float = "500ms"

    @model_validator(mode="after")
    def validate_timing(self) -> ReadyParams:
        """Reject invalid probe timing during preflight."""
        self.timeout_seconds()
        self.interval_seconds()
        return self

    def timeout_seconds(self) -> float:
        """Return the positive readiness deadline in seconds."""
        value = (
            float(self.timeout)
            if isinstance(self.timeout, (int, float))
            else parse_duration(self.timeout).total_seconds()
        )
        if value <= 0:
            raise ValueError("readiness timeout must be greater than zero")
        return value

    def interval_seconds(self) -> float:
        """Return the positive probe interval in seconds."""
        value = (
            float(self.interval)
            if isinstance(self.interval, (int, float))
            else parse_duration(self.interval).total_seconds()
        )
        if value <= 0:
            raise ValueError("readiness interval must be greater than zero")
        return value


class StartParams(_Params):
    """Parameters accepted by ``guest.start``."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    script: str = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    ready: ReadyParams | None = None


class StopParams(_Params):
    """Parameters accepted by ``guest.stop``."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")


class UploadParams(_Params):
    """Parameters accepted by ``guest.upload``."""

    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class DownloadParams(_Params):
    """Parameters accepted by ``guest.download``."""

    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    publish: bool = False
    refresh_at_end: bool = False


class CopyParams(_Params):
    """Parameters accepted by ``guest.copy``."""

    source: str = Field(min_length=1)
    destination: str = Field(min_length=1)


class MkdirParams(_Params):
    """Parameters accepted by ``guest.mkdir``."""

    path: str = Field(min_length=1)


class WaitParams(_Params):
    """Parameters accepted by ``guest.wait`` for a tracked detached process."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    timeout: str | float = "60s"
    interval: str | float = "500ms"

    @model_validator(mode="after")
    def validate_timing(self) -> WaitParams:
        """Reject invalid process polling timing during preflight."""
        self.timeout_seconds()
        self.interval_seconds()
        return self

    def timeout_seconds(self) -> float:
        """Return the positive maximum wait time in seconds."""
        value = (
            float(self.timeout)
            if isinstance(self.timeout, (int, float))
            else parse_duration(self.timeout).total_seconds()
        )
        if value <= 0:
            raise ValueError("wait timeout must be greater than zero")
        return value

    def interval_seconds(self) -> float:
        """Return the positive process polling interval in seconds."""
        value = (
            float(self.interval)
            if isinstance(self.interval, (int, float))
            else parse_duration(self.interval).total_seconds()
        )
        if value <= 0:
            raise ValueError("wait interval must be greater than zero")
        return value


class StatusParams(_Params):
    """Parameters accepted by ``guest.status`` to assert process state."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    running: bool


class GuestActionDispatcher:
    """Execute generic event and recurring actions through a :class:`GuestManager`."""

    ACTIONS = frozenset(
        {
            "guest.exec",
            "guest.start",
            "guest.stop",
            "guest.upload",
            "guest.copy",
            "guest.download",
            "guest.mkdir",
            "guest.wait",
            "guest.status",
        }
    )

    def __init__(
        self,
        guests: GuestManager,
        *,
        source_root: Path,
        asset_root: Path | None = None,
        run_dir: Path,
        journal: RunJournal | None = None,
    ) -> None:
        self.guests = guests
        self.source_root = source_root.resolve()
        self.asset_root = asset_root.resolve() if asset_root is not None else None
        self.run_dir = run_dir.resolve()
        self.journal = journal
        self._event_results: dict[str, asyncio.Future[ExecResult | None]] = {}
        self._final_downloads: dict[Path, tuple[str, str]] = {}

    async def finalize_downloads(self, *, best_effort: bool = False) -> None:
        """Refresh growing artifacts after captures and guest processes have stopped."""
        for destination, (node, source) in self._final_downloads.items():
            try:
                await self.guests.download(node, source, destination)
            except (GuestExecutionError, OSError) as exc:
                if not best_effort:
                    raise
                if self.journal is not None:
                    self.journal.record(
                        "guest.download.finalization_failed",
                        node=node,
                        source=source,
                        error=type(exc).__name__,
                    )

    def prepare_events(self, events: tuple[CompiledEvent, ...]) -> None:
        """Register result dependencies and unconditional end-of-run downloads."""
        loop = asyncio.get_running_loop()
        sources = {event.when.event for event in events if event.when is not None}
        self._event_results = {event_id: loop.create_future() for event_id in sources}
        for event in events:
            if event.action != "guest.download" or event.when is not None:
                continue
            params = DownloadParams.model_validate(event.parameters)
            if params.refresh_at_end:
                destination = self._safe_download_destination(
                    params.destination,
                    publish=params.publish,
                )
                self._final_downloads[destination] = (
                    self._require_node(event.from_, event.action),
                    params.source,
                )

    @staticmethod
    def _require_node(node: str | None, action: str) -> str:
        if node is None:
            raise GuestExecutionError(f"{action} requires an event 'from' node")
        return node

    def _safe_source(self, relative: str) -> Path:
        if relative.startswith("@helpers/"):
            root = Path(__file__).resolve().parents[1] / "guest"
            label = "guest helpers"
            relative = relative.removeprefix("@helpers/")
            if relative not in {"browser_timing.py", "response_timing.py"}:
                raise GuestExecutionError(f"unknown guest helper: {relative!r}")
        elif relative.startswith("@assets/"):
            if self.asset_root is None:
                raise GuestExecutionError("upload source requires --asset-dir")
            root = self.asset_root
            label = "asset directory"
            relative = relative.removeprefix("@assets/")
        else:
            root = self.source_root
            label = "scenario directory"
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise GuestExecutionError(f"upload source escapes {label}: {relative!r}") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise GuestExecutionError(f"upload source is not a regular file: {candidate}")
        return candidate

    def _safe_download_destination(self, relative: str, *, publish: bool = False) -> Path:
        root = self.run_dir / "internal" / ("staged-evidence" if publish else "downloads")
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise GuestExecutionError(
                f"download destination escapes run directory: {relative!r}"
            ) from exc
        return candidate

    def validate_event(self, event: CompiledEvent) -> None:
        """Validate a built-in event without executing it."""
        self._require_node(event.from_, event.action)
        if event.from_ is None or not self.guests.has_profile(event.from_):
            raise GuestExecutionError(
                f"node {event.from_!r} has no guest_profile for action {event.action!r}"
            )
        models: dict[str, type[_Params]] = {
            "guest.exec": ExecParams,
            "guest.start": StartParams,
            "guest.stop": StopParams,
            "guest.upload": UploadParams,
            "guest.copy": CopyParams,
            "guest.download": DownloadParams,
            "guest.mkdir": MkdirParams,
            "guest.wait": WaitParams,
            "guest.status": StatusParams,
        }
        try:
            model = models[event.action]
        except KeyError as exc:
            raise GuestExecutionError(f"unknown built-in guest action {event.action!r}") from exc
        params = model.model_validate(event.parameters)
        if isinstance(params, UploadParams):
            self._safe_source(params.source)
        elif isinstance(params, DownloadParams):
            self._safe_download_destination(params.destination)
        elif isinstance(params, CopyParams) and (
            event.to is None or not self.guests.has_profile(event.to)
        ):
            raise GuestExecutionError("guest.copy requires a guest 'to' node")

    def validate_traffic(self, action: CompiledTrafficAction) -> None:
        """Validate one recurring guest action without executing it."""
        if action.action not in self.ACTIONS:
            raise GuestExecutionError(f"recurring action {action.action!r} is not supported")
        if not self.guests.has_profile(action.node):
            raise GuestExecutionError(
                f"node {action.node!r} has no guest_profile for recurring action {action.action!r}"
            )
        models: dict[str, type[_Params]] = {
            "guest.exec": ExecParams,
            "guest.start": StartParams,
            "guest.stop": StopParams,
            "guest.upload": UploadParams,
            "guest.copy": CopyParams,
            "guest.download": DownloadParams,
            "guest.mkdir": MkdirParams,
            "guest.wait": WaitParams,
            "guest.status": StatusParams,
        }
        params = models[action.action].model_validate(action.parameters)
        if isinstance(params, UploadParams):
            self._safe_source(params.source)
        elif isinstance(params, DownloadParams):
            self._safe_download_destination(params.destination)
        elif isinstance(params, CopyParams) and (
            action.to is None or not self.guests.has_profile(action.to)
        ):
            raise GuestExecutionError("guest.copy requires a guest 'to' node")

    def validate_provision(self, action: CompiledProvisionAction) -> None:
        """Validate one out-of-band provisioning action before deployment starts."""
        if action.action not in {
            "guest.exec",
            "guest.start",
            "guest.upload",
            "guest.copy",
            "guest.mkdir",
        }:
            raise GuestExecutionError(f"provision action {action.action!r} is not supported")
        if not self.guests.has_profile(action.node):
            raise GuestExecutionError(
                f"node {action.node!r} has no guest_profile for provision action {action.action!r}"
            )
        models: dict[str, type[_Params]] = {
            "guest.exec": ExecParams,
            "guest.start": StartParams,
            "guest.upload": UploadParams,
            "guest.copy": CopyParams,
            "guest.mkdir": MkdirParams,
        }
        params = models[action.action].model_validate(action.parameters)
        if isinstance(params, UploadParams):
            self._safe_source(params.source)
        elif isinstance(params, CopyParams) and (
            action.to is None or not self.guests.has_profile(action.to)
        ):
            raise GuestExecutionError("guest.copy requires a guest 'to' node")

    async def handle_provision(self, action: CompiledProvisionAction) -> None:
        """Execute one provisioning action before student-visible capture begins."""
        if self.journal is not None:
            self.journal.record(
                "guest.provision.start",
                action=action.action,
                node=action.node,
                target=action.to,
                provision_sequence=action.sequence,
            )
        try:
            await self._execute(action.action, action.node, action.to, action.parameters)
        except BaseException as exc:
            if self.journal is not None:
                self.journal.record(
                    "guest.provision.failed",
                    action=action.action,
                    node=action.node,
                    target=action.to,
                    provision_sequence=action.sequence,
                    error=type(exc).__name__,
                    message=str(exc),
                )
            raise
        if self.journal is not None:
            self.journal.record(
                "guest.provision.completed",
                action=action.action,
                node=action.node,
                target=action.to,
                provision_sequence=action.sequence,
            )

    async def handle_event(self, event: CompiledEvent) -> None:
        """Execute one compiled event using a built-in generic guest primitive."""
        completion = self._event_results.get(event.id)
        try:
            if not await self._condition_matches(event):
                if self.journal is not None:
                    self.journal.record(
                        "guest.action.skipped",
                        action=event.action,
                        event_id=event.id,
                        condition_event=event.when.event if event.when is not None else None,
                    )
                if completion is not None and not completion.done():
                    completion.set_result(None)
                return
        except BaseException:
            if completion is not None and not completion.done():
                completion.set_result(None)
            raise
        if self.journal is not None:
            self.journal.record(
                "guest.action.start",
                action=event.action,
                event_id=event.id,
                node=event.from_,
                target=event.to,
            )
        try:
            result = await self._execute(event.action, event.from_, event.to, event.parameters)
        except BaseException as exc:
            if completion is not None and not completion.done():
                completion.set_result(None)
            if self.journal is not None:
                self.journal.record(
                    "guest.action.failed",
                    action=event.action,
                    event_id=event.id,
                    node=event.from_,
                    target=event.to,
                    error=type(exc).__name__,
                    message=str(exc),
                )
            raise
        if completion is not None and not completion.done():
            completion.set_result(result)
        if self.journal is not None:
            self.journal.record(
                "guest.action.completed",
                action=event.action,
                event_id=event.id,
                node=event.from_,
                target=event.to,
            )

    async def handle_traffic(self, action: CompiledTrafficAction) -> None:
        """Execute one recurring generic guest action."""
        parameters = dict(action.parameters)
        if action.action == "guest.exec":
            parameters.setdefault("timeout", 60.0)
        if self.journal is not None:
            self.journal.record(
                "guest.traffic.start",
                action=action.action,
                assignment=action.assignment,
                node=action.node,
                target=action.to,
            )
        try:
            await self._execute(action.action, action.node, action.to, parameters)
        except BaseException as exc:
            if self.journal is not None:
                self.journal.record(
                    "guest.traffic.failed",
                    action=action.action,
                    assignment=action.assignment,
                    node=action.node,
                    target=action.to,
                    error=type(exc).__name__,
                )
            raise
        if self.journal is not None:
            self.journal.record(
                "guest.traffic.completed",
                action=action.action,
                assignment=action.assignment,
                node=action.node,
                target=action.to,
            )

    def _context_env(self, node: str, target: str | None) -> dict[str, str]:
        env = {
            "LABFOUNDRY_SEED": str(self.guests.plan.root_seed),
            "LABFOUNDRY_SCENARIO_ID": self.guests.plan.scenario_id,
            "LABFOUNDRY_SCENARIO_START": self.guests.plan.scenario_start.isoformat(),
            "LABFOUNDRY_NODE": node,
        }
        try:
            source_ip = self.guests.node_ip(node, peer=target)
        except TypeError:
            source_ip = self.guests.node_ip(node)
        if source_ip is not None:
            env["LABFOUNDRY_NODE_IP"] = source_ip
        if target is not None:
            env["LABFOUNDRY_TARGET"] = target
            try:
                target_ip = self.guests.node_ip(target, peer=node)
            except TypeError:
                target_ip = self.guests.node_ip(target)
            if target_ip is not None:
                env["LABFOUNDRY_TARGET_IP"] = target_ip
        return env

    async def _condition_matches(self, event: CompiledEvent) -> bool:
        if event.when is None:
            return True
        try:
            completion = self._event_results[event.when.event]
        except KeyError as exc:
            raise GuestExecutionError(
                f"condition source event {event.when.event!r} was not prepared"
            ) from exc
        result = await completion
        if result is None:
            raise GuestExecutionError(
                f"condition source event {event.when.event!r} did not return a command result"
            )
        if event.when.exit_code is not None and result.exit_code != event.when.exit_code:
            return False
        if (
            event.when.stdout_matches is not None
            and re.search(event.when.stdout_matches, result.stdout) is None
        ):
            return False
        return (
            event.when.stderr_matches is None
            or re.search(event.when.stderr_matches, result.stderr) is not None
        )

    async def _execute(
        self, action: str, node_ref: str | None, target: str | None, raw: dict[str, Any]
    ) -> ExecResult | None:
        node = self._require_node(node_ref, action)
        context_env = self._context_env(node, target)
        if action == "guest.exec":
            params = ExecParams.model_validate(raw)
            timeout = params.timeout_seconds()
            delay = params.retry.delay_seconds()
            last_error: GuestExecutionError | None = None
            for attempt in range(params.retry.attempts):
                try:
                    if params.argv is not None:
                        result = await self.guests.exec_argv(
                            node,
                            params.argv,
                            env={**params.env, **context_env},
                            cwd=params.cwd,
                            timeout=timeout,
                            check=params.check,
                        )
                    else:
                        assert params.script is not None
                        result = await self.guests.exec_shell(
                            node,
                            params.script,
                            env={**params.env, **context_env},
                            cwd=params.cwd,
                            timeout=timeout,
                            check=params.check,
                        )
                    return result
                except GuestExecutionError as exc:
                    last_error = exc
                    if attempt + 1 >= params.retry.attempts:
                        raise
                    if delay > 0:
                        await asyncio.sleep(delay)
                    delay *= params.retry.backoff
            assert last_error is not None  # pragma: no cover - attempts is constrained >= 1
            raise last_error
        if action == "guest.start":
            start_params = StartParams.model_validate(raw)
            await self.guests.start_process(
                node,
                start_params.id,
                start_params.script,
                env={**start_params.env, **context_env},
                cwd=start_params.cwd,
            )
            if start_params.ready is not None:
                await self.guests.wait_process_ready(
                    node,
                    start_params.id,
                    start_params.ready.script,
                    timeout=start_params.ready.timeout_seconds(),
                    interval=start_params.ready.interval_seconds(),
                )
            return None
        if action == "guest.stop":
            stop_params = StopParams.model_validate(raw)
            await self.guests.stop_process(node, stop_params.id)
            return None
        if action == "guest.upload":
            upload_params = UploadParams.model_validate(raw)
            await self.guests.upload(
                node,
                self._safe_source(upload_params.source),
                upload_params.destination,
            )
            return None
        if action == "guest.copy":
            copy_params = CopyParams.model_validate(raw)
            if target is None or not self.guests.has_profile(target):
                raise GuestExecutionError("guest.copy requires a guest 'to' node")
            with tempfile.TemporaryDirectory(prefix="labfoundry-copy-") as directory:
                local = Path(directory) / "source"
                await self.guests.download(node, copy_params.source, local)
                await self.guests.upload(target, local, copy_params.destination)
            return None
        if action == "guest.download":
            download_params = DownloadParams.model_validate(raw)
            destination = self._safe_download_destination(
                download_params.destination, publish=download_params.publish
            )
            if download_params.refresh_at_end:
                self._final_downloads[destination] = (node, download_params.source)
            await self.guests.download(
                node,
                download_params.source,
                destination,
            )
            return None
        if action == "guest.mkdir":
            mkdir_params = MkdirParams.model_validate(raw)
            await self.guests.mkdir(node, mkdir_params.path)
            return None
        if action == "guest.wait":
            wait_params = WaitParams.model_validate(raw)
            await self.guests.wait_process(
                node,
                wait_params.id,
                timeout=wait_params.timeout_seconds(),
                interval=wait_params.interval_seconds(),
            )
            return None
        if action == "guest.status":
            status_params = StatusParams.model_validate(raw)
            actual = await self.guests.process_running(node, status_params.id)
            if actual is not status_params.running:
                raise GuestExecutionError(
                    f"process {status_params.id!r} on {node!r} running={actual}; "
                    f"expected {status_params.running}"
                )
            return None
        raise GuestExecutionError(f"unknown built-in guest action {action!r}")
