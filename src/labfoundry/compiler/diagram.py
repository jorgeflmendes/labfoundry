"""Render a compiled network topology as an SVG."""

from __future__ import annotations

import base64
import textwrap
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from html import escape
from importlib.resources import files
from xml.etree import ElementTree

from labfoundry.compiler.plan import CompiledNode, ScenarioPlan

_SYMBOLS = {
    "switch": "ethernet_switch.svg",
    "router": "router.svg",
    "workstation": "computer.svg",
    "vpcs": "vpcs_guest.svg",
    "cloud": "cloud.svg",
    "server": "computer.svg",
    "container": "docker_guest.svg",
    "vm": "computer.svg",
    "nat": "nat.svg",
}


class NetworkView(StrEnum):
    """Select the full topology or only declared capture coverage."""

    FULL = "full"
    CAPTURED = "captured"


def _visible_plan(
    plan: ScenarioPlan,
    view: NetworkView,
    captures: tuple[str, ...],
    segments: tuple[str, ...],
    exclude_segments: tuple[str, ...],
    nodes: tuple[str, ...],
    exclude_nodes: tuple[str, ...],
) -> ScenarioPlan:
    known_segments = {segment.name for segment in plan.segments}
    known_nodes = {node.name for node in plan.nodes}
    if plan.egress is not None:
        known_nodes.add(plan.egress.name)
    for label, selected, known in (
        ("capture", set(captures), {capture.name for capture in plan.captures}),
        ("segment", set(segments) | set(exclude_segments), known_segments),
        ("node", set(nodes) | set(exclude_nodes), known_nodes),
    ):
        if unknown := selected - known:
            raise ValueError(f"unknown {label}: {', '.join(sorted(unknown))}")
    visible_segments = (set(segments) if segments else known_segments) - set(exclude_segments)
    visible_nodes = (set(nodes) if nodes else known_nodes) - set(exclude_nodes)
    if nodes:
        node_segments = {
            interface.segment
            for node in plan.nodes
            if node.name in visible_nodes
            for interface in node.interfaces
        }
        if plan.egress is not None and plan.egress.name in visible_nodes:
            node_segments.add(plan.egress.segment)
        visible_segments &= node_segments
    selected_captures = tuple(
        capture for capture in plan.captures if not captures or capture.name in captures
    )
    captured_view = view == NetworkView.CAPTURED or bool(captures)
    captured_links = {
        endpoint.link for capture in selected_captures for endpoint in capture.endpoints
    }
    switches = {segment.switch_name for segment in plan.segments}
    links = tuple(
        link
        for link in plan.links
        if link.segment in visible_segments
        and all(
            endpoint.node in visible_nodes or endpoint.node in switches
            for endpoint in (link.a, link.b)
        )
        and (not captured_view or link.name in captured_links)
    )
    if captured_view:
        visible_segments &= {link.segment for link in links}
        visible_nodes &= {endpoint.node for link in links for endpoint in (link.a, link.b)}
    endpoints = {
        (endpoint.node, endpoint.adapter, endpoint.port)
        for link in links
        for endpoint in (link.a, link.b)
    }
    link_names = {link.name for link in links}
    filtered_nodes = []
    for node in plan.nodes:
        if node.name not in visible_nodes:
            continue
        interfaces = tuple(
            interface
            for interface in node.interfaces
            if interface.segment in visible_segments
            and (not captured_view or (node.name, interface.adapter, interface.port) in endpoints)
        )
        if node.interfaces and not interfaces:
            continue
        filtered_nodes.append(node.model_copy(update={"interfaces": interfaces}))
    egress = plan.egress
    if egress is not None and (
        captured_view or egress.segment not in visible_segments or egress.name not in visible_nodes
    ):
        egress = None
    return plan.model_copy(
        update={
            "nodes": tuple(filtered_nodes),
            "segments": tuple(
                segment for segment in plan.segments if segment.name in visible_segments
            ),
            "links": links,
            "egress": egress,
            "captures": tuple(
                capture.model_copy(
                    update={
                        "endpoints": tuple(
                            endpoint
                            for endpoint in capture.endpoints
                            if endpoint.link in link_names
                        )
                    }
                )
                for capture in selected_captures
            ),
        }
    )


@dataclass(frozen=True)
class _Device:
    name: str
    kind: str
    x: float
    y: float
    height: float
    title: str
    labels: tuple[str, ...]
    addresses: tuple[str, ...]
    width: float = 220

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


def _node_kind(node: CompiledNode) -> str:
    if node.expected_template_type in {"vpcs", "cloud"}:
        return node.expected_template_type
    if node.ipv4_forwarding:
        return "router"
    if node.expected_template_type == "docker":
        return "container"
    template = node.template.lower()
    if "server" in template or "service" in template:
        return "server"
    if "client" in template or "desktop" in template:
        return "workstation"
    return "vm"


