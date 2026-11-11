"""Render scenario actions as a chronological SVG in a column or node lanes."""

from __future__ import annotations

import json
import re
import textwrap
from datetime import date
from enum import StrEnum
from html import escape
from typing import Any

import yaml

from labfoundry.compiler.plan import ScenarioPlan


class FlowMode(StrEnum):
    """Select narrative labels or complete compiled action details."""

    NARRATIVE = "narrative"
    COMPLETE = "complete"


class FlowLayout(StrEnum):
    """Arrange actions in a compact column or lanes for participating nodes."""

    COMPACT = "compact"
    LANES = "lanes"


def narrative_labels(source: str) -> dict[str, str]:
    """Read Narrative/Narrativa comments immediately preceding YAML events."""
    document = yaml.compose(source)
    if not isinstance(document, yaml.MappingNode):
        return {}
    events = next((value for key, value in document.value if key.value == "events"), None)
    if not isinstance(events, yaml.SequenceNode):
        return {}
    lines = source.splitlines()
    labels = {}
    for index, event in enumerate(events.value):
        if not isinstance(event, yaml.MappingNode):
            continue
        identifier = next(
            (value.value for key, value in event.value if key.value == "id"),
            f"event-{index + 1:04d}",
        )
        start = event.start_mark.line - 1
        comments = []
        while start >= 0:
            line = lines[start].strip()
            if line and not line.startswith("#"):
                break
            if line.startswith("#"):
                comments.append(line[1:].strip())
            start -= 1
        narrative = []
        active = False
        for comment in reversed(comments):
            match = re.match(r"(?:Narrative|Narrativa):\s*(.*)", comment, re.IGNORECASE)
            if match:
                active = True
                narrative = [match[1]]
            elif re.match(r"[^:]+:\s", comment):
                active = False
            elif active:
                narrative.append(comment)
        if narrative:
            labels[identifier] = " ".join(narrative)
    return labels


