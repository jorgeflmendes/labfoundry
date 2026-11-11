import asyncio
import json
import struct
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.errors import EvidenceError
from labfoundry.evidence.capture import CaptureManager
from labfoundry.evidence.pcap import PcapTimestampMapper
from labfoundry.schema.loader import load_scenario


def _deployment(plan_links: tuple[str, ...]) -> DeploymentState:
    return DeploymentState(
        project_id="project-id",
        project_name="lf-test",
        gns3_version="2.2.61",
        link_ids={name: f"link-{index}" for index, name in enumerate(plan_links)},
    )


def _pcap_bytes() -> bytes:
    payload = b"\xaa\xbb\xcc\xdd\xee\xff\x00\x11\x22\x33\x44\x55\x08\x00"
    return (
        b"\xd4\xc3\xb2\xa1"
        + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
        + struct.pack("<IIII", 1_000, 0, len(payload), len(payload))
        + payload
    )


@pytest.mark.asyncio
async def test_capture_restart_keeps_packets_from_both_streams(tmp_path, scenario_path):
    original = ScenarioCompiler().compile(load_scenario(scenario_path))
    plan = original.model_copy(update={"captures": original.captures[:1]})
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    stopped = asyncio.Event()
    generation = 0

    async def stream(_project, _link, destination, *, ready):
        nonlocal generation
        data = bytearray(_pcap_bytes())
        struct.pack_into("<I", data, 24, 1000 + generation)
        generation += 1
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        ready.set()
        await stopped.wait()

    async def stop(*args):
        stopped.set()

    client = SimpleNamespace(
        start_capture=AsyncMock(),
        stop_capture=AsyncMock(side_effect=stop),
        stream_capture_to=stream,
        timeout=1,
    )
    manager = CaptureManager(client)
    await manager.start_all(plan, deployment, run_dir=tmp_path)
    manager.prepare_restart(plan)
    stopped.set()
    await asyncio.gather(*manager._streams.values())
    await manager.pause(plan, deployment, run_dir=tmp_path)
    assert manager._paused
    stopped = asyncio.Event()
    await manager.resume(plan, deployment, run_dir=tmp_path)
    result = await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)
    records = list(PcapTimestampMapper.iter_records(result[0].final_path))
    assert len(records) == 2
    assert records[1].timestamp - records[0].timestamp == 1
    assert list((tmp_path / "internal/raw/fragments").glob("*.pcap"))


def test_capture_fragment_format_change_does_not_overwrite_source(tmp_path):
    first, second = tmp_path / "first.pcap", tmp_path / "second.pcap"
    first.write_bytes(_pcap_bytes())
    changed = bytearray(_pcap_bytes())
    struct.pack_into("<I", changed, 20, 101)
    second.write_bytes(changed)
    with pytest.raises(EvidenceError, match="format changed"):
        CaptureManager._join_fragments((first, second), second)
    assert second.read_bytes() == changed


@pytest.mark.asyncio
async def test_capture_finalizer_runs_after_all_sources_stop(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    stopped = []

    async def finalize(_project_id: str, link_id: str, destination: Path) -> None:
        destination.write_bytes(_pcap_bytes())
        stopped.append(link_id)

    async def after_capture() -> None:
        assert len(stopped) == len(plan.captures)
        assert not (tmp_path / "evidence").exists()
        keys = tmp_path / "internal/staged-evidence/ssl_keys/browser.keys"
        keys.parent.mkdir()
        keys.write_text("", encoding="ascii")

    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    await manager.stop_download_and_retime(
        plan,
        deployment,
        run_dir=tmp_path,
        after_capture=after_capture,
    )
    assert (tmp_path / "evidence/ssl_keys/browser.keys").exists()
    clock = json.loads((tmp_path / "internal/capture-clock.json").read_text())
    assert clock["raw_anchor"] == "1000"
    assert clock["scenario_anchor"] == plan.scenario_start.isoformat()
    assert clock["segments"] == []
    assert any(item.code == "empty-tls-keylog" for item in manager.last_audit.findings)


@pytest.mark.asyncio
async def test_capture_clock_survives_finalizer_failure(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))

    async def finalize(_project_id, _link_id, destination):
        destination.write_bytes(_pcap_bytes())

    async def after_capture():
        raise EvidenceError("cleanup failed")

    clock = SimpleNamespace(
        segments=[
            SimpleNamespace(
                raw_start=Decimal("1000.123456789"),
                scenario_start=plan.scenario_start,
                speed=1.0,
            )
        ]
    )
    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    with pytest.raises(EvidenceError, match="cleanup failed"):
        await manager.stop_download_and_retime(
            plan, deployment, run_dir=tmp_path, clock=clock, after_capture=after_capture
        )
    saved = json.loads((tmp_path / "internal/capture-clock.json").read_text())
    assert saved["segments"][0]["raw_start"] == "1000.123456789"
    assert not (tmp_path / "evidence").exists()


