"""Run manifest and reproducibility metadata."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

from labfoundry import __version__
from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.plan import ScenarioPlan


def canonical_plan_hash(plan: ScenarioPlan) -> str:
    """Return the stable SHA-256 hash of a canonical serialized scenario plan."""
    encoded = json.dumps(
        plan.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest of one file without loading it entirely into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        assert temporary_path is not None
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _artifact_records(run_dir: Path) -> list[dict[str, Any]]:
    evidence = run_dir / "evidence"
    if not evidence.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(evidence.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        records.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records


def write_internal_metadata(
    run_dir: Path,
    *,
    plan: ScenarioPlan,
    deployment: DeploymentState | None = None,
    extra_manifest: dict[str, Any] | None = None,
) -> None:
    """Atomically write private ground truth and reproducibility metadata with artifact hashes."""
    internal = run_dir / "internal"
    internal.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        internal / "scenario-plan.json",
        plan.model_dump_json(indent=2, by_alias=True),
    )
    ground_truth = {
        "scenario_id": plan.scenario_id,
        "seed": plan.root_seed,
        "aliases": plan.aliases,
        "events": [event.model_dump(mode="json", by_alias=True) for event in plan.events],
        "nodes": [node.model_dump(mode="json") for node in plan.nodes],
    }
    _atomic_write_text(
        internal / "ground-truth.json",
        json.dumps(ground_truth, indent=2, sort_keys=True),
    )

    manifest: dict[str, Any] = {
        "labfoundry_version": __version__,
        "scenario_id": plan.scenario_id,
        "seed": plan.root_seed,
        "scenario_start": plan.scenario_start.isoformat(),
        "clock_speed": plan.clock_speed,
        "scenario_plan_sha256": canonical_plan_hash(plan),
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
        },
        "artifacts": _artifact_records(run_dir),
    }
    if deployment is not None:
        _atomic_write_text(internal / "deployment.json", deployment.model_dump_json(indent=2))
        manifest["gns3_version"] = deployment.gns3_version
        manifest["gns3_project_id"] = deployment.project_id
        manifest["templates"] = deployment.template_ids
        manifest["template_metadata"] = deployment.template_metadata
        if deployment.environment:
            manifest["scenario_environment"] = deployment.environment
    if extra_manifest:
        manifest["extra"] = extra_manifest
    _atomic_write_text(
        run_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True),
    )
