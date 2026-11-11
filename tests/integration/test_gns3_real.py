"""Opt-in acceptance tests for a real GNS3 2.2.61 + VirtualBox host.

These tests are intentionally skipped unless LABFOUNDRY_INTEGRATION=1. They create
short-lived GNS3 projects and always attempt cleanup in ``finally`` blocks.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from labfoundry.backends.gns3.backend import DeploymentState
from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.compiler.plan import CompiledEvent
from labfoundry.errors import BackendError
from labfoundry.evidence.pcap import PcapNgSource, PcapNgWriter
from labfoundry.runtime.infrastructure import InfrastructureActionDispatcher
from labfoundry.schema.scenario import ScenarioSpec

pytestmark = pytest.mark.integration


def _enabled() -> bool:
    return os.environ.get("LABFOUNDRY_INTEGRATION") == "1"


def _client() -> GNS3Client:
    if not _enabled():
        pytest.skip("set LABFOUNDRY_INTEGRATION=1 to run real GNS3 integration tests")
    base_url = os.environ.get("LABFOUNDRY_GNS3_URL", "http://127.0.0.1:3080")
    username = os.environ.get("LABFOUNDRY_GNS3_USER")
    password = os.environ.get("LABFOUNDRY_GNS3_PASSWORD")
    verify_tls = os.environ.get("LABFOUNDRY_GNS3_VERIFY_TLS", "1") not in {"0", "false"}
    return GNS3Client(
        base_url,
        username=username,
        password=password,
        verify_tls=verify_tls,
        timeout=60.0,
    )


@pytest.mark.asyncio
async def test_real_controller_and_compute_contract() -> None:
    """Verify the supported controller version and the selected compute."""
    compute_id = os.environ.get("LABFOUNDRY_GNS3_COMPUTE", "local")
    async with _client() as client:
        version = await client.assert_supported_2_2()
        compute = await client.get_compute(compute_id)

    assert version.version
    assert compute.compute_id == compute_id
    assert compute.connected is not False
    assert "ethernet_switch" in compute.capabilities.node_types


@pytest.mark.asyncio
async def test_real_project_link_and_capture_round_trip(tmp_path: Path) -> None:
    """Create a temporary project, link two switches, capture, download, and delete it."""
    compute_id = os.environ.get("LABFOUNDRY_GNS3_COMPUTE", "local")
    async with _client() as client:
        project = await client.create_project(f"lf-integration-{uuid.uuid4().hex[:12]}")
        try:
            first = await client.create_ethernet_switch(
                project.project_id,
                name="lf-it-switch-a",
                compute_id=compute_id,
                port_count=1,
                x=-120,
                y=0,
            )
            second = await client.create_ethernet_switch(
                project.project_id,
                name="lf-it-switch-b",
                compute_id=compute_id,
                port_count=1,
                x=120,
                y=0,
            )
            link = await client.create_link(
                project.project_id,
                a_node_id=first.node_id,
                a_adapter=0,
                a_port=0,
                b_node_id=second.node_id,
                b_adapter=0,
                b_port=0,
            )
            await client.start_all_nodes(project.project_id)
            await client.start_capture(project.project_id, link.link_id)
            ready = asyncio.Event()
            capture = tmp_path / "integration.pcap"
            stream = asyncio.create_task(
                client.stream_capture_to(project.project_id, link.link_id, capture, ready=ready)
            )
            await ready.wait()
            await asyncio.sleep(0.2)
            await client.stop_capture(project.project_id, link.link_id)
            await stream
            assert capture.stat().st_size >= 24  # classic PCAP global header

            pcapng = tmp_path / "integration.pcapng"
            PcapNgWriter.write(
                pcapng,
                sources=(PcapNgSource(path=capture, name="switch-link"),),
                metadata={},
                timestamp_policy="preserve",
            )
            assert pcapng.read_bytes().startswith(b"\x0a\x0d\x0d\x0a")

        finally:
            await client.delete_project(project.project_id)


@pytest.mark.asyncio
async def test_real_project_notifications_websocket() -> None:
    """Receive an actual controller notification after creating a project node."""
    compute_id = os.environ.get("LABFOUNDRY_GNS3_COMPUTE", "local")
    async with _client() as client:
        project = await client.create_project(f"lf-websocket-it-{uuid.uuid4().hex[:12]}")
        notifications = client.notifications(project.project_id)
        try:
            next_notification = asyncio.create_task(anext(notifications))
            await asyncio.sleep(0.2)
            await client.create_ethernet_switch(
                project.project_id,
                name="lf-websocket-switch",
                compute_id=compute_id,
                port_count=1,
                x=0,
                y=0,
            )
            payload = await asyncio.wait_for(next_notification, timeout=10.0)

            assert payload
        finally:
            await notifications.aclose()
            await client.delete_project(project.project_id)


@pytest.mark.asyncio
async def test_real_virtualbox_template_node_lifecycle() -> None:
    """Optionally create/start/stop one real VirtualBox node from a named GNS3 template."""
    template_name = os.environ.get("LABFOUNDRY_TEST_VBOX_TEMPLATE")
    if not template_name:
        pytest.skip("set LABFOUNDRY_TEST_VBOX_TEMPLATE to exercise a real VirtualBox template")

    compute_id = os.environ.get("LABFOUNDRY_GNS3_COMPUTE", "local")
    async with _client() as client:
        templates = [item for item in await client.list_templates() if item.name == template_name]
        assert len(templates) == 1, f"expected one template named {template_name!r}"
        template = templates[0]
        assert template.template_type == "virtualbox"

        project = await client.create_project(f"lf-vbox-it-{uuid.uuid4().hex[:12]}")
        try:
            node = await client.create_node_from_template(
                project.project_id,
                template.template_id,
                name="lf-vbox-integration",
                x=0,
                y=0,
                compute_id=compute_id if template.compute_id is None else None,
            )
            assert node.node_type == "virtualbox"
            assert node.ports
            switch = await client.create_ethernet_switch(
                project.project_id,
                name="lf-vbox-switch",
                compute_id=compute_id,
                port_count=1,
                x=-120,
                y=0,
            )
            link = await client.create_link(
                project.project_id,
                a_node_id=node.node_id,
                a_adapter=0,
                a_port=0,
                b_node_id=switch.node_id,
                b_adapter=0,
                b_port=0,
            )
            await client.start_node(project.project_id, node.node_id)
            await asyncio.sleep(1.0)

            spec = ScenarioSpec.model_validate(
                {
                    "version": "0.2",
                    "scenario": {"id": "filter-limits", "seed": 1},
                    "clock": {"start": "2026-01-01T00:00:00+00:00"},
                    "network": {
                        "segments": {"lab": {"subnet": "10.0.0.0/24"}},
                        "nodes": {
                            "node": {
                                "template": "unused",
                                "interfaces": {"eth0": {"segment": "lab", "ip": "auto"}},
                            }
                        },
                    },
                }
            )
            plan = ScenarioCompiler().compile(spec)
            deployment = DeploymentState(
                project_id=project.project_id,
                project_name=project.name,
                gns3_version="2.2.61",
                link_ids={plan.links[0].name: link.link_id},
            )
            delay = next(
                item
                for item in await client.available_link_filters(project.project_id, link.link_id)
                if item.type == "delay"
            )
            values = [
                (parameter.maximum + 1)
                if index == 0 and parameter.maximum is not None
                else (parameter.minimum or 0)
                for index, parameter in enumerate(delay.parameters)
            ]
            event = CompiledEvent(
                sequence=0,
                id="invalid-delay",
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                action="gns3.link.update",
                parameters={"link": plan.links[0].name, "filters": {"delay": values}},
            )
            dispatcher = InfrastructureActionDispatcher(client, plan, deployment)
            with pytest.raises(BackendError, match="exceeds maximum"):
                await dispatcher.handle_event(event)

            await client.stop_node(project.project_id, node.node_id)
        finally:
            await client.delete_project(project.project_id)
