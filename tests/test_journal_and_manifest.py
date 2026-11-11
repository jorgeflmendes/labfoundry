from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from labfoundry.compiler.plan import ScenarioPlan
from labfoundry.groundtruth.manifest import sha256_file, write_internal_metadata
from labfoundry.runtime.journal import RunJournal


def _plan() -> ScenarioPlan:
    return ScenarioPlan(
        scenario_id="case",
        root_seed=7,
        scenario_start=datetime(2025, 1, 1, tzinfo=UTC),
        clock_speed=1.0,
        segments=(),
        nodes=(),
        links=(),
        events=(),
        traffic=(),
        captures=(),
        aliases={},
    )


def test_run_journal_writes_ordered_jsonl(tmp_path: Path) -> None:
    journal = RunJournal(tmp_path)
    journal.record("run.started", seed=7, sequence=99, timestamp="overridden")
    journal.record("run.completed", captures=2)

    rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2]
    assert [row["event"] for row in rows] == ["run.started", "run.completed"]
    assert rows[0]["seed"] == 7
    assert rows[0]["timestamp"] != "overridden"


def test_manifest_hashes_existing_evidence_files(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    pcap = evidence / "perimeter.pcap"
    pcap.write_bytes(b"pcap-bytes")

    write_internal_metadata(tmp_path, plan=_plan())

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifacts"] == [
        {
            "path": "evidence/perimeter.pcap",
            "size": len(b"pcap-bytes"),
            "sha256": sha256_file(pcap),
        }
    ]
    assert manifest["runtime"]["python"]