@cache
def _asset(filename: str) -> tuple[float, float, str]:
    payload = files("labfoundry.compiler").joinpath("gns3_symbols", filename).read_bytes()
    root = ElementTree.fromstring(payload)
    uri = "data:image/svg+xml;base64," + base64.b64encode(payload).decode("ascii")
    return float(root.attrib["width"]), float(root.attrib["height"]), uri


def render_network_svg(
    plan: ScenarioPlan,
    *,
    view: NetworkView = NetworkView.FULL,
    captures: tuple[str, ...] = (),
    segments: tuple[str, ...] = (),
    exclude_segments: tuple[str, ...] = (),
    nodes: tuple[str, ...] = (),
    exclude_nodes: tuple[str, ...] = (),
) -> str:
    """Draw segment bands and orthogonal links independently of GNS3 coordinates."""
    plan = _visible_plan(plan, view, captures, segments, exclude_segments, nodes, exclude_nodes)
    captured_view = view == NetworkView.CAPTURED or bool(captures)
    memberships = {
        node.name: {interface.segment for interface in node.interfaces} for node in plan.nodes
    }
    backbone = [node for node in plan.nodes if len(memberships[node.name]) != 1]
    backbone_names = {node.name for node in backbone}
    backbone_links = [
        link
        for link in plan.links
        if link.a.node in backbone_names or link.b.node in backbone_names
    ]
    leaves = {
        segment.name: [node.name for node in plan.nodes if memberships[node.name] == {segment.name}]
        for segment in plan.segments
    }
    descriptions = {}
    for node in plan.nodes:
        addresses = tuple(f"{interface.name}: {interface.address}" for interface in node.interfaces)
        descriptions[node.name] = (
            _node_kind(node),
            node.name if captured_view else node.template,
            addresses,
        )
    for segment in plan.segments:
        descriptions[segment.switch_name] = (
            "switch",
            f"Ethernet switch · {segment.name}",
            (segment.subnet,),
        )
    if plan.egress is not None:
        descriptions[plan.egress.name] = ("nat", "NAT", (plan.egress.segment,))
        leaves[plan.egress.segment].append(plan.egress.name)

    label_lines = {name: tuple(textwrap.wrap(name, width=24)) for name in descriptions}
    address_lines = {
        name: tuple(line for address in details[2] for line in textwrap.wrap(address, width=28))
        for name, details in descriptions.items()
    }
    name_height = max((len(lines) for lines in label_lines.values()), default=1) * 16
    columns = max([1, len(backbone), *(len(names) + 1 for names in leaves.values())])
    left = 100 + len(backbone_links) * 28
    width = max(800, left + columns * 252 + 24)
    devices = {}
    bands = []

    def place(name: str, x: float, y: float) -> None:
        kind, title, _ = descriptions[name]
        devices[name] = _Device(
            name,
            kind,
            x,
            y,
            name_height + 105 + len(address_lines[name]) * 14,
            title,
            label_lines[name],
            address_lines[name],
        )

    for index, node in enumerate(backbone):
        place(node.name, left + index * 252, 100)
    top_bottom = max((device.y + device.height for device in devices.values()), default=100)
    y = top_bottom + 24 + len(backbone_links) * 12 if backbone else 100
    for segment in plan.segments:
        names = leaves[segment.name]
        fan_count = sum(
            link.segment == segment.name and (link.a.node in names or link.b.node in names)
            for link in plan.links
        )
        if plan.egress is not None and plan.egress.segment == segment.name:
            fan_count += 1
        switch_y = y + 54 + fan_count * 12
        place(segment.switch_name, left, switch_y)
        for index, name in enumerate(names):
            place(name, left + (index + 1) * 252, switch_y)
        bottom = (
            max(devices[name].y + devices[name].height for name in [segment.switch_name, *names])
            + 16
        )
        bands.append((segment, y, bottom - y))
        y = bottom + 20
    height = y + 12
    captured: dict[str, list[str]] = {}
    for capture in plan.captures:
        for endpoint in capture.endpoints:
            captured.setdefault(endpoint.link, []).append(f"{capture.name}: {endpoint.name}")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "<style>text{font-family:Arial,sans-serif;fill:#1b2733}.title{font-size:22px;font-weight:700}"
        ".hint{font-size:12px;fill:#53647a}.name{font-family:monospace;font-size:13px;font-weight:700;text-anchor:middle}"
        ".address{font-family:monospace;font-size:11px;text-anchor:middle;fill:#53647a}"
        ".link{fill:none;stroke:#64748b;stroke-width:2}.captured{fill:none;stroke:#1769aa;stroke-width:2.5}</style>",
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text class="title" x="42" y="45">{escape(plan.scenario_id)}</text>',
        '<text class="hint" x="42" y="68">'
        + ("Capture coverage" if captured_view else f"Network topology · seed {plan.root_seed}")
        + " · blue links: capture points</text>",
    ]
    if not descriptions:
        parts.append(
            '<text class="hint" x="42" y="110">No devices match the selected filters.</text>'
        )
    for segment, band_y, band_height in bands:
        parts.append(
            f'<rect class="segment" x="{left - 20}" y="{band_y}" '
            f'width="{(len(leaves[segment.name]) + 1) * 252 + 8}" '
            f'height="{band_height}" rx="12" fill="#f1f5f9"/>'
        )
        parts.append(
            f'<text class="hint" x="{left}" y="{band_y + 26}">{escape(segment.name)}'
            f" · {escape(segment.subnet)}</text>"
        )

    fan_index: dict[str, int] = {}
    backbone_ports: dict[str, int] = {}
    backbone_rail = 0
    switch_ports: dict[str, int] = {}

    def draw_link(
        name: str, first_name: str, second_name: str, segment_name: str, index: int
    ) -> None:
        nonlocal backbone_rail
        first, second = devices[first_name], devices[second_name]
        switch, node = (first, second) if first.kind == "switch" else (second, first)
        marker: tuple[float, float]
        rail_y: float
        if node.name in backbone_names:
            port = switch_ports.get(switch.name, 0)
            switch_ports[switch.name] = port + 1
            rail_x = 50 + backbone_rail * 28
            backbone_rail += 1
            node_port = backbone_ports.get(node.name, 0)
            backbone_ports[node.name] = node_port + 1
            connections = sum(
                link.a.node == node.name or link.b.node == node.name for link in backbone_links
            )
            start_x = node.x + (node_port + 1) * node.width / (connections + 1)
            rail_y = top_bottom + 8 + backbone_rail * 12
            end_y = switch.y + 30 + port * 12
            points = [
                (start_x, node.y + node.height),
                (start_x, rail_y),
                (rail_x, rail_y),
                (rail_x, end_y),
                (switch.x, end_y),
            ]
            marker = (rail_x, (rail_y + end_y) / 2)
            if first_name == switch.name:
                points.reverse()
        else:
            port = fan_index.get(segment_name, 0)
            fan_index[segment_name] = port + 1
            rail_y = switch.y - 20 - port * 12
            connections = sum(
                link.segment == segment_name
                and (link.a.node in leaves[segment_name] or link.b.node in leaves[segment_name])
                for link in plan.links
            )
            if plan.egress is not None and plan.egress.segment == segment_name:
                connections += 1
            start_x = switch.x + (port + 1) * switch.width / (connections + 1)
            points = [
                (start_x, switch.y),
                (start_x, rail_y),
                (node.center[0], rail_y),
                (node.center[0], node.y),
            ]
            marker = (node.center[0], node.y - 12)
            if first_name != switch.name:
                points.reverse()
        labels = captured.get(name)
        title = ", ".join(labels) if labels else name
        path = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in points)
        css = "captured" if labels else "link"
        parts.append(
            f'<path class="{css}" data-name="{escape(name, quote=True)}" d="{path}">'
            f"<title>{escape(title)}</title></path>"
        )
        if labels:
            _, _, uri = _asset("inspect.svg")
            parts.append(
                f'<image class="capture-marker" x="{marker[0] - 11:.3f}" '
                f'y="{marker[1] - 11:.3f}" width="22" height="22" href="{uri}"/>'
            )

    for index, link in enumerate(plan.links):
        draw_link(link.name, link.a.node, link.b.node, link.segment, index)
    if plan.egress is not None:
        segment = next(item for item in plan.segments if item.name == plan.egress.segment)
        draw_link(
            f"{plan.egress.name}--{segment.switch_name}",
            plan.egress.name,
            segment.switch_name,
            segment.name,
            len(plan.links),
        )
    for device in devices.values():
        symbol_width, symbol_height, uri = _asset(_SYMBOLS[device.kind])
        x = device.center[0] - symbol_width / 2
        icon_y = device.y + name_height + 24
        parts.extend(
            (
                f'<g data-kind="{device.kind}" data-name="{escape(device.name, quote=True)}">',
                f"<title>{escape(device.title)}</title>",
                f'<rect class="device" x="{device.x}" y="{device.y}" '
                f'width="{device.width}" height="{device.height}" '
                'rx="8" fill="white" stroke="#cbd5e1"/>',
                f'<image x="{x:.3f}" y="{icon_y}" width="{symbol_width}" '
                f'height="{symbol_height}" href="{uri}"/>',
            )
        )
        for index, label in enumerate(device.labels):
            parts.append(
                f'<text class="name" x="{device.center[0]}" '
                f'y="{device.y + 24 + index * 16}">{escape(label)}</text>'
            )
        for index, address in enumerate(device.addresses):
            parts.append(
                f'<text class="address" x="{device.center[0]}" '
                f'y="{icon_y + 76 + index * 14}">{escape(address)}</text>'
            )
        parts.append("</g>")
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
