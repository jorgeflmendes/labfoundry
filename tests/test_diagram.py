from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path
from xml.etree import ElementTree

import pytest

from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.compiler.diagram import NetworkView, render_network_svg
from labfoundry.compiler.plan import CompiledEgress
from labfoundry.schema.loader import load_scenario

SVG = "{http://www.w3.org/2000/svg}"


def test_diagram_marks_only_selected_capture_links(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))

    image = ElementTree.fromstring(render_network_svg(plan))
    links = image.findall(f"{SVG}path")
    captured = [link for link in links if link.attrib.get("class") == "captured"]
    ordinary = [link for link in links if link.attrib.get("class") == "link"]

    assert len(captured) == sum(len(capture.endpoints) for capture in plan.captures)
    assert len(ordinary) == len(plan.links) - len(captured)
    assert any("users-perimeter" in (link.findtext(f"{SVG}title") or "") for link in captured)
    assert any("wan-perimeter" in (link.findtext(f"{SVG}title") or "") for link in captured)
    assert any(
        "gateway:servers--servers" in (link.findtext(f"{SVG}title") or "") for link in ordinary
    )

    groups = {
        item.attrib["data-name"]: item
        for item in image.findall(f"{SVG}g")
        if "data-name" in item.attrib
    }
    assert set(groups) == {
        *(node.name for node in plan.nodes),
        *(segment.switch_name for segment in plan.segments),
    }
    assert groups["ws-01"].attrib["data-kind"] == "workstation"
    assert groups["gateway"].attrib["data-kind"] == "router"
    assert groups["__lf_seg_users"].attrib["data-kind"] == "switch"
    assert all(group.find(f"{SVG}image") is not None for group in groups.values())
    assert all(
        group.find(f"{SVG}image").attrib["href"].startswith("data:image/svg+xml;base64,")
        for group in groups.values()
    )
    assert image.findall(f"{SVG}image[@class='capture-marker']")
    assert all(
        link.attrib["d"].count(" L ") >= 3
        for link in captured + ordinary
        if link.find(f"{SVG}title") is not None
    )


