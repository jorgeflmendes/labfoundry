"""Scenario DSL to frozen ScenarioPlan compiler."""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from ipaddress import IPv4Interface
from itertools import pairwise
from typing import Literal

from labfoundry.compiler.ipam import DeterministicIPAM
from labfoundry.compiler.plan import (
    CompiledCapture,
    CompiledCaptureEndpoint,
    CompiledCaptureMetadata,
    CompiledEgress,
    CompiledEvent,
    CompiledEventCondition,
    CompiledInterface,
    CompiledLink,
    CompiledNode,
    CompiledProvisionAction,
    CompiledRealismAudit,
    CompiledSegment,
    CompiledTrafficAction,
    LinkEndpoint,
    ScenarioPlan,
)
from labfoundry.compiler.seeds import derive_seed
from labfoundry.errors import CompilationError
from labfoundry.schema.scenario import (
    BackgroundTrafficSpec,
    CaptureEndpointSpec,
    EventSpec,
    IntervalSpec,
    ScenarioSpec,
)
from labfoundry.util.duration import parse_duration


@dataclass(frozen=True)
class _CompiledInterval:
    distribution: Literal[
        "fixed",
        "uniform",
        "exponential",
        "normal",
        "lognormal",
        "gamma",
        "weibull",
        "pareto",
    ]
    interval_seconds: float | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None
    mean_seconds: float | None = None
    stddev_seconds: float | None = None
    median_seconds: float | None = None
    sigma: float | None = None
    scale_seconds: float | None = None
    shape: float | None = None


