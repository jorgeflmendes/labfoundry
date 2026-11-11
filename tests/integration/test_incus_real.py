"""Opt-in acceptance test for a prepared scenario VM and a local GNS3 compute."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from labfoundry.backends.gns3.backend import GNS3Backend
from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.backends.gns3.incus import IncusEnvironment
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.compiler.plan import CompiledEvent
from labfoundry.errors import GuestExecutionError
from labfoundry.evidence.capture import CaptureManager
from labfoundry.evidence.pcap import PcapTimestampMapper
from labfoundry.guest.base import GuestCommand
from labfoundry.guest.config import load_guest_inventory
from labfoundry.guest.manager import GuestManager
from labfoundry.runtime.clock import ScenarioClock
from labfoundry.runtime.infrastructure import InfrastructureActionDispatcher
from labfoundry.schema.scenario import ScenarioSpec

pytestmark = pytest.mark.integration


async def test_real_incus_files_clock_processes_and_ethernet_capture(tmp_path: Path) -> None:
    config = os.environ.get("LABFOUNDRY_TEST_INCUS_INVENTORY")
    image = os.environ.get("LABFOUNDRY_TEST_INCUS_IMAGE")
    if not config or not image:
        pytest.skip("set LABFOUNDRY_TEST_INCUS_INVENTORY and LABFOUNDRY_TEST_INCUS_IMAGE")
    profile = os.environ.get("LABFOUNDRY_TEST_INCUS_PROFILE", "incus")
    inventory_path = Path(config)
    inventory = load_guest_inventory(inventory_path)
    start = datetime(2019, 1, 2, tzinfo=UTC)
    nodes = {
        name: {
            "template": image,
            "template_type": "incus",
            "guest_profile": profile,
            "interfaces": {
                "eth0": {
                    "segment": "lan",
                    "ip": f"10.42.0.{index}/24",
                    "mac": f"00:1b:21:10:20:{index:02x}",
                }
            },
        }
        for name, index in (("left", 2), ("right", 3))
    }
    plan = ScenarioCompiler().compile(
        ScenarioSpec.model_validate(
            {
                "scenario": {"id": "incus-acceptance", "seed": 1},
                "clock": {"start": start, "sync_guests": True, "warp_gaps": True},
                "network": {"segments": {"lan": {"subnet": "10.42.0.0/24"}}, "nodes": nodes},
                "captures": [{"name": "wire", "endpoint": {"node": "left", "interface": "eth0"}}],
            }
        )
    )
    environment = IncusEnvironment.for_plan(plan, inventory, base_dir=inventory_path.parent)
    async with GNS3Client(
        os.environ.get("LABFOUNDRY_GNS3_URL", "http://127.0.0.1:3080"),
        username=os.environ.get("LABFOUNDRY_GNS3_USER"),
        password=os.environ.get("LABFOUNDRY_GNS3_PASSWORD"),
        timeout=120,
    ) as client:
        backend = GNS3Backend(client, environment=environment)
        deployment = await backend.deploy(plan)
        guests = GuestManager(plan, inventory, base_dir=inventory_path.parent)
        guests.bind_deployment(deployment)
        captures = CaptureManager(client)
        capturing = False
        clock = ScenarioClock(start, warp_gaps=True, settle_delay=0.1)
        try:
            await backend.start(deployment)
            await guests.healthcheck_all(timeout=120)
            await guests.configure_network()
            await guests.check_identity_uniqueness()
            payload = tmp_path / "payload"
            payload.write_bytes(bytes(range(256)) * 1000)
            await guests.upload("left", payload, "/tmp/lf-private-payload")
            result = tmp_path / "download"
            await guests.download("left", "/tmp/lf-private-payload", result)
            assert result.read_bytes() == payload.read_bytes()
            assert (
                await guests.exec_argv(
                    "right", ("/bin/sh", "-c", "test ! -e /tmp/lf-private-payload")
                )
            ).exit_code == 0
            executor = guests.executor("left")
            value = await executor.exec(
                GuestCommand(
                    "/bin/sh",
                    ("-c", 'printf "%s:%s" "$TOKEN" "$PWD"'),
                    env={"TOKEN": "a; b 'c'"},
                    cwd="/tmp",
                )
            )
            assert value.stdout == "a; b 'c':/tmp"
            marker = "/tmp/lf-timeout-marker"
            with pytest.raises(GuestExecutionError):
                await executor.exec(
                    GuestCommand(
                        "/bin/sh", ("-c", f"(sleep 2; echo leaked > {marker}) & wait"), timeout=0.3
                    )
                )
            await asyncio.sleep(2.2)
            await executor.exec(GuestCommand("/bin/sh", ("-c", f"test ! -e {marker}")))
            host_before = datetime.now(UTC).timestamp()
            await guests.set_clock_all(start)
            for name in nodes:
                now = await guests.exec_argv(name, ("/bin/date", "+%s"))
                assert abs(int(now.stdout) - start.timestamp()) < 10
            assert abs(datetime.now(UTC).timestamp() - host_before) < 10
            await guests.start_process(
                "left", "http", "exec python3 -m http.server 8080 --bind 10.42.0.2"
            )
            clock.start_runtime()
            await captures.start_all(plan, deployment, run_dir=tmp_path)
            capturing = True
            request = (
                "/usr/bin/python3",
                "-c",
                "import urllib.request; "
                "print(urllib.request.urlopen('http://10.42.0.2:8080/',timeout=10).status)",
            )
            assert (await guests.exec_argv("right", request, timeout=15)).stdout.strip() == "200"
            await clock.sleep_until(start + timedelta(minutes=5))
            await guests.set_clock_all(start + timedelta(minutes=5))
            assert (await guests.exec_argv("right", request, timeout=15)).stdout.strip() == "200"
            infrastructure = InfrastructureActionDispatcher(
                client,
                plan,
                deployment,
                backend=backend,
                guests=guests,
                captures=captures,
                run_dir=tmp_path,
            )
            await guests.exec_shell("left", "printf before > /var/lib/lf-checkpoint-state")
            for index, action in enumerate(("create", "restore", "delete")):
                if action == "restore":
                    await guests.exec_shell("left", "printf after > /var/lib/lf-checkpoint-state")
                event = CompiledEvent(
                    sequence=index,
                    id=f"snapshot-{action}",
                    timestamp=clock.now(),
                    action=f"gns3.snapshot.{action}",
                    parameters={"name": "acceptance"},
                )
                await infrastructure.handle_event(event)
                if action == "restore":
                    result = await guests.exec_argv(
                        "left", ("/bin/cat", "/var/lib/lf-checkpoint-state")
                    )
                    assert result.stdout == "before"
                    now = await guests.exec_argv("left", ("/bin/date", "+%Y"))
                    assert now.stdout.strip() == "2019"
            await guests.start_process(
                "left", "after-snapshot", "exec python3 -m http.server 8080 --bind 10.42.0.2"
            )
            assert (await guests.exec_argv("right", request, timeout=15)).stdout.strip() == "200"
            exported = await captures.stop_download_and_retime(
                plan, deployment, run_dir=tmp_path, clock=clock
            )
            capturing = False
            assert exported[0].packets > 0
            timestamps = [
                float(record.timestamp)
                for record in PcapTimestampMapper.iter_records(exported[0].final_path)
            ]
            assert timestamps[-1] - timestamps[0] >= 295
            assert timestamps == sorted(timestamps)
        finally:
            try:
                if capturing:
                    await captures.stop_download_and_retime(
                        plan, deployment, run_dir=tmp_path, clock=clock
                    )
                await guests.stop_all_processes()
                await backend.stop(deployment)
            finally:
                await backend.destroy(deployment)
