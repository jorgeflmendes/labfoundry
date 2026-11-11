from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

from typer.testing import CliRunner

from labfoundry.cli.main import app
from labfoundry.compiler.compiler import ScenarioCompiler
from labfoundry.compiler.flow import FlowLayout, FlowMode, narrative_labels, render_flow_svg
from labfoundry.schema.loader import load_scenario

SVG = "{http://www.w3.org/2000/svg}"


def test_narrative_comments_follow_yaml_event_boundaries() -> None:
    source = """events:
  # Execução: Not narrative.
  # Narrativa: Tiago reads <mail> & follows
  #   the link.
  - id: first
    parameters:
      script: |
        # Narrativa: This is script content.
        echo test

  # Narrative: A different actor replies.
  - id: second
  - action: wait
"""
    assert narrative_labels(source) == {
        "first": "Tiago reads <mail> & follows the link.",
        "second": "A different actor replies.",
    }


def test_flow_orders_events_and_filters_background_without_inventing_targets(
    scenario_path: Path,
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    first = plan.events[0].model_copy(update={"id": "first", "to": None})
    second = first.model_copy(
        update={"id": "second", "timestamp": first.timestamp + timedelta(seconds=5)}
    )
    background = first.model_copy(update={"id": "background-0001"})
    plan = plan.model_copy(update={"events": (second, background, first)})
    image = ElementTree.fromstring(render_flow_svg(plan, labels={"first": "Read <mail> & reply"}))
    events = image.findall(f"{SVG}g[@class='event']")
    assert [event.attrib["data-id"] for event in events] == ["first", "second"]
    assert "Read <mail> & reply" in " ".join(image.itertext())
    assert not image.findall(f".//{SVG}path[@class='transfer']")
    assert float(events[1].attrib["data-y"]) >= (
        float(events[0].attrib["data-y"]) + float(events[0].attrib["data-height"])
    )


def test_complete_flow_keeps_long_scripts_conditions_and_preparation(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    script = "echo " + "x" * 400 + "\nprintf '<>&'\n"
    event = plan.events[0].model_copy(update={"parameters": {"script": script, "timeout": "30s"}})
    plan = plan.model_copy(update={"events": (event,)})
    image = ElementTree.fromstring(
        render_flow_svg(
            plan,
            mode=FlowMode.COMPLETE,
            include_background=True,
            include_provision=True,
        )
    )
    group = image.find(f"{SVG}g[@data-id='{event.id}']")
    assert group is not None
    details = json.loads(group.findtext(f"{SVG}title") or "")
    assert details["parameters"]["script"] == script
    assert details["parameters"]["timeout"] == "30s"
    assert len(image.findall(f"{SVG}g[@class='event']")) == (
        len(plan.provision) + len(plan.events) + len(plan.traffic)
    )
    assert render_flow_svg(plan, mode=FlowMode.COMPLETE) == render_flow_svg(
        plan, mode=FlowMode.COMPLETE
    )


def test_day_filter_can_produce_an_empty_diagram(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    image = ElementTree.fromstring(render_flow_svg(plan, day=date(1990, 1, 1)))
    assert not image.findall(f"{SVG}g[@class='event']")
    assert "No actions match" in " ".join(image.itertext())


def test_compact_layout_reduces_area_without_dropping_actions_or_parameters(
    scenario_path: Path,
) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    compact = ElementTree.fromstring(render_flow_svg(plan, mode=FlowMode.COMPLETE))
    lanes = ElementTree.fromstring(
        render_flow_svg(plan, mode=FlowMode.COMPLETE, layout=FlowLayout.LANES)
    )
    assert float(compact.attrib["width"]) * float(compact.attrib["height"]) < (
        float(lanes.attrib["width"]) * float(lanes.attrib["height"])
    )
    for image in (compact, lanes):
        details = [
            json.loads(group.findtext(f"{SVG}title") or "")
            for group in image.findall(f"{SVG}g[@class='event']")
        ]
        assert details == [
            event.model_dump(mode="json", by_alias=True)
            for event in sorted(plan.events, key=lambda event: (event.timestamp, event.sequence))
            if not event.id.startswith("background-")
        ]


def test_transfer_arrow_and_condition_are_taken_from_compiled_events(scenario_path: Path) -> None:
    plan = ScenarioCompiler().compile(load_scenario(scenario_path))
    from labfoundry.compiler.plan import CompiledEventCondition

    event = plan.events[0].model_copy(
        update={
            "to": plan.nodes[-1].name,
            "when": CompiledEventCondition(event="prior", exit_code=0, stdout_matches="<ok>"),
        }
    )
    plan = plan.model_copy(update={"events": (event,)})
    image = ElementTree.fromstring(
        render_flow_svg(plan, mode=FlowMode.COMPLETE, layout=FlowLayout.LANES)
    )
    assert len(image.findall(f".//{SVG}path[@class='transfer']")) == 1
    group = image.find(f"{SVG}g[@data-id='{event.id}']")
    assert group is not None
    details = json.loads(group.findtext(f"{SVG}title") or "")
    assert details["when"]["stdout_matches"] == "<ok>"
    assert details["to"] == event.to


def test_flow_cli_modes_and_invalid_filters(tmp_path: Path, scenario_path: Path) -> None:
    runner = CliRunner()
    for mode in ("narrative", "complete"):
        target = tmp_path / f"{mode}.svg"
        result = runner.invoke(app, ["flow", str(scenario_path), "--mode", mode, "-o", str(target)])
        assert result.exit_code == 0, result.output
        image = ElementTree.fromstring(target.read_text(encoding="utf-8"))
        assert image.findall(f"{SVG}g[@class='event']")
    assert runner.invoke(app, ["flow", str(scenario_path), "--mode", "invented"]).exit_code == 2
    assert runner.invoke(app, ["flow", str(scenario_path), "--day", "not-a-date"]).exit_code == 2
    target = tmp_path / "filtered.svg"
    result = runner.invoke(
        app,
        [
            "flow",
            str(scenario_path),
            "--mode",
            "complete",
            "--no-provision",
            "--no-background",
            "--day",
            "1990-01-01",
            "-o",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.output
    image = ElementTree.fromstring(target.read_text(encoding="utf-8"))
    assert not image.findall(f"{SVG}g[@class='event']")
