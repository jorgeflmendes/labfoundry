"""Capture lifecycle, multi-interface export, and evidence audit for GNS3 links."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.compiler.plan import CompiledCapture, CompiledCaptureEndpoint, ScenarioPlan
from labfoundry.errors import EvidenceError
from labfoundry.evidence.audit import AuditFinding, RealismAuditResult, audit_captures
from labfoundry.evidence.pcap import PcapNgSource, PcapNgWriter, PcapTimestampMapper


@dataclass(frozen=True, slots=True)
class ExportedCapture:
    """Metadata describing one published evidence capture."""

    name: str
    raw_paths: tuple[Path, ...]
    final_path: Path
    packets: int
    output_format: str
    timestamp_policy: str

    @property
    def raw_path(self) -> Path:
        """Return the sole raw path for legacy single-interface consumers."""
        if len(self.raw_paths) != 1:
            raise EvidenceError(f"capture {self.name!r} has multiple raw interface files")
        return self.raw_paths[0]


class CaptureManager:
    """Coordinate GNS3 link captures, staged evidence export, and realism audit."""

    def __init__(self, client: GNS3Client, *, source_root: Path | None = None) -> None:
        self.client = client
        self.source_root = None if source_root is None else source_root.resolve()
        self._streams: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._fragments: dict[tuple[str, str], list[Path]] = {}
        self._paused: set[tuple[str, str]] = set()
        self.last_audit: RealismAuditResult | None = None

    async def start_all(
        self,
        plan: ScenarioPlan,
        deployment: DeploymentState,
        *,
        run_dir: Path | None = None,
    ) -> None:
        """Start every capture endpoint declared by the compiled plan."""
        started_link_ids: list[str] = []
        started_keys: list[tuple[str, str]] = []
        try:
            for capture in plan.captures:
                for endpoint in capture.endpoints:
                    link_id = deployment.link_ids[endpoint.link]
                    key = (capture.name, endpoint.name)
                    generation = len(self._fragments.get(key, ()))
                    suffix = f"-{generation}" if generation else ""
                    await self.client.start_capture(
                        deployment.project_id,
                        link_id,
                        data_link_type=capture.data_link_type,
                        capture_file_name=f"{capture.name}-{endpoint.name}{suffix}.pcap",
                    )
                    started_link_ids.append(link_id)
                    if run_dir is not None:
                        raw_path = self._raw_path(run_dir, capture, endpoint)
                        ready = asyncio.Event()
                        task = asyncio.create_task(
                            self.client.stream_capture_to(
                                deployment.project_id, link_id, raw_path, ready=ready
                            )
                        )
                        self._streams[(capture.name, endpoint.name)] = task
                        started_keys.append(key)
                        await ready.wait()
                        if task.done():
                            task.result()
                            raise EvidenceError(
                                f"capture stream {capture.name}.{endpoint.name} "
                                "ended during startup"
                            )
        except Exception as exc:
            cleanup_errors: list[str] = []
            for link_id in reversed(started_link_ids):
                try:
                    await self.client.stop_capture(deployment.project_id, link_id)
                except Exception as cleanup_exc:
                    cleanup_errors.append(f"{link_id}: {cleanup_exc}")
            tasks = [self._streams.pop(key) for key in started_keys]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if cleanup_errors:
                exc.add_note("capture rollback also failed: " + "; ".join(cleanup_errors))
            raise

    @staticmethod
    def _selected(
        plan: ScenarioPlan, nodes: tuple[str, ...] | None
    ) -> list[tuple[CompiledCapture, CompiledCaptureEndpoint]]:
        links = {
            link.name
            for link in plan.links
            if nodes is None or link.a.node in nodes or link.b.node in nodes
        }
        return [
            (capture, endpoint)
            for capture in plan.captures
            for endpoint in capture.endpoints
            if nodes is None or endpoint.link in links
        ]

    def prepare_restart(self, plan: ScenarioPlan, nodes: tuple[str, ...] | None = None) -> None:
        """Check active streams before a planned node or snapshot interruption."""
        for capture, endpoint in self._selected(plan, nodes):
            task = self._streams.get((capture.name, endpoint.name))
            if task is not None and task.done():
                task.result()
                raise EvidenceError(
                    f"capture stream {capture.name}.{endpoint.name} ended unexpectedly"
                )

    async def pause(
        self,
        plan: ScenarioPlan,
        deployment: DeploymentState,
        *,
        run_dir: Path,
        nodes: tuple[str, ...] | None = None,
    ) -> None:
        """Drain affected captures after their endpoints stop, retaining complete raw fragments."""
        for capture, endpoint in self._selected(plan, nodes):
            key = (capture.name, endpoint.name)
            if key in self._paused:
                continue
            raw = self._raw_path(run_dir, capture, endpoint)
            link = deployment.link_ids[endpoint.link]
            task = self._streams.pop(key, None)
            if task is None:
                await self.client.finalize_capture_to(deployment.project_id, link, raw)
            else:
                await self.client.stop_capture(deployment.project_id, link)
                try:
                    await asyncio.wait_for(task, timeout=self.client.timeout)
                except Exception:
                    await self.client.download_pcap_to(deployment.project_id, link, raw)
            sum(1 for _record in PcapTimestampMapper.iter_records(raw))
            fragments = self._fragments.setdefault(key, [])
            target = (
                raw.parent / "fragments" / f"{capture.name}.{endpoint.name}.{len(fragments)}.pcap"
            )
            target.parent.mkdir(exist_ok=True)
            raw.replace(target)
            fragments.append(target)
            self._paused.add(key)

    async def resume(
        self,
        plan: ScenarioPlan,
        deployment: DeploymentState,
        *,
        run_dir: Path,
        nodes: tuple[str, ...] | None = None,
    ) -> None:
        """Restart paused streams before their guests resume sending packets."""
        selected = {
            (capture.name, endpoint.name) for capture, endpoint in self._selected(plan, nodes)
        }
        keys = self._paused & selected
        captures = tuple(
            capture.model_copy(
                update={
                    "endpoints": tuple(
                        endpoint
                        for endpoint in capture.endpoints
                        if (capture.name, endpoint.name) in keys
                    )
                }
            )
            for capture in plan.captures
            if any((capture.name, endpoint.name) in keys for endpoint in capture.endpoints)
        )
        if captures:
            await self.start_all(
                plan.model_copy(update={"captures": captures}), deployment, run_dir=run_dir
            )
            self._paused.difference_update(keys)

    @staticmethod
    def _join_fragments(sources: Sequence[Path], destination: Path) -> None:
        temporary = destination.with_name(f".{destination.name}.joined")
        try:
            header = PcapTimestampMapper.classic_header(sources[0])
            with temporary.open("wb") as output:
                for index, source in enumerate(sources):
                    if PcapTimestampMapper.classic_header(source) != header:
                        raise EvidenceError("capture format changed across a node restart")
                    with source.open("rb") as handle:
                        raw_header = handle.read(24)
                        if index == 0:
                            output.write(raw_header)
                        shutil.copyfileobj(handle, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            sum(1 for _record in PcapTimestampMapper.iter_records(temporary))
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    async def stop_download_and_retime(
        self,
        plan: ScenarioPlan,
        deployment: DeploymentState,
        *,
        run_dir: Path,
        clock: Any | None = None,
        after_capture: Callable[[], Awaitable[None]] | None = None,
    ) -> list[ExportedCapture]:
        """Finalize GNS3 captures, audit them, and publish only accepted evidence."""
        raw_dir = run_dir / "internal" / "raw"
        staged_dir = run_dir / "internal" / "staged-evidence"
        evidence_dir = run_dir / "evidence"
        raw_dir.mkdir(parents=True, exist_ok=True)
        staged_dir.mkdir(parents=True, exist_ok=True)

        raw_files: dict[str, list[tuple[CompiledCaptureEndpoint, Path]]] = {}
        finalize_errors: list[str] = []
        for capture in plan.captures:
            endpoints: list[tuple[CompiledCaptureEndpoint, Path]] = []
            for endpoint in capture.endpoints:
                raw_path = self._raw_path(run_dir, capture, endpoint)
                link_id = deployment.link_ids[endpoint.link]
                try:
                    key = (capture.name, endpoint.name)
                    task = self._streams.pop(key, None)
                    paused = key in self._paused
                    if paused:
                        self._join_fragments(self._fragments.pop(key), raw_path)
                        self._paused.remove(key)
                    elif task is None:
                        await self.client.finalize_capture_to(
                            deployment.project_id, link_id, raw_path
                        )
                    elif task.done():
                        try:
                            task.result()
                        finally:
                            await self.client.stop_capture(deployment.project_id, link_id)
                        raise EvidenceError(
                            f"capture stream {capture.name}.{endpoint.name} ended before shutdown"
                        )
                    else:
                        await self.client.stop_capture(deployment.project_id, link_id)
                        try:
                            await task
                        except Exception as stream_error:
                            try:
                                await self.client.download_pcap_to(
                                    deployment.project_id, link_id, raw_path
                                )
                            except Exception as fallback_error:
                                stream_error.add_note(
                                    f"capture download fallback also failed: {fallback_error}"
                                )
                                raise
                    # A truncated stream is not a complete source of evidence.
                    sum(1 for _record in PcapTimestampMapper.iter_records(raw_path))
                    fragments = self._fragments.pop(key, [])
                    if fragments:
                        self._join_fragments((*fragments, raw_path), raw_path)
                except Exception as exc:
                    finalize_errors.append(f"{capture.name}.{endpoint.name}: {exc}")
                else:
                    endpoints.append((endpoint, raw_path))
            raw_files[capture.name] = endpoints
        if finalize_errors:
            raise EvidenceError("failed to finalize captures safely: " + "; ".join(finalize_errors))

        all_paths = [path for items in raw_files.values() for _endpoint, path in items]
        first_timestamps = [PcapTimestampMapper.first_timestamp_or_none(path) for path in all_paths]
        non_empty_anchors = [timestamp for timestamp in first_timestamps if timestamp is not None]
        raw_anchor = min(non_empty_anchors) if non_empty_anchors else Decimal(0)

        segments: Sequence[tuple[Decimal, datetime, float]] = ()
        if clock is not None and getattr(clock, "segments", None):
            segments = tuple(
                (seg.raw_start, seg.scenario_start, seg.speed) for seg in clock.segments
            )
        (run_dir / "internal" / "capture-clock.json").write_text(
            json.dumps(
                {
                    "raw_anchor": str(raw_anchor),
                    "scenario_anchor": plan.scenario_start.isoformat(),
                    "speed": plan.clock_speed,
                    "segments": [
                        {
                            "raw_start": str(raw),
                            "scenario_start": scenario.isoformat(),
                            "speed": speed,
                        }
                        for raw, scenario, speed in segments
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        if after_capture is not None:
            await after_capture()

        staged: list[ExportedCapture] = []
        for capture in plan.captures:
            source_items = raw_files[capture.name]
            raw_paths = tuple(path for _endpoint, path in source_items)
            suffix = ".pcapng" if capture.output_format == "pcapng" else ".pcap"
            final_path = staged_dir / f"{capture.name}{suffix}"
            packets = self._export_capture(
                capture,
                source_items,
                destination=final_path,
                raw_anchor=raw_anchor,
                scenario_start=plan.scenario_start,
                speed=plan.clock_speed,
                segments=segments,
                rewrite_tcp_timestamps=plan.warp_gaps or plan.clock_speed != 1.0,
            )
            staged.append(
                ExportedCapture(
                    name=capture.name,
                    raw_paths=raw_paths,
                    final_path=final_path,
                    packets=packets,
                    output_format=capture.output_format,
                    timestamp_policy=capture.timestamp_policy,
                )
            )

        self.last_audit = self._audit(
            plan, [item.final_path for item in staged], run_dir, raw_files
        )
        if self.last_audit is not None and self.last_audit.should_fail(plan.realism_audit.fail_on):
            codes = ", ".join(item.code for item in self.last_audit.findings)
            raise EvidenceError(f"realism audit rejected evidence: {codes}")

        evidence_dir.mkdir(parents=True, exist_ok=True)
        exported: list[ExportedCapture] = []
        for item in staged:
            published = evidence_dir / item.final_path.name
            item.final_path.replace(published)
            exported.append(
                ExportedCapture(
                    name=item.name,
                    raw_paths=item.raw_paths,
                    final_path=published,
                    packets=item.packets,
                    output_format=item.output_format,
                    timestamp_policy=item.timestamp_policy,
                )
            )
        published_names = {item.final_path.name for item in exported}
        for artifact in staged_dir.rglob("*"):
            if artifact.is_file() and not artifact.is_symlink():
                relative = artifact.relative_to(staged_dir)
                if relative.name in published_names and len(relative.parts) == 1:
                    continue
                destination = evidence_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                artifact.replace(destination)
        return exported

    @staticmethod
    def _raw_path(
        run_dir: Path, capture: CompiledCapture, endpoint: CompiledCaptureEndpoint
    ) -> Path:
        return run_dir / "internal" / "raw" / f"{capture.name}.{endpoint.name}.raw.pcap"

    @staticmethod
    def _export_capture(
        capture: CompiledCapture,
        source_items: list[tuple[CompiledCaptureEndpoint, Path]],
        *,
        destination: Path,
        raw_anchor: Decimal,
        scenario_start: datetime,
        speed: float,
        segments: Sequence[tuple[Decimal, datetime, float]] = (),
        rewrite_tcp_timestamps: bool = False,
    ) -> int:
        if capture.output_format == "pcap":
            if len(source_items) != 1:
                raise EvidenceError("classic PCAP export requires one capture endpoint")
            raw_path = source_items[0][1]
            if capture.timestamp_policy == "preserve":
                PcapTimestampMapper.copy_without_retime(raw_path, destination)
                return sum(1 for _record in PcapTimestampMapper.iter_records(raw_path))
            return PcapTimestampMapper.retime(
                raw_path,
                destination,
                raw_anchor=raw_anchor,
                scenario_anchor=scenario_start,
                speed=speed,
                rewrite_tcp_timestamps=rewrite_tcp_timestamps,
                segments=segments,
            )
        minimal_metadata = capture.metadata_policy == "minimal"
        return PcapNgWriter.write(
            destination,
            sources=tuple(
                PcapNgSource(
                    path=path,
                    name=f"interface-{index}" if minimal_metadata else endpoint.name,
                    description=None if minimal_metadata else endpoint.description,
                )
                for index, (endpoint, path) in enumerate(source_items, start=1)
            ),
            metadata=(
                {}
                if minimal_metadata
                else {
                    "comment": capture.metadata.comment,
                    "hardware": capture.metadata.hardware,
                    "os": capture.metadata.os,
                    "application": capture.metadata.application,
                }
            ),
            timestamp_policy=capture.timestamp_policy,
            raw_anchor=raw_anchor if capture.timestamp_policy == "retime" else None,
            scenario_anchor=scenario_start if capture.timestamp_policy == "retime" else None,
            speed=speed,
            segments=segments,
            rewrite_tcp_timestamps=rewrite_tcp_timestamps,
            deduplicate_packets=capture.deduplicate_packets,
        )

    def _audit(
        self,
        plan: ScenarioPlan,
        paths: list[Path],
        run_dir: Path,
        raw_files: dict[str, list[tuple[CompiledCaptureEndpoint, Path]]],
    ) -> RealismAuditResult | None:
        if not plan.realism_audit.enabled:
            return None
        baseline = self._baseline_path(plan.realism_audit.baseline)
        result = audit_captures(
            paths,
            baseline=baseline,
            clock_speed=plan.clock_speed,
            retimed=any(capture.timestamp_policy == "retime" for capture in plan.captures),
            warped=plan.warp_gaps,
            max_idle_gap_seconds=plan.max_idle_gap_seconds,
            keylog_files=tuple((run_dir / "internal" / "staged-evidence").rglob("*.keys")),
        )
        empty_endpoints = tuple(
            AuditFinding(
                "warning",
                "empty-capture-endpoint",
                f"{name}.{endpoint.name} captured no packets",
            )
            for name, items in raw_files.items()
            for endpoint, path in items
            if PcapTimestampMapper.first_timestamp_or_none(path) is None
        )
        if empty_endpoints:
            result = replace(result, findings=result.findings + empty_endpoints)
        result.write(run_dir / "internal" / "realism-audit.json")
        return result

    def _baseline_path(self, value: str | None) -> Path | None:
        if value is None:
            return None
        if self.source_root is None:
            raise EvidenceError("realism audit baseline requires a scenario source root")
        candidate = (self.source_root / value).resolve()
        try:
            candidate.relative_to(self.source_root)
        except ValueError as exc:
            raise EvidenceError(f"baseline escapes scenario directory: {value!r}") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise EvidenceError(f"baseline is not a regular file: {candidate}")
        return candidate
