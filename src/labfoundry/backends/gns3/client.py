"""Async client for the documented GNS3 2.2 Controller API."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import urlparse, urlunparse

import httpx
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ValidationError
from websockets.asyncio.client import connect as websocket_connect
from websockets.exceptions import WebSocketException

from labfoundry.backends.gns3.models import (
    GNS3AvailableFilter,
    GNS3Compute,
    GNS3Link,
    GNS3Node,
    GNS3Project,
    GNS3Snapshot,
    GNS3Template,
    GNS3Version,
    VirtualBoxTemplateCreate,
)
from labfoundry.errors import (
    GNS3APIError,
    GNS3CompatibilityError,
    GNS3ProtocolError,
    GNS3TransportError,
)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


class GNS3Client:
    """Typed Controller API client with a local-compute route for live PCAP streaming."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3080",
        *,
        username: str | None = None,
        password: str | None = None,
        verify_tls: bool = True,
        timeout: float = 30.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("GNS3 base_url must use http or https")
        if not parsed.hostname:
            raise ValueError("GNS3 base_url must include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("GNS3 base_url must not contain embedded credentials")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError(
                "GNS3 base_url must point to the controller origin, without a path/query"
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError(f"invalid GNS3 base_url port: {exc}") from exc
        self.base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.timeout = timeout
        if password is not None and username is None:
            raise ValueError("password requires username")
        self._http = self._new_http_client()

    def _new_http_client(self) -> httpx.AsyncClient:
        auth: httpx.BasicAuth | None = None
        if self.username is not None:
            auth = httpx.BasicAuth(self.username, self.password or "")
        return httpx.AsyncClient(
            base_url=self.base_url,
            auth=auth,
            verify=self.verify_tls,
            timeout=httpx.Timeout(self.timeout),
            headers={"Accept": "application/json"},
        )

    def _capture_stream_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.timeout,
            read=None,
            write=self.timeout,
            pool=self.timeout,
        )

    async def __aenter__(self) -> GNS3Client:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying asynchronous HTTP client."""
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        expected: set[int] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise GNS3TransportError(str(exc), method=method, path=path) from exc
        ok = expected if expected is not None else set(range(200, 300))
        if response.status_code not in ok:
            message = response.text
            try:
                payload = response.json()
                if isinstance(payload, dict) and "message" in payload:
                    message = str(payload["message"])
            except ValueError:
                pass
            raise GNS3APIError(
                response.status_code,
                message,
                method=method,
                path=path,
            )
        return response

    @staticmethod
    def _parse_model(response: httpx.Response, model: type[_ModelT]) -> _ModelT:
        try:
            return model.model_validate(response.json())
        except (ValueError, TypeError, ValidationError) as exc:
            request = response.request
            raise GNS3ProtocolError(
                f"invalid {model.__name__} response: {exc}",
                method=request.method,
                path=request.url.path,
            ) from exc

    @staticmethod
    def _parse_model_list(response: httpx.Response, model: type[_ModelT]) -> list[_ModelT]:
        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError(f"expected JSON array, got {type(payload).__name__}")
            return [model.model_validate(item) for item in payload]
        except (ValueError, TypeError, ValidationError) as exc:
            request = response.request
            raise GNS3ProtocolError(
                f"invalid list[{model.__name__}] response: {exc}",
                method=request.method,
                path=request.url.path,
            ) from exc

    async def version(self) -> GNS3Version:
        """Return the version information reported by the GNS3 controller."""
        response = await self._request("GET", "/v2/version", expected={200})
        return self._parse_model(response, GNS3Version)

    async def assert_supported_2_2(self) -> GNS3Version:
        """Validate that the controller version is within the supported GNS3 2.2 contract."""
        info = await self.version()
        try:
            version = Version(info.version)
        except InvalidVersion as exc:
            raise GNS3CompatibilityError(
                f"GNS3 returned an unparseable version {info.version!r}"
            ) from exc
        minimum = Version("2.2.61")
        maximum = Version("2.3.0")
        if version < minimum or version >= maximum:
            raise GNS3CompatibilityError(
                f"LabFoundry supports GNS3 Server >=2.2.61,<2.3.0; server reports {info.version}"
            )
        return info

    async def list_computes(self) -> list[GNS3Compute]:
        """List computes registered with the controller."""
        response = await self._request("GET", "/v2/computes", expected={200})
        return self._parse_model_list(response, GNS3Compute)

    async def get_compute(self, compute_id: str) -> GNS3Compute:
        """Return one compute by identifier."""
        response = await self._request("GET", f"/v2/computes/{compute_id}", expected={200})
        return self._parse_model(response, GNS3Compute)

    async def list_templates(self) -> list[GNS3Template]:
        """List templates visible to the controller."""
        response = await self._request("GET", "/v2/templates", expected={200})
        return self._parse_model_list(response, GNS3Template)

    async def create_virtualbox_template(self, template: VirtualBoxTemplateCreate) -> GNS3Template:
        """Create a VirtualBox template through the Controller API."""
        response = await self._request(
            "POST",
            "/v2/templates",
            json_body=template.model_dump(mode="json", exclude_none=True),
            expected={201},
        )
        return self._parse_model(response, GNS3Template)

    async def create_project(
        self,
        name: str,
        *,
        auto_close: bool = False,
        auto_open: bool = False,
        auto_start: bool = False,
    ) -> GNS3Project:
        """Create and open a GNS3 project."""
        response = await self._request(
            "POST",
            "/v2/projects",
            json_body={
                "name": name,
                "auto_close": auto_close,
                "auto_open": auto_open,
                "auto_start": auto_start,
            },
            expected={201},
        )
        return self._parse_model(response, GNS3Project)

    async def delete_project(self, project_id: str) -> None:
        """Delete a GNS3 project and its controller-managed resources."""
        await self._request("DELETE", f"/v2/projects/{project_id}", expected={204})

    async def create_snapshot(self, project_id: str, name: str) -> GNS3Snapshot:
        """Create a project snapshot."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/snapshots",
            json_body={"name": name},
            expected={201},
        )
        return self._parse_model(response, GNS3Snapshot)

    async def list_snapshots(self, project_id: str) -> list[GNS3Snapshot]:
        """List snapshots belonging to a project."""
        response = await self._request(
            "GET", f"/v2/projects/{project_id}/snapshots", expected={200}
        )
        return self._parse_model_list(response, GNS3Snapshot)

    async def restore_snapshot(self, project_id: str, snapshot_id: str) -> GNS3Project:
        """Restore a project snapshot."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/snapshots/{snapshot_id}/restore",
            json_body={},
            expected={201},
        )
        return self._parse_model(response, GNS3Project)

    async def delete_snapshot(self, project_id: str, snapshot_id: str) -> None:
        """Delete a project snapshot."""
        await self._request(
            "DELETE",
            f"/v2/projects/{project_id}/snapshots/{snapshot_id}",
            expected={204},
        )

    async def create_node_from_template(
        self,
        project_id: str,
        template_id: str,
        *,
        name: str,
        x: int,
        y: int,
        compute_id: str | None = None,
    ) -> GNS3Node:
        """Create a project node from an existing GNS3 template."""
        body: dict[str, Any] = {"name": name, "x": x, "y": y}
        if compute_id is not None:
            body["compute_id"] = compute_id
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/templates/{template_id}",
            json_body=body,
            expected={201},
        )
        return self._parse_model(response, GNS3Node)

    async def create_ethernet_switch(
        self,
        project_id: str,
        *,
        name: str,
        compute_id: str,
        port_count: int,
        x: int,
        y: int,
    ) -> GNS3Node:
        """Create a built-in GNS3 Ethernet switch node."""
        ports_mapping = [
            {
                "name": f"Ethernet{port}",
                "port_number": port,
                "type": "access",
                "vlan": 1,
            }
            for port in range(port_count)
        ]
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/nodes",
            json_body={
                "compute_id": compute_id,
                "name": name,
                "node_type": "ethernet_switch",
                "x": x,
                "y": y,
                "properties": {"ports_mapping": ports_mapping},
            },
            expected={201},
        )
        return self._parse_model(response, GNS3Node)

    async def create_udp_tunnel_node(
        self,
        project_id: str,
        *,
        name: str,
        compute_id: str,
        ports: list[dict[str, Any]],
        x: int,
        y: int,
    ) -> GNS3Node:
        """Create a cloud containing only explicit UDP Ethernet endpoints."""
        if not ports or any(
            set(port) != {"name", "port_number", "type", "lport", "rhost", "rport"}
            or port["type"] != "udp"
            for port in ports
        ):
            raise ValueError("managed tunnel nodes require explicit UDP ports")
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/nodes",
            json_body={
                "compute_id": compute_id,
                "name": name,
                "node_type": "cloud",
                "x": x,
                "y": y,
                "properties": {"ports_mapping": ports},
            },
            expected={201},
        )
        return self._parse_model(response, GNS3Node)

    async def create_nat_node(
        self,
        project_id: str,
        *,
        name: str,
        compute_id: str,
        x: int,
        y: int,
    ) -> GNS3Node:
        """Create the built-in GNS3 NAT node used for explicit real-world egress."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/nodes",
            json_body={
                "compute_id": compute_id,
                "name": name,
                "node_type": "nat",
                "x": x,
                "y": y,
            },
            expected={201},
        )
        return self._parse_model(response, GNS3Node)

    async def list_nodes(self, project_id: str) -> list[GNS3Node]:
        """List all nodes in a project."""
        response = await self._request("GET", f"/v2/projects/{project_id}/nodes", expected={200})
        return self._parse_model_list(response, GNS3Node)

    async def start_node(self, project_id: str, node_id: str) -> GNS3Node:
        """Start one project node."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/nodes/{node_id}/start",
            json_body={},
            expected={200},
        )
        return self._parse_model(response, GNS3Node)

    async def stop_node(self, project_id: str, node_id: str) -> GNS3Node:
        """Stop one project node."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/nodes/{node_id}/stop",
            json_body={},
            expected={200},
        )
        return self._parse_model(response, GNS3Node)

    async def start_all_nodes(self, project_id: str) -> None:
        """Start all nodes in a project."""
        await self._request(
            "POST", f"/v2/projects/{project_id}/nodes/start", json_body={}, expected={204}
        )

    async def stop_all_nodes(self, project_id: str) -> None:
        """Stop all nodes in a project."""
        await self._request(
            "POST", f"/v2/projects/{project_id}/nodes/stop", json_body={}, expected={204}
        )

    async def create_link(
        self,
        project_id: str,
        *,
        a_node_id: str,
        a_adapter: int,
        a_port: int,
        b_node_id: str,
        b_adapter: int,
        b_port: int,
    ) -> GNS3Link:
        """Create a link between two concrete GNS3 node ports."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/links",
            json_body={
                "nodes": [
                    {
                        "node_id": a_node_id,
                        "adapter_number": a_adapter,
                        "port_number": a_port,
                    },
                    {
                        "node_id": b_node_id,
                        "adapter_number": b_adapter,
                        "port_number": b_port,
                    },
                ]
            },
            expected={201},
        )
        return self._parse_model(response, GNS3Link)

    async def get_link(self, project_id: str, link_id: str) -> GNS3Link:
        """Return one project link."""
        response = await self._request(
            "GET", f"/v2/projects/{project_id}/links/{link_id}", expected={200}
        )
        return self._parse_model(response, GNS3Link)

    async def update_link(
        self,
        project_id: str,
        link_id: str,
        *,
        suspend: bool | None = None,
        filters: dict[str, Any] | None = None,
    ) -> GNS3Link:
        """Update mutable properties of a project link."""
        body: dict[str, Any] = {}
        if suspend is not None:
            body["suspend"] = suspend
        if filters is not None:
            body["filters"] = filters
        response = await self._request(
            "PUT",
            f"/v2/projects/{project_id}/links/{link_id}",
            json_body=body,
            expected={200, 201},
        )
        return self._parse_model(response, GNS3Link)

    async def available_link_filters(
        self, project_id: str, link_id: str
    ) -> list[GNS3AvailableFilter]:
        """Return typed link filters advertised by the GNS3 controller."""
        response = await self._request(
            "GET",
            f"/v2/projects/{project_id}/links/{link_id}/available_filters",
            expected={200},
        )
        return self._parse_model_list(response, GNS3AvailableFilter)

    async def start_capture(
        self,
        project_id: str,
        link_id: str,
        *,
        data_link_type: str = "DLT_EN10MB",
        capture_file_name: str | None = None,
    ) -> GNS3Link:
        """Start packet capture on a project link."""
        body: dict[str, Any] = {"data_link_type": data_link_type}
        if capture_file_name is not None:
            body["capture_file_name"] = capture_file_name
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/links/{link_id}/start_capture",
            json_body=body,
            expected={201},
        )
        return self._parse_model(response, GNS3Link)

    async def stop_capture(self, project_id: str, link_id: str) -> GNS3Link:
        """Stop packet capture on a project link."""
        response = await self._request(
            "POST",
            f"/v2/projects/{project_id}/links/{link_id}/stop_capture",
            json_body={},
            expected={201},
        )
        return self._parse_model(response, GNS3Link)

    async def download_pcap(self, project_id: str, link_id: str) -> bytes:
        """Download the raw PCAP bytes for a captured link."""
        response = await self._request(
            "GET", f"/v2/projects/{project_id}/links/{link_id}/pcap", expected={200}
        )
        return response.content

    async def download_pcap_to(self, project_id: str, link_id: str, destination: Path) -> None:
        """Stream a raw PCAP to disk atomically without buffering the full capture in RAM."""
        path = f"/v2/projects/{project_id}/links/{link_id}/pcap"
        temporary = destination.with_name(f".{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._http.stream(
                "GET", path, timeout=self._capture_stream_timeout()
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    message = body.decode("utf-8", errors="replace")
                    raise GNS3APIError(response.status_code, message, method="GET", path=path)
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except httpx.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            raise GNS3TransportError(str(exc), method="GET", path=path) from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    async def finalize_capture_to(self, project_id: str, link_id: str, destination: Path) -> None:
        """Open the live PCAP stream, stop capture, and drain it atomically to disk."""
        link = await self.get_link(project_id, link_id)
        path = f"/v2/projects/{project_id}/links/{link_id}/pcap"
        if link.capture_compute_id == "local":
            nodes = {node.node_id: node for node in await self.list_nodes(project_id)}
            always_running = {
                "cloud",
                "nat",
                "ethernet_switch",
                "ethernet_hub",
                "frame_relay_switch",
                "atm_switch",
            }
            candidates = [
                endpoint
                for endpoint in link.nodes
                if endpoint.node_id in nodes and nodes[endpoint.node_id].status == "started"
            ]
            capture_endpoint = next(
                (
                    endpoint
                    for endpoint in candidates
                    if nodes[endpoint.node_id].compute_id == "local"
                    and nodes[endpoint.node_id].node_type in always_running
                ),
                candidates[0] if candidates else None,
            )
            if capture_endpoint is None:
                raise GNS3ProtocolError(
                    "active capture has no running endpoint", method="GET", path=path
                )
            capture_node = nodes[capture_endpoint.node_id]
            path = (
                f"/v2/compute/projects/{project_id}/{capture_node.node_type}/nodes/"
                f"{capture_node.node_id}/adapters/{capture_endpoint.adapter_number}/ports/"
                f"{capture_endpoint.port_number}/pcap"
            )
        temporary = destination.with_name(f".{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with self._http.stream(
                "GET", path, timeout=self._capture_stream_timeout()
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    message = body.decode("utf-8", errors="replace")
                    raise GNS3APIError(response.status_code, message, method="GET", path=path)
                stop_path = f"/v2/projects/{project_id}/links/{link_id}/stop_capture"
                async with self._new_http_client() as control_http:
                    stop_response = await control_http.post(stop_path, json={})
                if stop_response.status_code != 201:
                    message = stop_response.text
                    try:
                        payload = stop_response.json()
                        if isinstance(payload, dict) and "message" in payload:
                            message = str(payload["message"])
                    except ValueError:
                        pass
                    raise GNS3APIError(
                        stop_response.status_code,
                        message,
                        method="POST",
                        path=stop_path,
                    )
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except httpx.HTTPError as exc:
            if _has_classic_pcap_record(temporary):
                os.replace(temporary, destination)
                return
            temporary.unlink(missing_ok=True)
            raise GNS3TransportError(str(exc), method="GET", path=path) from exc
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    async def stream_capture_to(
        self,
        project_id: str,
        link_id: str,
        destination: Path,
        *,
        ready: asyncio.Event | None = None,
    ) -> None:
        """Drain an active capture to disk until GNS3 closes the stream."""
        path = f"/v2/projects/{project_id}/links/{link_id}/pcap"
        temporary = destination.with_name(f".{destination.name}.part")
        try:
            path = await self._capture_stream_path(project_id, link_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Quiet Dynamips links may withhold HTTP headers until the first packet.
            if ready is not None:
                ready.set()
            async with self._http.stream(
                "GET", path, timeout=self._capture_stream_timeout()
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    message = body.decode("utf-8", errors="replace")
                    raise GNS3APIError(response.status_code, message, method="GET", path=path)
                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except httpx.HTTPError as exc:
            if _has_classic_pcap_header(temporary):
                os.replace(temporary, destination)
                return
            temporary.unlink(missing_ok=True)
            raise GNS3TransportError(str(exc), method="GET", path=path) from exc
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if ready is not None:
                ready.set()

    async def _capture_stream_path(self, project_id: str, link_id: str) -> str:
        link = await self.get_link(project_id, link_id)
        path = f"/v2/projects/{project_id}/links/{link_id}/pcap"
        if link.capture_compute_id != "local":
            return path
        nodes = {node.node_id: node for node in await self.list_nodes(project_id)}
        always_running = {
            "cloud",
            "nat",
            "ethernet_switch",
            "ethernet_hub",
            "frame_relay_switch",
            "atm_switch",
        }
        candidates = [
            endpoint
            for endpoint in link.nodes
            if endpoint.node_id in nodes and nodes[endpoint.node_id].status == "started"
        ]
        capture_endpoint = next(
            (
                endpoint
                for endpoint in candidates
                if nodes[endpoint.node_id].compute_id == "local"
                and nodes[endpoint.node_id].node_type in always_running
            ),
            candidates[0] if candidates else None,
        )
        if capture_endpoint is None:
            raise GNS3ProtocolError(
                "active capture has no running endpoint", method="GET", path=path
            )
        capture_node = nodes[capture_endpoint.node_id]
        return (
            f"/v2/compute/projects/{project_id}/{capture_node.node_type}/nodes/"
            f"{capture_node.node_id}/adapters/{capture_endpoint.adapter_number}/ports/"
            f"{capture_endpoint.port_number}/pcap"
        )

    def notification_ws_url(self, project_id: str) -> str:
        """Build the authenticated WebSocket URL for project notifications."""
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse(
            (scheme, parsed.netloc, f"/v2/projects/{project_id}/notifications/ws", "", "", "")
        )

    async def notifications(self, project_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded project notification messages from the controller WebSocket."""
        headers: dict[str, str] = {}
        if self.username is not None:
            token = base64.b64encode(f"{self.username}:{self.password or ''}".encode()).decode(
                "ascii"
            )
            headers["Authorization"] = f"Basic {token}"
        ws_url = self.notification_ws_url(project_id)
        ssl_context: ssl.SSLContext | None = None
        if ws_url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            if not self.verify_tls:
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE
        try:
            async with websocket_connect(
                ws_url,
                additional_headers=headers or None,
                open_timeout=self.timeout,
                ssl=ssl_context,
            ) as websocket:
                async for message in websocket:
                    payload = json.loads(message)
                    if isinstance(payload, dict):
                        yield payload
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise GNS3TransportError(
                str(exc), method="GET", path=f"/v2/projects/{project_id}/notifications/ws"
            ) from exc


def _has_classic_pcap_header(path: Path) -> bool:
    """Return whether an interrupted GNS3 capture contains a complete classic-PCAP header."""
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
        return (
            magic
            in {
                b"\xd4\xc3\xb2\xa1",
                b"\xa1\xb2\xc3\xd4",
                b"M<\xb2\xa1",
                b"\xa1\xb2<M",
            }
            and path.stat().st_size >= 24
        )
    except OSError:
        return False


def _has_classic_pcap_record(path: Path) -> bool:
    """Return whether an interrupted stream has at least one complete PCAP record."""
    try:
        with path.open("rb") as handle:
            header = handle.read(40)
        magic = header[:4]
        if (
            magic
            not in {
                b"\xd4\xc3\xb2\xa1",
                b"\xa1\xb2\xc3\xd4",
                b"M<\xb2\xa1",
                b"\xa1\xb2<M",
            }
            or len(header) != 40
        ):
            return False
        endian: Literal["little", "big"] = (
            "little" if magic in {b"\xd4\xc3\xb2\xa1", b"M<\xb2\xa1"} else "big"
        )
        captured_length = int.from_bytes(header[32:36], endian)
        return path.stat().st_size >= 40 + captured_length
    except OSError:
        return False
