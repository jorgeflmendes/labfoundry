from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from labfoundry.backends.gns3.client import (
    GNS3Client,
    _has_classic_pcap_header,
    _has_classic_pcap_record,
)


@pytest.mark.asyncio
async def test_capture_start_does_not_wait_for_traffic(tmp_path, monkeypatch) -> None:
    traffic = asyncio.Event()
    ready = asyncio.Event()
    payload = b"\xd4\xc3\xb2\xa1" + b"\x00" * 20

    async def handler(request: httpx.Request) -> httpx.Response:
        await traffic.wait()
        return httpx.Response(200, content=payload)

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(client, "_capture_stream_path", AsyncMock(return_value="/capture"))
    destination = tmp_path / "capture.pcap"
    task = asyncio.create_task(
        client.stream_capture_to("project", "link", destination, ready=ready)
    )
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert not task.done()
        traffic.set()
        await task
        assert destination.read_bytes() == payload
    finally:
        traffic.set()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_capture_source_failure_releases_startup_waiter(tmp_path, monkeypatch) -> None:
    client = GNS3Client("http://gns3.test:3080")
    monkeypatch.setattr(
        client, "_capture_stream_path", AsyncMock(side_effect=RuntimeError("source unavailable"))
    )
    ready = asyncio.Event()
    try:
        with pytest.raises(RuntimeError, match="source unavailable"):
            await client.stream_capture_to(
                "project", "link", tmp_path / "capture.pcap", ready=ready
            )
        assert ready.is_set()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_create_nat_node_uses_controller_node_route() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "node_id": "nat-id",
                "name": "labfoundry-nat",
                "node_type": "nat",
                "compute_id": "local",
                "ports": [{"adapter_number": 0, "port_number": 0}],
            },
        )

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        node = await client.create_nat_node(
            "project", name="labfoundry-nat", compute_id="local", x=10, y=20
        )
    finally:
        await client.aclose()

    assert node.node_type == "nat"
    assert seen["method"] == "POST"
    assert seen["path"] == "/v2/projects/project/nodes"
    assert seen["body"]["node_type"] == "nat"


@pytest.mark.asyncio
async def test_download_pcap_to_streams_atomically(tmp_path) -> None:
    payload = b"0123456789" * 1000

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    destination = tmp_path / "capture.pcap"
    try:
        await client.download_pcap_to("project", "link", destination)
    finally:
        await client.aclose()

    assert destination.read_bytes() == payload
    assert not (tmp_path / ".capture.pcap.part").exists()


def test_interrupted_stream_requires_a_complete_classic_pcap_header(tmp_path) -> None:
    partial = tmp_path / "partial.pcap"
    partial.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 19)
    assert not _has_classic_pcap_header(partial)

    partial.write_bytes(b"\xd4\xc3\xb2\xa1" + b"\x00" * 20)
    assert _has_classic_pcap_header(partial)
    assert not _has_classic_pcap_record(partial)

    partial.write_bytes(
        b"\xd4\xc3\xb2\xa1"
        + b"\x00" * 20
        + b"\x00" * 8
        + b"\x04\x00\x00\x00"
        + b"\x00" * 4
        + b"data"
    )
    assert _has_classic_pcap_record(partial)


@pytest.mark.asyncio
async def test_stream_capture_keeps_a_valid_partial_pcap_when_gns3_closes_early(
    tmp_path, monkeypatch
) -> None:
    payload = b"\xd4\xc3\xb2\xa1" + b"\x00" * 20

    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
            raise httpx.ReadError("stream closed")

        async def aclose(self) -> None:
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/capture"
        assert request.extensions["timeout"]["read"] is None
        return httpx.Response(200, stream=InterruptedStream())

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(client, "_capture_stream_path", AsyncMock(return_value="/capture"))
    destination = tmp_path / "capture.pcap"
    try:
        await client.stream_capture_to("project", "link", destination)
    finally:
        await client.aclose()

    assert destination.read_bytes() == payload
    assert not (tmp_path / ".capture.pcap.part").exists()


@pytest.mark.asyncio
async def test_finalize_capture_opens_stream_before_stopping(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/links/link"):
            return httpx.Response(
                200,
                json={
                    "link_id": "link",
                    "project_id": "project",
                    "nodes": [{"node_id": "switch", "adapter_number": 0, "port_number": 0}],
                    "capture_compute_id": "local",
                    "capturing": True,
                },
            )
        if request.url.path.endswith("/nodes"):
            return httpx.Response(
                200,
                json=[
                    {
                        "node_id": "switch",
                        "name": "switch",
                        "node_type": "ethernet_switch",
                        "compute_id": "local",
                        "status": "started",
                    }
                ],
            )
        if request.method == "GET":
            return httpx.Response(200, content=b"pcap-bytes")
        return httpx.Response(
            201,
            json={
                "link_id": "link",
                "project_id": "project",
                "nodes": [],
                "filters": {},
                "suspend": False,
                "capturing": False,
            },
        )

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    monkeypatch.setattr(
        client,
        "_new_http_client",
        lambda: httpx.AsyncClient(
            base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
        ),
    )
    destination = tmp_path / "capture.pcap"
    try:
        await client.finalize_capture_to("project", "link", destination)
    finally:
        await client.aclose()

    assert destination.read_bytes() == b"pcap-bytes"
    assert calls == [
        ("GET", "/v2/projects/project/links/link"),
        ("GET", "/v2/projects/project/nodes"),
        (
            "GET",
            "/v2/compute/projects/project/ethernet_switch/nodes/switch/adapters/0/ports/0/pcap",
        ),
        ("POST", "/v2/projects/project/links/link/stop_capture"),
    ]