class ScenarioCompiler:
    """Compile a validated scenario specification into a reproducible plan."""

    def compile(self, spec: ScenarioSpec, *, seed: int | None = None) -> ScenarioPlan:
        """Compile a validated scenario specification into a deterministic immutable plan."""
        root_seed = spec.scenario.seed if seed is None else seed
        if not 0 <= root_seed <= 2**63 - 1:
            raise CompilationError("seed must be between 0 and 2^63-1")

        expanded_groups = self._expand_node_names(spec)
        ipam = DeterministicIPAM(root_seed)
        nodes = self._compile_nodes(spec, expanded_groups, ipam)
        segments, links = self._compile_segments_and_links(spec, nodes)
        events, aliases = self._compile_events(spec, expanded_groups, root_seed)
        if spec.background_traffic is not None:
            if not spec.clock.warp_gaps:
                raise CompilationError("background_traffic requires clock.warp_gaps")
            events = self._add_background_events(
                events, spec.background_traffic, expanded_groups, root_seed
            )
        traffic = self._compile_traffic(spec, expanded_groups, root_seed)
        provision = self._compile_provision(spec, expanded_groups)
        captures = self._compile_captures(spec, nodes, links)
        duration_seconds = None
        if spec.clock.duration is not None:
            duration_seconds = parse_duration(spec.clock.duration).total_seconds()
            if duration_seconds <= 0:
                raise CompilationError("clock.duration must be greater than zero")
        if spec.clock.warp_gaps and duration_seconds is not None:
            raise CompilationError("clock.warp_gaps cannot be combined with clock.duration")
        if spec.clock.warp_gaps and spec.clock.speed != 1.0:
            raise CompilationError("clock.warp_gaps requires clock.speed: 1.0")
        if traffic and duration_seconds is None:
            raise CompilationError(
                "clock.duration is required when recurring traffic is configured"
            )
        if duration_seconds is not None and events:
            scenario_end = spec.clock.start + timedelta(seconds=duration_seconds)
            latest = max(event.timestamp for event in events)
            if latest > scenario_end:
                raise CompilationError(
                    f"event {latest.isoformat()} occurs after clock.duration ends at "
                    f"{scenario_end.isoformat()}"
                )
        egress = None
        if spec.network.egress is not None:
            switch_port = sum(1 for link in links if link.segment == spec.network.egress.segment)
            egress = CompiledEgress(
                mode=spec.network.egress.mode,
                name=spec.network.egress.name,
                segment=spec.network.egress.segment,
                switch_port=switch_port,
            )

        return ScenarioPlan(
            scenario_id=spec.scenario.id,
            root_seed=root_seed,
            scenario_start=spec.clock.start,
            clock_speed=spec.clock.speed,
            warp_gaps=spec.clock.warp_gaps,
            max_idle_gap_seconds=(
                parse_duration(spec.background_traffic.max_idle_gap).total_seconds()
                if spec.background_traffic is not None
                and spec.background_traffic.max_idle_gap is not None
                else None
            ),
            sync_guest_clocks=spec.clock.sync_guests,
            settle_delay_seconds=parse_duration(spec.clock.settle_delay).total_seconds(),
            duration_seconds=duration_seconds,
            allow_cloud_nodes=spec.network.allow_cloud_nodes,
            segments=tuple(segments),
            nodes=tuple(nodes),
            links=tuple(links),
            egress=egress,
            events=tuple(events),
            traffic=tuple(traffic),
            provision=tuple(provision),
            captures=tuple(captures),
            realism_audit=CompiledRealismAudit(
                enabled=spec.evidence.realism_audit.enabled,
                baseline=spec.evidence.realism_audit.baseline,
                fail_on=spec.evidence.realism_audit.fail_on,
            ),
            aliases=aliases,
        )

    @staticmethod
    def _expand_node_names(spec: ScenarioSpec) -> dict[str, tuple[str, ...]]:
        result: dict[str, tuple[str, ...]] = {}
        owners: dict[str, str] = {}
        for group, node in spec.network.nodes.items():
            expanded: tuple[str, ...]
            if node.count == 1:
                expanded = (group,)
            else:
                width = max(2, len(str(node.count)))
                expanded = tuple(f"{group}-{i:0{width}d}" for i in range(1, node.count + 1))
            for name in expanded:
                previous_owner = owners.get(name)
                if previous_owner is not None:
                    raise CompilationError(
                        f"node name {name!r} is generated by both {previous_owner!r} and {group!r}"
                    )
                owners[name] = group
            result[group] = expanded
        return result

    def _compile_nodes(
        self,
        spec: ScenarioSpec,
        groups: dict[str, tuple[str, ...]],
        ipam: DeterministicIPAM,
    ) -> list[CompiledNode]:
        # Static reservations are done first so automatic allocations never steal them.
        for group_name, group_spec in spec.network.nodes.items():
            for instance_name in groups[group_name]:
                for iface_name, iface in group_spec.interfaces.items():
                    if isinstance(iface.ip, IPv4Interface):
                        network = spec.network.segments[iface.segment].subnet
                        ipam.reserve(network, iface.ip, owner=f"{instance_name}.{iface_name}")

        nodes: list[CompiledNode] = []
        for group_index, (group_name, group_spec) in enumerate(spec.network.nodes.items()):
            for instance_index, instance_name in enumerate(groups[group_name]):
                interfaces: list[CompiledInterface] = []
                for iface_name, iface in group_spec.interfaces.items():
                    network = spec.network.segments[iface.segment].subnet
                    if iface.ip == "auto":
                        address = str(
                            ipam.allocate(
                                network,
                                owner=f"{instance_name}.{iface_name}",
                                namespace=f"{instance_name}:{iface_name}",
                            )
                        )
                    elif iface.ip == "dhcp":
                        address = "dhcp"
                    else:
                        address = str(iface.ip)
                    interfaces.append(
                        CompiledInterface(
                            name=iface_name,
                            segment=iface.segment,
                            address=address,
                            mac=iface.mac,
                            gateway=str(iface.gateway) if iface.gateway is not None else None,
                            dns=tuple(str(item) for item in iface.dns),
                            adapter=iface.adapter,
                            port=iface.port,
                        )
                    )
                nodes.append(
                    CompiledNode(
                        name=instance_name,
                        group=group_name,
                        template=group_spec.template,
                        expected_template_type=group_spec.template_type,
                        interfaces=tuple(interfaces),
                        guest_profile=group_spec.guest_profile,
                        ipv4_forwarding=group_spec.ipv4_forwarding,
                        x=300 + (group_index % 5) * 220,
                        y=200 + (group_index // 5) * 180 + instance_index * 90,
                    )
                )
        self._validate_unique_macs(nodes)
        return nodes

    @staticmethod
    def _validate_unique_macs(nodes: list[CompiledNode]) -> None:
        owners: dict[str, str] = {}
        for node in nodes:
            for interface in node.interfaces:
                if interface.mac is None:
                    continue
                normalized = interface.mac.replace("-", ":").lower()
                owner = f"{node.name}.{interface.name}"
                previous = owners.get(normalized)
                if previous is not None:
                    raise CompilationError(
                        f"MAC address {interface.mac!r} is used by both {previous!r} and {owner!r}"
                    )
                owners[normalized] = owner

    @staticmethod
    def _compile_segments_and_links(
        spec: ScenarioSpec, nodes: list[CompiledNode]
    ) -> tuple[list[CompiledSegment], list[CompiledLink]]:
        by_segment: dict[str, list[tuple[CompiledNode, CompiledInterface]]] = defaultdict(list)
        for node in nodes:
            for interface in node.interfaces:
                by_segment[interface.segment].append((node, interface))

        segments: list[CompiledSegment] = []
        links: list[CompiledLink] = []
        for index, (segment_name, segment_spec) in enumerate(spec.network.segments.items()):
            attached = sorted(
                by_segment.get(segment_name, []),
                key=lambda item: (item[0].name, item[1].name),
            )
            switch_name = f"__lf_seg_{segment_name}"
            extra_ports = (
                1
                if spec.network.egress is not None and spec.network.egress.segment == segment_name
                else 0
            )
            port_count = max(8, len(attached) + extra_ports)
            segments.append(
                CompiledSegment(
                    name=segment_name,
                    subnet=str(segment_spec.subnet),
                    switch_name=switch_name,
                    port_count=port_count,
                    x=40,
                    y=100 + index * 180,
                )
            )
            for switch_port, (node, interface) in enumerate(attached):
                links.append(
                    CompiledLink(
                        name=f"{node.name}:{interface.name}--{segment_name}",
                        segment=segment_name,
                        a=LinkEndpoint(
                            node=node.name,
                            adapter=interface.adapter,
                            port=interface.port,
                        ),
                        b=LinkEndpoint(node=switch_name, adapter=0, port=switch_port),
                    )
                )
        return segments, links

    def _compile_events(
        self,
        spec: ScenarioSpec,
        groups: dict[str, tuple[str, ...]],
        root_seed: int,
    ) -> tuple[list[CompiledEvent], dict[str, tuple[str, ...]]]:
        aliases: dict[str, tuple[str, ...]] = {key: value for key, value in groups.items()}
        compiled: list[CompiledEvent] = []
        event_times: dict[str, datetime] = {}
        compiled_by_id: dict[str, CompiledEvent] = {}
        previous_time: datetime | None = None

        for sequence, event in enumerate(spec.events):
            event_id = event.id or f"event-{sequence + 1:04d}"
            if event_id in event_times:
                raise CompilationError(f"duplicate event id {event_id!r}")

            if event.select is not None:
                population = aliases.get(event.select.from_)
                if population is None:
                    raise CompilationError(
                        f"event {event_id} selector references unknown group/alias "
                        f"{event.select.from_!r}"
                    )
                if event.select.count > len(population):
                    raise CompilationError(
                        f"event {event_id} selects {event.select.count} from only "
                        f"{len(population)} nodes"
                    )
                selector_seed = derive_seed(
                    root_seed,
                    f"selector:{event_id}:{event.select.as_}",
                )
                rng = random.Random(selector_seed)
                selected = tuple(sorted(rng.sample(list(population), event.select.count)))
                if event.select.as_ in aliases:
                    raise CompilationError(
                        f"event {event_id} selector alias {event.select.as_!r} collides with "
                        "an existing node group or alias"
                    )
                aliases[event.select.as_] = selected

            timestamp = self._event_timestamp(
                spec.clock.start,
                event,
                previous_time=previous_time,
                event_times=event_times,
            )
            if timestamp < spec.clock.start:
                raise CompilationError(
                    f"event {event_id} occurs before scenario start "
                    f"({spec.clock.start.isoformat()})"
                )
            source = self._resolve_single_ref(event.from_, aliases, event_id, "from")
            target = self._resolve_single_ref(event.to, aliases, event_id, "to")

            condition = None
            if event.when is not None:
                prior = compiled_by_id.get(event.when.event)
                if prior is None:
                    raise CompilationError(
                        f"event {event_id} condition references unknown prior event "
                        f"{event.when.event!r}"
                    )
                if prior.action != "guest.exec":
                    raise CompilationError(
                        f"event {event_id} condition requires prior guest.exec event "
                        f"{event.when.event!r}"
                    )
                if prior.timestamp >= timestamp:
                    raise CompilationError(
                        f"event {event_id} condition source {prior.id!r} must occur earlier"
                    )
                condition = CompiledEventCondition(
                    event=event.when.event,
                    exit_code=event.when.exit_code,
                    stdout_matches=event.when.stdout_matches,
                    stderr_matches=event.when.stderr_matches,
                )

            item = CompiledEvent(
                sequence=sequence,
                id=event_id,
                timestamp=timestamp,
                action=event.action,
                **{"from": source},
                to=target,
                parameters=dict(event.parameters),
                when=condition,
            )
            compiled.append(item)
            compiled_by_id[event_id] = item
            event_times[event_id] = timestamp
            previous_time = timestamp

        compiled.sort(key=lambda item: (item.timestamp, item.sequence))
        return compiled, aliases

    def _add_background_events(
        self,
        events: list[CompiledEvent],
        background: BackgroundTrafficSpec,
        groups: dict[str, tuple[str, ...]],
        root_seed: int,
    ) -> list[CompiledEvent]:
        if not events:
            raise CompilationError("background_traffic requires scheduled events")
        min_gap = parse_duration(background.min_gap).total_seconds()
        min_interval = parse_duration(background.min_interval).total_seconds()
        max_interval = parse_duration(background.max_interval).total_seconds()
        max_idle_gap = (
            parse_duration(background.max_idle_gap).total_seconds()
            if background.max_idle_gap is not None
            else None
        )
        if min_gap <= 0 or min_interval <= 0 or max_interval < min_interval:
            raise CompilationError("background_traffic requires positive, ordered intervals")
        if max_idle_gap is not None and max_idle_gap < max(min_gap, max_interval):
            raise CompilationError(
                "background_traffic max_idle_gap must cover min_gap and max_interval"
            )

        timestamps = sorted({event.timestamp for event in events})
        generated: list[CompiledEvent] = []
        for left, right in pairwise(timestamps):
            if (right - left).total_seconds() < min_gap:
                continue
            namespace = f"background:{left.isoformat()}:{right.isoformat()}"
            rng = random.Random(derive_seed(root_seed, namespace))
            cursor = left
            for _ in range(background.max_events_per_gap):
                candidate = cursor + timedelta(seconds=rng.uniform(min_interval, max_interval))
                if candidate >= right or (
                    max_idle_gap is None and candidate + timedelta(seconds=min_interval) >= right
                ):
                    break
                cursor = candidate
                if all(item.weight == 1 for item in background.activities):
                    activity = rng.choice(background.activities)
                else:
                    activity = rng.choices(
                        background.activities,
                        weights=[item.weight for item in background.activities],
                    )[0]
                source = self._resolve_traffic_target(activity.from_, groups)
                target = self._resolve_traffic_target(activity.to, groups)
                event_id = f"background-{len(generated) + 1:04d}"
                if any(event.id == event_id for event in events):
                    raise CompilationError(f"generated background event id {event_id!r} conflicts")
                parameters = dict(activity.parameters)
                env = parameters.get("env", {})
                if not isinstance(env, dict):
                    raise CompilationError("background activity env must be a mapping")
                if "LABFOUNDRY_BACKGROUND_SEED" in env:
                    raise CompilationError(
                        "background activity env reserves LABFOUNDRY_BACKGROUND_SEED"
                    )
                parameters["env"] = {
                    **env,
                    "LABFOUNDRY_BACKGROUND_SEED": str(
                        derive_seed(root_seed, f"background-action:{event_id}")
                    ),
                }
                generated.append(
                    CompiledEvent(
                        sequence=len(events) + len(generated),
                        id=event_id,
                        timestamp=cursor,
                        action="guest.exec",
                        **{"from": source},
                        to=target,
                        parameters=parameters,
                    )
                )
            if max_idle_gap is not None and (right - cursor).total_seconds() > max_idle_gap:
                raise CompilationError(
                    f"background_traffic cannot cover idle gap from {left.isoformat()} "
                    f"to {right.isoformat()} with "
                    f"max_events_per_gap={background.max_events_per_gap}"
                )
        return sorted([*events, *generated], key=lambda item: (item.timestamp, item.sequence))

    @staticmethod
    def _resolve_single_ref(
        reference: str | None,
        aliases: dict[str, tuple[str, ...]],
        event_id: str,
        field: str,
    ) -> str | None:
        if reference is None:
            return None
        values = aliases.get(reference)
        if values is None:
            raise CompilationError(
                f"event {event_id} field {field!r} references unknown node/alias {reference!r}"
            )
        if len(values) != 1:
            raise CompilationError(
                f"event {event_id} field {field!r} refers to {reference!r}, which resolves to "
                f"{len(values)} nodes; use a selector alias resolving to one node"
            )
        return values[0]

    def _event_timestamp(
        self,
        scenario_start: datetime,
        event: EventSpec,
        *,
        previous_time: datetime | None,
        event_times: dict[str, datetime],
    ) -> datetime:
        if event.at is not None:
            return self._parse_at(scenario_start, event.at)

        assert event.after is not None
        if isinstance(event.after, str):
            if previous_time is None:
                raise CompilationError(
                    "first event cannot use relative 'after' without an event reference"
                )
            return previous_time + parse_duration(event.after)

        base = previous_time
        if event.after.event is not None:
            try:
                base = event_times[event.after.event]
            except KeyError as exc:
                raise CompilationError(
                    f"relative event references unknown prior event {event.after.event!r}"
                ) from exc
        if base is None:
            raise CompilationError("relative event has no base timestamp")
        return base + parse_duration(event.after.delay)

    @staticmethod
    def _parse_at(scenario_start: datetime, value: str) -> datetime:
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=scenario_start.tzinfo)
            return parsed

        try:
            parts = [int(part) for part in raw.split(":")]
            if len(parts) == 2:
                local_time = time(parts[0], parts[1])
            elif len(parts) == 3:
                local_time = time(parts[0], parts[1], parts[2])
            else:
                raise ValueError
        except ValueError as exc:
            raise CompilationError(f"invalid event time {value!r}") from exc

        result = datetime.combine(scenario_start.date(), local_time, tzinfo=scenario_start.tzinfo)
        if result < scenario_start:
            # Times-of-day before the scenario start are interpreted as the next day.
            result += timedelta(days=1)
        return result

    def _compile_provision(
        self,
        spec: ScenarioSpec,
        groups: dict[str, tuple[str, ...]],
    ) -> list[CompiledProvisionAction]:
        """Expand out-of-band provisioning actions to concrete nodes deterministically."""
        result: list[CompiledProvisionAction] = []
        sequence = 0
        for action in spec.provision:
            target = self._resolve_traffic_target(action.to, groups)
            for node in groups[action.nodes]:
                result.append(
                    CompiledProvisionAction(
                        sequence=sequence,
                        node=node,
                        action=action.action,
                        to=target,
                        parameters=dict(action.parameters),
                    )
                )
                sequence += 1
        return result

    def _compile_traffic(
        self,
        spec: ScenarioSpec,
        groups: dict[str, tuple[str, ...]],
        root_seed: int,
    ) -> list[CompiledTrafficAction]:
        result: list[CompiledTrafficAction] = []
        for assignment_index, assignment in enumerate(spec.traffic.assignments):
            profile = spec.traffic.profiles[assignment.profile]
            for node in groups[assignment.nodes]:
                for action_index, action in enumerate(profile.actions):
                    intervals = self._compile_interval(action.interval)
                    namespace = (
                        f"traffic:{assignment.profile}:{assignment.nodes}:{node}:"
                        f"{assignment_index}:{action_index}"
                    )
                    result.append(
                        CompiledTrafficAction(
                            assignment=assignment.profile,
                            node=node,
                            action=action.action,
                            distribution=intervals.distribution,
                            interval_seconds=intervals.interval_seconds,
                            min_seconds=intervals.min_seconds,
                            max_seconds=intervals.max_seconds,
                            mean_seconds=intervals.mean_seconds,
                            stddev_seconds=intervals.stddev_seconds,
                            median_seconds=intervals.median_seconds,
                            sigma=intervals.sigma,
                            scale_seconds=intervals.scale_seconds,
                            shape=intervals.shape,
                            to=self._resolve_traffic_target(action.to, groups),
                            parameters=dict(action.parameters),
                            seed=derive_seed(root_seed, namespace),
                        )
                    )
        return result

    @staticmethod
    def _resolve_traffic_target(
        reference: str | None, groups: dict[str, tuple[str, ...]]
    ) -> str | None:
        if reference is None:
            return None
        values = groups.get(reference)
        if values is None:
            raise CompilationError(f"traffic target references unknown node group {reference!r}")
        if len(values) != 1:
            raise CompilationError(
                f"traffic target {reference!r} expands to multiple nodes; use a singleton target"
            )
        return values[0]

    @staticmethod
    def _compile_interval(interval: IntervalSpec) -> _CompiledInterval:
        if interval.distribution == "fixed":
            assert interval.value is not None
            seconds = parse_duration(interval.value).total_seconds()
            if seconds <= 0:
                raise CompilationError("fixed traffic interval must be greater than zero")
            return _CompiledInterval(
                distribution="fixed",
                interval_seconds=seconds,
            )
        if interval.distribution == "uniform":
            assert interval.min is not None and interval.max is not None
            minimum = parse_duration(interval.min).total_seconds()
            maximum = parse_duration(interval.max).total_seconds()
            if minimum <= 0 or maximum <= 0:
                raise CompilationError("uniform traffic intervals must be greater than zero")
            if minimum > maximum:
                raise CompilationError("uniform traffic interval min cannot exceed max")
            return _CompiledInterval(
                distribution="uniform",
                min_seconds=minimum,
                max_seconds=maximum,
            )

        if interval.distribution == "exponential":
            assert interval.mean is not None
            mean = parse_duration(interval.mean).total_seconds()
            if mean <= 0:
                raise CompilationError("exponential traffic mean must be greater than zero")
            return _CompiledInterval(distribution="exponential", mean_seconds=mean)
        if interval.distribution == "normal":
            assert interval.mean is not None and interval.stddev is not None
            mean = parse_duration(interval.mean).total_seconds()
            stddev = parse_duration(interval.stddev).total_seconds()
            if mean <= 0 or stddev <= 0:
                raise CompilationError("normal traffic mean and stddev must be greater than zero")
            return _CompiledInterval(
                distribution="normal", mean_seconds=mean, stddev_seconds=stddev
            )
        if interval.distribution == "lognormal":
            assert interval.median is not None and interval.sigma is not None
            median = parse_duration(interval.median).total_seconds()
            if median <= 0 or interval.sigma <= 0:
                raise CompilationError(
                    "lognormal traffic median and sigma must be greater than zero"
                )
            return _CompiledInterval(
                distribution="lognormal", median_seconds=median, sigma=interval.sigma
            )

        assert interval.scale is not None and interval.shape is not None
        scale = parse_duration(interval.scale).total_seconds()
        if scale <= 0 or interval.shape <= 0:
            raise CompilationError(
                f"{interval.distribution} traffic scale and shape must be greater than zero"
            )
        return _CompiledInterval(
            distribution=interval.distribution,
            scale_seconds=scale,
            shape=interval.shape,
        )

    @staticmethod
    def _compile_captures(
        spec: ScenarioSpec,
        nodes: list[CompiledNode],
        links: list[CompiledLink],
    ) -> list[CompiledCapture]:
        by_endpoint: dict[tuple[str, int, int], CompiledLink] = {}
        node_interfaces: dict[tuple[str, str], tuple[int, int]] = {}
        interface_names: dict[tuple[str, int, int], str] = {}
        for node in nodes:
            for interface in node.interfaces:
                node_interfaces[(node.name, interface.name)] = (interface.adapter, interface.port)
                interface_names[(node.name, interface.adapter, interface.port)] = interface.name
        for link in links:
            by_endpoint[(link.a.node, link.a.adapter, link.a.port)] = link

        captures: list[CompiledCapture] = []
        seen_names: set[str] = set()
        seen_links: dict[str, str] = {}
        for capture in spec.captures:
            if capture.name in seen_names:
                raise CompilationError(f"duplicate capture name {capture.name!r}")
            seen_names.add(capture.name)
            endpoints: list[CompiledCaptureEndpoint] = []
            selected = list(capture.resolved_endpoints())
            if capture.segments:
                for segment in capture.segments:
                    if segment not in spec.network.segments:
                        raise CompilationError(
                            f"capture {capture.name!r} references unknown segment {segment!r}"
                        )
                    if not any(link.segment == segment for link in links):
                        raise CompilationError(
                            f"capture {capture.name!r} segment {segment!r} has no links"
                        )
                for link in links:
                    if link.segment not in capture.segments:
                        continue
                    interface_name = interface_names[(link.a.node, link.a.adapter, link.a.port)]
                    selected.append(
                        CaptureEndpointSpec(
                            node=link.a.node,
                            interface=interface_name,
                            name=f"{link.a.node}-{interface_name}",
                        )
                    )
            for index, endpoint in enumerate(selected):
                key = (endpoint.node, endpoint.interface)
                try:
                    adapter, port = node_interfaces[key]
                except KeyError as exc:
                    raise CompilationError(
                        f"capture {capture.name!r} references unknown endpoint "
                        f"{endpoint.node}.{endpoint.interface}"
                    ) from exc
                try:
                    link = by_endpoint[(endpoint.node, adapter, port)]
                except KeyError as exc:
                    raise CompilationError(
                        f"capture {capture.name!r} endpoint is not attached to a compiled link"
                    ) from exc
                previous_capture = seen_links.get(link.name)
                if previous_capture is not None:
                    raise CompilationError(
                        f"captures {previous_capture!r} and {capture.name!r} resolve to the same "
                        f"GNS3 link {link.name!r}; one link can only have one active capture"
                    )
                seen_links[link.name] = capture.name
                endpoint_name = endpoint.name or f"interface-{index + 1}"
                endpoints.append(
                    CompiledCaptureEndpoint(
                        name=endpoint_name,
                        description=endpoint.description,
                        link=link.name,
                    )
                )
            captures.append(
                CompiledCapture(
                    name=capture.name,
                    endpoints=tuple(endpoints),
                    data_link_type=capture.data_link_type,
                    output_format=capture.output_format,
                    timestamp_policy=capture.timestamp_policy,
                    deduplicate_packets=capture.deduplicate_packets,
                    metadata_policy=capture.metadata_policy,
                    metadata=CompiledCaptureMetadata(
                        comment=capture.metadata.comment,
                        hardware=capture.metadata.hardware,
                        os=capture.metadata.os,
                        application=capture.metadata.application,
                    ),
                )
            )
        return captures
