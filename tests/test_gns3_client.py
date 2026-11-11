import json

import httpx
import pytest

from labfoundry.backends.gns3.client import GNS3Client
from labfoundry.backends.gns3.models import VirtualBoxTemplateCreate


@pytest.mark.asyncio
async def test_virtualbox_template_uses_documented_controller_route() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "template_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": "RF Ubuntu",
                "template_type": "virtualbox",
                "compute_id": "local",
            },
        )

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        await client.create_virtualbox_template(
            VirtualBoxTemplateCreate(
                name="RF Ubuntu",
                vmname="Ubuntu-Base",
                linked_clone=True,
                adapters=2,
            )
        )
    finally:
        await client.aclose()

    assert seen["method"] == "POST"
    assert seen["path"] == "/v2/templates"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["template_type"] == "virtualbox"
    assert body["vmname"] == "Ubuntu-Base"
    assert body["linked_clone"] is True
    assert body["adapter_type"] == "Intel PRO/1000 MT Desktop (82540EM)"


@pytest.mark.asyncio
async def test_capture_routes_and_status_codes_match_gns3_22_docs() -> None:
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        base_link = {
            "link_id": "link-id",
            "project_id": "project-id",
            "nodes": [],
            "filters": {},
            "suspend": False,
        }
        if request.url.path.endswith("/start_capture"):
            return httpx.Response(201, json={**base_link, "capturing": True})
        if request.url.path.endswith("/stop_capture"):
            return httpx.Response(201, json={**base_link, "capturing": False})
        if request.url.path.endswith("/pcap"):
            return httpx.Response(200, content=b"pcap-bytes")
        raise AssertionError(request.url)

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        await client.start_capture("project-id", "link-id")
        await client.stop_capture("project-id", "link-id")
        data = await client.download_pcap("project-id", "link-id")
    finally:
        await client.aclose()

    assert data == b"pcap-bytes"
    assert calls == [
        ("POST", "/v2/projects/project-id/links/link-id/start_capture"),
        ("POST", "/v2/projects/project-id/links/link-id/stop_capture"),
        ("GET", "/v2/projects/project-id/links/link-id/pcap"),
    ]


@pytest.mark.asyncio
async def test_update_link_accepts_status_returned_by_gns3_2261() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "link_id": "link-id",
                "project_id": "project-id",
                "nodes": [],
                "filters": {"delay": [150, 0]},
                "suspend": False,
            },
        )

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        link = await client.update_link("project-id", "link-id", filters={"delay": [150, 0]})
    finally:
        await client.aclose()

    assert link.filters == {"delay": [150, 0]}


@pytest.mark.asyncio
async def test_transport_errors_are_wrapped() -> None:
    from labfoundry.errors import GNS3TransportError

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("controller offline", request=request)

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(GNS3TransportError, match="controller offline"):
            await client.version()
    finally:
        await client.aclose()


def test_password_requires_username() -> None:
    with pytest.raises(ValueError, match="password requires username"):
        GNS3Client("http://gns3.test:3080", password="secret")


@pytest.mark.asyncio
async def test_virtualbox_custom_adapter_omits_null_optional_fields() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "template_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                "name": "RF Ubuntu",
                "template_type": "virtualbox",
                "compute_id": "local",
            },
        )

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        await client.create_virtualbox_template(
            VirtualBoxTemplateCreate(
                name="RF Ubuntu",
                vmname="Ubuntu-Base",
                adapters=2,
                custom_adapters=[{"adapter_number": 1}],
            )
        )
    finally:
        await client.aclose()

    body = seen["body"]
    assert isinstance(body, dict)
    assert body["custom_adapters"] == [{"adapter_number": 1}]


@pytest.mark.asyncio
async def test_invalid_success_response_is_protocol_error() -> None:
    from labfoundry.errors import GNS3ProtocolError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(GNS3ProtocolError, match="invalid GNS3Version response"):
            await client.version()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unsupported_gns3_version_is_compatibility_error() -> None:
    from labfoundry.errors import GNS3CompatibilityError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": "3.1.0a5"})

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(GNS3CompatibilityError, match=r">=2\.2\.61,<2\.3\.0"):
            await client.assert_supported_2_2()
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "ftp://gns3.test:3080",
        "http://user:secret@gns3.test:3080",
        "http://gns3.test:3080/v2",
        "http://gns3.test:3080/?debug=1",
        "http://gns3.test:3080/#fragment",
        "http://gns3.test:99999",
    ],
)
def test_controller_origin_rejects_unsafe_or_ambiguous_urls(url: str) -> None:
    with pytest.raises(ValueError):
        GNS3Client(url)


def test_controller_origin_normalizes_trailing_slash() -> None:
    client = GNS3Client("https://gns3.test:3080/")
    try:
        assert client.base_url == "https://gns3.test:3080"
    finally:
        import asyncio

        asyncio.run(client.aclose())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported", "supported"),
    [("2.2.60", False), ("2.2.61", True), ("2.2.99", True), ("2.3.0", False)],
)
async def test_supported_gns3_version_window(reported: str, supported: bool) -> None:
    from labfoundry.errors import GNS3CompatibilityError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": reported})

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        if supported:
            info = await client.assert_supported_2_2()
            assert info.version == reported
        else:
            with pytest.raises(GNS3CompatibilityError):
                await client.assert_supported_2_2()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_available_filters_are_typed_from_controller_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/available_filters")
        return httpx.Response(
            200,
            json=[
                {
                    "type": "delay",
                    "name": "Delay",
                    "description": "Delay packets",
                    "parameters": [
                        {
                            "name": "latency",
                            "type": "int",
                            "minimum": 0,
                            "maximum": 32767,
                            "unit": "ms",
                        }
                    ],
                }
            ],
        )

    client = GNS3Client("http://gns3.test:3080")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url="http://gns3.test:3080", transport=httpx.MockTransport(handler)
    )
    try:
        filters = await client.available_link_filters("project", "link")
    finally:
        await client.aclose()

    assert filters[0].type == "delay"
    assert filters[0].parameters[0].maximum == 32767


@pytest.mark.asyncio
async def test_project_notifications_decode_objects_and_authenticate(monkeypatch) -> None:
    seen: dict[str, object] = {}

    class FakeWebSocket:
        def __init__(self) -> None:
            self._messages = iter(['{"event": "node.created"}', '["ignored"]'])

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            return None

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    def connect(url: str, **kwargs):
        seen["url"] = url
        seen.update(kwargs)
        return FakeWebSocket()

    monkeypatch.setattr("labfoundry.backends.gns3.client.websocket_connect", connect)
    client = GNS3Client(
        "https://gns3.test:3080",
        username="operator",
        password="secret",
    )
    try:
        notifications = [item async for item in client.notifications("project-id")]
    finally:
        await client.aclose()

    assert notifications == [{"event": "node.created"}]
    assert seen["url"] == "wss://gns3.test:3080/v2/projects/project-id/notifications/ws"
    assert seen["additional_headers"] == {"Authorization": "Basic b3BlcmF0b3I6c2VjcmV0"}