def test_diagram_includes_nat_and_escapes_scenario_label(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    plan = plan.model_copy(
        update={
            "scenario_id": "<network&case>",
            "egress": CompiledEgress(
                name="outside", mode="nat", segment="simulated_internet", switch_port=1
            ),
        }
    )

    svg = render_network_svg(plan)
    image = ElementTree.fromstring(svg)
    labels = " ".join(element.text or "" for element in image.iter())

    assert "&lt;network&amp;case&gt;" in svg
    assert "<network&case>" in labels
    assert "outside" in labels
    assert "NAT" in labels
    assert svg == render_network_svg(plan)


def test_diagram_links_join_card_edges_without_crossing_other_cards(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    image = ElementTree.fromstring(render_network_svg(plan))
    cards = {
        group.attrib["data-name"]: group.find(f"{SVG}rect[@class='device']")
        for group in image.findall(f"{SVG}g")
        if "data-name" in group.attrib
    }

    def bounds(name: str) -> tuple[float, float, float, float]:
        card = cards[name]
        assert card is not None
        x, y, width, height = (float(card.attrib[key]) for key in ("x", "y", "width", "height"))
        return x, y, x + width, y + height

    for link in plan.links:
        path = next(
            item
            for item in image.findall(f"{SVG}path")
            if item.attrib.get("data-name") == link.name
        )
        values = [float(value) for value in re.findall(r"-?\d+\.\d+", path.attrib["d"])]
        points = list(zip(values[::2], values[1::2], strict=True))
        for name, (x, y) in ((link.a.node, points[0]), (link.b.node, points[-1])):
            left, top, right, bottom = bounds(name)
            assert left <= x <= right and top <= y <= bottom
            assert x in (left, right) or y in (top, bottom)
        for (x1, y1), (x2, y2) in pairwise(points):
            assert x1 == x2 or y1 == y2
            for name in cards:
                left, top, right, bottom = bounds(name)
                if x1 == x2:
                    assert not (left < x1 < right and min(y1, y2) < bottom and max(y1, y2) > top)
                else:
                    assert not (top < y1 < bottom and min(x1, x2) < right and max(x1, x2) > left)


def test_diagram_ignores_overlapping_gns3_coordinates_and_separates_labels(
    scenario_path: Path,
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    plan = plan.model_copy(
        update={"nodes": tuple(node.model_copy(update={"x": 0, "y": 0}) for node in plan.nodes)}
    )
    image = ElementTree.fromstring(render_network_svg(plan))
    cards = image.findall(f".//{SVG}rect[@class='device']")
    for index, first in enumerate(cards):
        x, y, w, h = (float(first.attrib[key]) for key in ("x", "y", "width", "height"))
        for second in cards[index + 1 :]:
            sx, sy, sw, sh = (float(second.attrib[key]) for key in ("x", "y", "width", "height"))
            assert x + w <= sx or sx + sw <= x or y + h <= sy or sy + sh <= y
    for group in image.findall(f"{SVG}g"):
        icon = group.find(f"{SVG}image")
        assert icon is not None
        top = float(icon.attrib["y"])
        bottom = top + float(icon.attrib["height"])
        for label in group.findall(f"{SVG}text"):
            baseline = float(label.attrib["y"])
            assert baseline < top or baseline - 12 > bottom


def test_diagram_uses_declared_gns3_node_type(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    for template_type in ("cloud", "vpcs"):
        node = plan.nodes[0].model_copy(update={"expected_template_type": template_type})
        altered = plan.model_copy(update={"nodes": (node, *plan.nodes[1:])})
        image = ElementTree.fromstring(render_network_svg(altered))
        group = next(
            item for item in image.findall(f"{SVG}g") if item.attrib.get("data-name") == node.name
        )
        assert group.attrib["data-kind"] == template_type


def test_captured_view_removes_uncaptured_interfaces_and_nat_from_all_svg_content(
    scenario_path: Path,
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    original = plan.model_dump()
    hidden_segment = next(segment for segment in plan.segments if segment.name == "servers")
    plan = plan.model_copy(
        update={
            "egress": CompiledEgress(
                name="private-nat",
                mode="nat",
                segment="servers",
                switch_port=10,
            )
        }
    )
    svg = render_network_svg(plan, view=NetworkView.CAPTURED)
    assert hidden_segment.subnet not in svg
    assert hidden_segment.switch_name not in svg
    assert "private-nat" not in svg
    assert "10.20.20.1/24" not in svg
    assert "linux-vbox" not in svg
    assert "seed " not in svg
    assert "10.10.10.1/24" in svg and "198.51.100.1/24" in svg
    image = ElementTree.fromstring(svg)
    assert len(image.findall(f"{SVG}path")) == sum(len(c.endpoints) for c in plan.captures)
    assert plan.model_dump(exclude={"egress"}) == {
        key: value for key, value in original.items() if key != "egress"
    }


def test_named_capture_and_node_filters_do_not_leave_hidden_addresses(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    svg = render_network_svg(plan, captures=("users-perimeter",))
    assert "198.51.100.1/24" not in svg
    assert "10.10.10.1/24" in svg
    image = ElementTree.fromstring(svg)
    assert len(image.findall(f"{SVG}path")) == 1
    svg = render_network_svg(plan, nodes=("gateway",), exclude_segments=("servers",))
    assert "10.20.20.1/24" not in svg
    assert "10.10.10.1/24" in svg
    svg = render_network_svg(plan, view=NetworkView.CAPTURED, exclude_nodes=("gateway",))
    assert "No devices match" in svg
    assert not ElementTree.fromstring(svg).findall(f"{SVG}g")


def test_network_filters_reject_unknown_names(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    for options in (
        {"captures": ("missing",)},
        {"segments": ("missing",)},
        {"exclude_segments": ("missing",)},
        {"nodes": ("missing",)},
        {"exclude_nodes": ("missing",)},
    ):
        with pytest.raises(ValueError, match="unknown"):
            render_network_svg(plan, **options)