def render_flow_svg(
    plan: ScenarioPlan,
    *,
    mode: FlowMode = FlowMode.NARRATIVE,
    layout: FlowLayout = FlowLayout.COMPACT,
    labels: dict[str, str] | None = None,
    include_background: bool = False,
    include_provision: bool = False,
    day: date | None = None,
) -> str:
    """Render scheduled actions with optional preparation and background activity.

    The day filter applies to scheduled events, not preparation or recurring definitions.
    Background and preparation rows are opt-in even when the mode is complete.
    """
    labels = labels or {}
    events = sorted(plan.events, key=lambda event: (event.timestamp, event.sequence))
    if not include_background:
        events = [event for event in events if not event.id.startswith("background-")]
    if day is not None:
        events = [event for event in events if event.timestamp.date() == day]
    rows: list[tuple[str, str, str | None, str | None, dict[str, Any], str | None]] = []
    if include_provision:
        for action in plan.provision:
            rows.append(
                (
                    "Preparation",
                    f"provision-{action.sequence}",
                    action.node,
                    action.to,
                    action.model_dump(mode="json"),
                    None,
                )
            )
    for event in events:
        rows.append(
            (
                event.timestamp.date().isoformat(),
                event.id,
                event.from_,
                event.to,
                event.model_dump(mode="json", by_alias=True),
                event.timestamp.isoformat(),
            )
        )
    if include_background:
        for traffic in plan.traffic:
            rows.append(
                (
                    "Recurring traffic (intervals, not scheduled timestamps)",
                    traffic.assignment,
                    traffic.node,
                    traffic.to,
                    traffic.model_dump(mode="json"),
                    None,
                )
            )

    participants = list(dict.fromkeys(source or "Scenario" for _, _, source, _, _, _ in rows))
    participants.extend(
        target for _, _, _, target, _, _ in rows if target and target not in participants
    )
    participants = list(dict.fromkeys(participants)) or ["Scenario"]
    compact = layout == FlowLayout.COMPACT
    lane_width, margin = 330, 148
    width = 980 if compact else margin + lane_width * len(participants) + 24
    card_width = width - margin - 24 if compact else 306
    centers = {
        name: (margin + card_width / 2 if compact else margin + lane_width * (index + 0.5))
        for index, name in enumerate(participants)
    }
    parts = []

    def text(x: float, y: float, value: str, css: str = "body") -> None:
        parts.append(f'<text x="{x}" y="{y}" class="{css}">{escape(value)}</text>')

    text(24, 36, plan.scenario_id, "heading")
    text(24, 60, f"{mode.value} · seed {plan.root_seed} · {len(rows)} actions", "muted")
    text(
        24,
        80,
        "Top to bottom: scheduled order, not proof of causality. Times retain YAML offsets.",
        "muted",
    )
    if not compact:
        for node in participants:
            x = centers[node] - lane_width / 2
            parts.append(f'<rect x="{x}" y="100" width="{lane_width}" height="32" class="header"/>')
            text(x + 14, 122, node, "label")
    y = 100 if compact else 144
    section = None
    previous_bottom = None
    for number, (phase, identifier, source, target, details, timestamp) in enumerate(rows, 1):
        if phase != section:
            parts.append(f'<rect x="20" y="{y}" width="{width - 40}" height="32" class="phase"/>')
            text(32, y + 22, phase, "label")
            y += 40
            section = phase
            previous_bottom = None
        source = source or "Scenario"
        center = centers[source]
        x = center - card_width / 2
        if mode == FlowMode.COMPLETE:
            content = json.dumps(details, ensure_ascii=False, indent=2)
        else:
            content = labels.get(identifier, identifier.replace("_", " "))
        lines = [
            wrapped
            for line in content.splitlines()
            for wrapped in (
                textwrap.wrap(
                    line,
                    width=int((card_width - 28) / 7.5),
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                or [""]
            )
        ]
        heading = textwrap.wrap(identifier, width=int((card_width - 28) / 8)) or [identifier]
        body_y = 28 + 16 * len(heading)
        height = body_y + 10 + 16 * len(lines)
        parts.append(
            f'<g class="event" data-id="{escape(identifier, quote=True)}" '
            f'data-y="{y}" data-height="{height + 42}">'
        )
        tooltip = json.dumps(details, ensure_ascii=False) if mode == FlowMode.COMPLETE else content
        parts.append(f"<title>{escape(tooltip)}</title>")
        if previous_bottom is not None:
            parts.append(
                f'<path d="M 130 {previous_bottom} V {y + 20}" class="order" '
                'marker-end="url(#arrow)"/>'
            )
        text(28, y + 24, f"{number:03d}", "label")
        if timestamp:
            text(28, y + 44, timestamp[11:], "muted")
        parts.append(
            f'<rect x="{x}" y="{y}" width="{card_width}" height="{height}" rx="8" class="card"/>'
        )
        for index, line in enumerate(heading):
            text(x + 14, y + 24 + index * 16, line, "label")
        for index, line in enumerate(lines):
            text(
                x + 14,
                y + body_y + index * 16,
                line,
                "code" if mode == FlowMode.COMPLETE else "body",
            )
        arrow_y = y + height + 10
        if compact:
            destination = target or "destination not declared"
            text(x + 14, arrow_y + 12, f"{source} → {destination}", "muted")
        elif target and target != source:
            end = centers[target]
            parts.append(
                f'<path d="M {center} {arrow_y} H {end}" class="transfer" '
                'marker-end="url(#arrow)"/>'
            )
            text(min(center, end) + 8, arrow_y + 18, f"{source} → {target}", "muted")
        else:
            text(
                x + 14,
                arrow_y + 16,
                "No destination declared" if target is None else "Same-node action",
                "muted",
            )
        parts.append("</g>")
        previous_bottom = y + height
        y += height + 42
    if not rows:
        text(24, y + 24, "No actions match the selected filters.")
        y += 60
    if mode == FlowMode.COMPLETE:
        metadata = plan.model_dump(
            mode="json",
            by_alias=True,
            exclude={"events", "provision", "traffic"},
        )
        text(24, y + 24, "Execution settings, topology and capture definitions", "label")
        y += 50
        for line in json.dumps(metadata, ensure_ascii=False, indent=2).splitlines():
            for wrapped in textwrap.wrap(line, width=max(40, (width - 60) // 8)) or [""]:
                text(24, y, wrapped, "code")
                y += 18
    height = y + 30
    lanes = []
    for index in range(0 if compact else len(participants)):
        lanes.append(
            f'<rect x="{margin + lane_width * index}" y="142" width="{lane_width}" '
            f'height="{height - 142}" fill="{"#f1f5f9" if index % 2 == 0 else "#ffffff"}"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Scenario flow">'
        "<style>text{font-family:Arial,sans-serif;fill:#1e293b}.heading{font-size:24px;font-weight:bold}"
        ".label{font-size:13px;font-weight:bold}.body{font-size:13px}.muted{font-size:11px;fill:#64748b}"
        ".code{font-family:monospace;font-size:12px;white-space:pre}.header{fill:#e2e8f0}"
        ".phase{fill:#dbeafe}.card{fill:white;stroke:#94a3b8}.transfer{fill:none;stroke:#2563eb;stroke-width:2}"
        ".order{fill:none;stroke:#94a3b8;stroke-width:1.5}"
        '</style><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" '
        'orient="auto-start-reverse"><path d="M 0 0 L 8 4 L 0 8 Z" fill="#2563eb"/></marker></defs>'
        f'<rect width="{width}" height="{height}" fill="white"/>'
        + "".join(lanes + parts)
        + "</svg>"
    )