@pytest.mark.asyncio
async def test_capture_clock_survives_export_failure(
    tmp_path: Path, monkeypatch, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))

    async def finalize(_project_id, _link_id, destination):
        destination.write_bytes(_pcap_bytes())

    def fail_export(*args, **kwargs):
        raise EvidenceError("export failed")

    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    monkeypatch.setattr(manager, "_export_capture", fail_export)
    clock = SimpleNamespace(
        segments=[
            SimpleNamespace(
                raw_start=Decimal("1000.123456789"),
                scenario_start=plan.scenario_start,
                speed=1.0,
            )
        ]
    )
    with pytest.raises(EvidenceError, match="export failed"):
        await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path, clock=clock)
    saved = json.loads((tmp_path / "internal/capture-clock.json").read_text())
    assert saved["segments"] == [
        {
            "raw_start": "1000.123456789",
            "scenario_start": plan.scenario_start.isoformat(),
            "speed": 1.0,
        }
    ]
    assert not (tmp_path / "evidence").exists()


@pytest.mark.asyncio
async def test_capture_manager_preserves_and_rejects_truncated_sources(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    damaged = _pcap_bytes() + b"\x01\x02"

    async def finalize(_project_id: str, _link_id: str, destination: Path) -> None:
        destination.write_bytes(damaged)

    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    with pytest.raises(EvidenceError, match="truncated packet header"):
        await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert not (tmp_path / "evidence").exists()
    assert all(path.read_bytes() == damaged for path in (tmp_path / "internal/raw").glob("*"))


@pytest.mark.asyncio
async def test_capture_start_rolls_back_already_started_links(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    client = SimpleNamespace(
        start_capture=AsyncMock(side_effect=[None, RuntimeError("second capture failed")]),
        stop_capture=AsyncMock(return_value=None),
    )
    manager = CaptureManager(client)

    with pytest.raises(RuntimeError, match="second capture failed"):
        await manager.start_all(plan, deployment)

    client.stop_capture.assert_awaited_once_with("project-id", "link-0")


@pytest.mark.asyncio
async def test_capture_stop_attempts_every_link_before_failing(
    scenario_path: Path, tmp_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    client = SimpleNamespace(
        finalize_capture_to=AsyncMock(side_effect=[RuntimeError("first failed"), None]),
    )
    manager = CaptureManager(client)

    with pytest.raises(EvidenceError, match="failed to finalize captures safely"):
        await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert client.finalize_capture_to.await_count == 2


@pytest.mark.asyncio
async def test_capture_manager_uses_atomic_finalize(tmp_path: Path, scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    finalized: list[str] = []

    async def finalize(_project_id: str, link_id: str, destination: Path) -> None:
        finalized.append(link_id)
        destination.write_bytes(_pcap_bytes())

    client = SimpleNamespace(finalize_capture_to=finalize)
    manager = CaptureManager(client)

    await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert finalized == ["link-0", "link-1"]


@pytest.mark.asyncio
async def test_capture_manager_drains_streams_while_capture_is_active(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    stopped = {link_id: asyncio.Event() for link_id in deployment.link_ids.values()}

    async def stream(
        _project_id: str,
        link_id: str,
        destination: Path,
        *,
        ready: asyncio.Event,
    ) -> None:
        ready.set()
        await stopped[link_id].wait()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_pcap_bytes())

    async def stop(_project_id: str, link_id: str) -> None:
        stopped[link_id].set()

    client = SimpleNamespace(
        start_capture=AsyncMock(return_value=None),
        stream_capture_to=stream,
        stop_capture=AsyncMock(side_effect=stop),
    )
    manager = CaptureManager(client)

    await manager.start_all(plan, deployment, run_dir=tmp_path)
    assert len(manager._streams) == 2

    exported = await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert len(exported) == 2
    assert client.stop_capture.await_count == 2


@pytest.mark.asyncio
async def test_capture_manager_rejects_an_early_closed_stream(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    release = asyncio.Event()

    async def stream(
        _project_id: str,
        _link_id: str,
        destination: Path,
        *,
        ready: asyncio.Event,
    ) -> None:
        ready.set()
        await release.wait()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_pcap_bytes())

    client = SimpleNamespace(
        start_capture=AsyncMock(),
        stream_capture_to=stream,
        stop_capture=AsyncMock(),
    )
    manager = CaptureManager(client)

    await manager.start_all(plan, deployment, run_dir=tmp_path)
    release.set()
    await asyncio.sleep(0)
    with pytest.raises(EvidenceError, match="ended before shutdown"):
        await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert client.stop_capture.await_count == 2


@pytest.mark.asyncio
async def test_capture_manager_recovers_an_interrupted_capture_stream(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))
    stopped = {link_id: asyncio.Event() for link_id in deployment.link_ids.values()}

    async def stream(
        _project_id: str,
        link_id: str,
        destination: Path,
        *,
        ready: asyncio.Event,
    ) -> None:
        ready.set()
        await stopped[link_id].wait()
        if link_id == "link-1":
            raise RuntimeError("compute stream closed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_pcap_bytes())

    async def download(_project_id: str, _link_id: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(_pcap_bytes())

    async def stop(_project_id: str, link_id: str) -> None:
        stopped[link_id].set()

    client = SimpleNamespace(
        start_capture=AsyncMock(),
        stream_capture_to=stream,
        stop_capture=AsyncMock(side_effect=stop),
        download_pcap_to=download,
    )
    manager = CaptureManager(client)

    await manager.start_all(plan, deployment, run_dir=tmp_path)
    exported = await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert len(exported) == 2


@pytest.mark.asyncio
async def test_capture_manager_publishes_audited_multi_interface_pcapng(
    tmp_path: Path, scenario_data: dict[str, Any]
) -> None:
    from labfoundry.evidence.pcap import PcapTimestampMapper
    from labfoundry.schema.scenario import ScenarioSpec

    raw = scenario_data
    raw["captures"] = [
        {
            "name": "combined",
            "format": "pcapng",
            "timestamp_policy": "preserve",
            "endpoints": [
                {"name": "users", "node": "gateway", "interface": "users"},
                {"name": "wan", "node": "gateway", "interface": "wan"},
            ],
        }
    ]
    plan = ScenarioCompiler().compile(ScenarioSpec.model_validate(raw))
    deployment = _deployment(
        tuple(endpoint.link for capture in plan.captures for endpoint in capture.endpoints)
    )

    async def finalize(_project_id: str, _link_id: str, destination: Path) -> None:
        destination.write_bytes(_pcap_bytes())

    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    nested = tmp_path / "internal" / "staged-evidence" / "mail-keys" / "adelino.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("test-key", encoding="utf-8")
    exported = await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert exported[0].final_path.suffix == ".pcapng"
    assert len(list(PcapTimestampMapper.iter_records(exported[0].final_path))) == 2
    assert manager.last_audit is not None
    assert (tmp_path / "internal" / "realism-audit.json").is_file()
    assert (tmp_path / "evidence" / "mail-keys" / "adelino.json").read_text() == "test-key"


@pytest.mark.asyncio
async def test_capture_manager_reports_empty_endpoint_in_merged_capture(
    tmp_path: Path, scenario_data: dict[str, Any]
) -> None:
    from labfoundry.schema.scenario import ScenarioSpec

    raw = scenario_data
    raw["captures"] = [
        {
            "name": "combined",
            "format": "pcapng",
            "timestamp_policy": "preserve",
            "endpoints": [
                {"name": "users", "node": "gateway", "interface": "users"},
                {"name": "wan", "node": "gateway", "interface": "wan"},
            ],
        }
    ]
    plan = ScenarioCompiler().compile(ScenarioSpec.model_validate(raw))
    deployment = _deployment(
        tuple(endpoint.link for capture in plan.captures for endpoint in capture.endpoints)
    )

    async def finalize(_project_id: str, link_id: str, destination: Path) -> None:
        if link_id == "link-0":
            destination.write_bytes(_pcap_bytes())
        else:
            destination.write_bytes(
                b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1)
            )

    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert manager.last_audit is not None
    assert any(
        finding.code == "empty-capture-endpoint" and "combined.wan" in finding.message
        for finding in manager.last_audit.findings
    )


@pytest.mark.asyncio
async def test_capture_manager_keeps_rejected_evidence_internal(
    tmp_path: Path, scenario_path: Path
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    deployment = _deployment(tuple(capture.link for capture in plan.captures))

    async def finalize(_project_id: str, _link_id: str, destination: Path) -> None:
        destination.write_bytes(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHIIII", 2, 4, 0, 0, 65535, 1))

    manager = CaptureManager(SimpleNamespace(finalize_capture_to=finalize))
    with pytest.raises(EvidenceError, match="realism audit rejected evidence"):
        await manager.stop_download_and_retime(plan, deployment, run_dir=tmp_path)

    assert not (tmp_path / "evidence").exists()
    assert (tmp_path / "internal" / "realism-audit.json").is_file()
