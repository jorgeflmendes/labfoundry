"""Pydantic model for the LabFoundry scenario DSL."""

from __future__ import annotations

import re
from datetime import datetime
from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from labfoundry.schema.common import Identifier


class StrictModel(BaseModel):
    """Frozen Pydantic base model that rejects unknown DSL fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioMeta(StrictModel):
    """Scenario identity and deterministic root seed."""

    id: Identifier
    seed: int = Field(ge=0, le=2**63 - 1)


class ClockSpec(StrictModel):
    """Scenario wall-clock start, runtime speed multiplier and optional duration."""

    start: datetime
    speed: float = Field(default=1.0, gt=0.0, le=10000.0)
    duration: str | None = None
    warp_gaps: bool = False
    sync_guests: bool = False
    settle_delay: str = "3s"

    @field_validator("start")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        """Require an explicit timezone on the scenario start timestamp."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock.start must include a timezone offset")
        return value


class SegmentSpec(StrictModel):
    """IPv4 subnet assigned to one logical network segment."""

    subnet: IPv4Network


class InterfaceSpec(StrictModel):
    """Logical node interface and optional guest-side IPv4 configuration."""

    segment: Identifier
    ip: Literal["auto", "dhcp"] | IPv4Interface = "auto"
    mac: str | None = Field(
        default=None,
        pattern=r"^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$",
    )
    gateway: IPv4Address | None = None
    dns: tuple[IPv4Address, ...] = ()
    adapter: int = Field(default=0, ge=0, le=35)
    port: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_addressing(self) -> InterfaceSpec:
        """Reject static-only network settings when DHCP owns the interface."""
        if self.ip == "dhcp" and self.gateway is not None:
            raise ValueError("DHCP interfaces cannot declare a static gateway")
        return self


class NodeSpec(StrictModel):
    """Declarative node-group definition backed by a GNS3 template."""

    template: str = Field(min_length=1)
    template_type: (
        Literal[
            "virtualbox",
            "incus",
            "docker",
            "vpcs",
            "dynamips",
            "iou",
            "vmware",
            "traceng",
            "cloud",
            "ethernet_switch",
            "ethernet_hub",
            "nat",
        ]
        | None
    ) = None
    count: int = Field(default=1, ge=1, le=1000)
    interfaces: dict[Identifier, InterfaceSpec] = Field(default_factory=dict)
    guest_profile: Identifier | None = None
    ipv4_forwarding: bool = False

    @model_validator(mode="after")
    def unique_adapter_ports(self) -> NodeSpec:
        """Reject duplicate mappings and nonzero ports on single-port VM adapters."""
        used: set[tuple[int, int]] = set()
        for name, interface in self.interfaces.items():
            key = (interface.adapter, interface.port)
            if key in used:
                raise ValueError(f"interface {name!r} reuses adapter/port {key}")
            if self.template_type in {"virtualbox", "incus"} and interface.port != 0:
                raise ValueError(f"{self.template_type} interface {name!r} must use port 0")
            used.add(key)
        if self.template_type == "incus" and not self.guest_profile:
            raise ValueError("Incus nodes require a guest_profile")
        return self


class EgressSpec(StrictModel):
    """Explicit real-world egress attached to one logical segment through GNS3 NAT."""

    mode: Literal["nat"] = "nat"
    segment: Identifier
    name: Identifier = "labfoundry-nat"


class NetworkSpec(StrictModel):
    """Collection of logical segments, node groups and optional real-world egress."""

    segments: dict[Identifier, SegmentSpec]
    nodes: dict[Identifier, NodeSpec]
    egress: EgressSpec | None = None
    allow_cloud_nodes: bool = False

    @model_validator(mode="after")
    def validate_network(self) -> NetworkSpec:
        """Reject overlapping segments, unsafe cloud nodes and invalid egress references."""
        names = list(self.segments)
        for index, left_name in enumerate(names):
            left = self.segments[left_name].subnet
            for right_name in names[index + 1 :]:
                right = self.segments[right_name].subnet
                if left.overlaps(right):
                    raise ValueError(
                        f"network segments {left_name!r} ({left}) and "
                        f"{right_name!r} ({right}) overlap"
                    )
        if self.egress is not None and self.egress.segment not in self.segments:
            raise ValueError(f"egress references unknown segment {self.egress.segment!r}")
        if any(node.template_type == "nat" for node in self.nodes.values()):
            raise ValueError("NAT nodes must be declared through network.egress")
        if not self.allow_cloud_nodes:
            cloud_groups = [
                name for name, node in self.nodes.items() if node.template_type == "cloud"
            ]
            if cloud_groups:
                raise ValueError(
                    "cloud templates can expose host interfaces to a scenario; set "
                    "network.allow_cloud_nodes=true explicitly to allow them: "
                    + ", ".join(sorted(cloud_groups))
                )
        return self


class SelectorSpec(StrictModel):
    """Deterministic event selector that binds chosen nodes to an alias."""

    from_: Identifier = Field(alias="from")
    count: int = Field(default=1, ge=1)
    as_: Identifier = Field(alias="as")


class EventAfterSpec(StrictModel):
    """Relative event timing expression."""

    event: Identifier | None = None
    delay: str


class EventConditionSpec(StrictModel):
    """Expected result from an earlier guest command before an event can run."""

    event: Identifier
    exit_code: int | None = None
    stdout_matches: str | None = Field(default=None, max_length=4096)
    stderr_matches: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def has_expectation(self) -> EventConditionSpec:
        """Require at least one result predicate."""
        if self.exit_code is None and self.stdout_matches is None and self.stderr_matches is None:
            raise ValueError("event condition needs exit_code, stdout_matches, or stderr_matches")
        for name, pattern in (
            ("stdout_matches", self.stdout_matches),
            ("stderr_matches", self.stderr_matches),
        ):
            if pattern is not None:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"{name} is not a valid regular expression") from exc
        return self


class EventSpec(StrictModel):
    """One semantic timeline event."""

    id: Identifier | None = None
    at: str | None = None
    after: str | EventAfterSpec | None = None
    select: SelectorSpec | None = None
    when: EventConditionSpec | None = None
    action: str = Field(min_length=1)
    from_: Identifier | None = Field(default=None, alias="from")
    to: Identifier | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def one_time_expression(self) -> EventSpec:
        """Require exactly one absolute or relative time expression."""
        if (self.at is None) == (self.after is None):
            raise ValueError("event must define exactly one of 'at' or 'after'")
        return self


class IntervalSpec(StrictModel):
    """Distribution specification for recurring background traffic."""

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
    value: str | None = None
    min: str | None = None
    max: str | None = None
    mean: str | None = None
    stddev: str | None = None
    median: str | None = None
    sigma: float | None = None
    scale: str | None = None
    shape: float | None = None

    @model_validator(mode="after")
    def fields_match_distribution(self) -> IntervalSpec:
        """Require only the parameters valid for the selected interval distribution."""
        present = {
            name
            for name, value in {
                "value": self.value,
                "min": self.min,
                "max": self.max,
                "mean": self.mean,
                "stddev": self.stddev,
                "median": self.median,
                "sigma": self.sigma,
                "scale": self.scale,
                "shape": self.shape,
            }.items()
            if value is not None
        }
        expected = {
            "fixed": {"value"},
            "uniform": {"min", "max"},
            "exponential": {"mean"},
            "normal": {"mean", "stddev"},
            "lognormal": {"median", "sigma"},
            "gamma": {"shape", "scale"},
            "weibull": {"shape", "scale"},
            "pareto": {"shape", "scale"},
        }[self.distribution]
        if present != expected:
            raise ValueError(
                f"{self.distribution} interval requires exactly {sorted(expected)!r}; "
                f"got {sorted(present)!r}"
            )
        return self


class TrafficActionSpec(StrictModel):
    """One recurring semantic traffic action in a profile."""

    action: str = Field(min_length=1)
    interval: IntervalSpec
    to: Identifier | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class TrafficProfileSpec(StrictModel):
    """Named collection of recurring traffic actions."""

    actions: list[TrafficActionSpec]


class TrafficAssignmentSpec(StrictModel):
    """Assignment of a traffic profile to a logical node group."""

    profile: Identifier
    nodes: Identifier


class TrafficSpec(StrictModel):
    """Background-traffic profiles and assignments."""

    profiles: dict[Identifier, TrafficProfileSpec] = Field(default_factory=dict)
    assignments: list[TrafficAssignmentSpec] = Field(default_factory=list)


class BackgroundActivitySpec(StrictModel):
    """A guest command eligible for a warped idle gap."""

    from_: Identifier = Field(alias="from")
    to: Identifier | None = None
    weight: int = Field(default=1, ge=1)
    parameters: dict[str, Any]


class BackgroundTrafficSpec(StrictModel):
    """Bounded guest activity inserted between scheduled events."""

    min_gap: str
    min_interval: str
    max_interval: str
    max_idle_gap: str | None = None
    max_events_per_gap: int = Field(ge=1, le=100)
    activities: tuple[BackgroundActivitySpec, ...] = Field(min_length=1)


class ProvisionActionSpec(StrictModel):
    """Out-of-band guest action executed after network setup and before packet capture."""

    nodes: Identifier
    action: Literal["guest.exec", "guest.start", "guest.upload", "guest.copy", "guest.mkdir"]
    to: Identifier | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class CaptureEndpointSpec(StrictModel):
    """Logical node interface used as a packet-capture observation point."""

    node: Identifier
    interface: Identifier
    name: Identifier | None = None
    description: str | None = Field(default=None, max_length=4096)


class CaptureMetadataSpec(StrictModel):
    """Optional PCAPNG section metadata for an exported capture."""

    comment: str | None = Field(default=None, max_length=4096)
    hardware: str | None = Field(default=None, max_length=1024)
    os: str | None = Field(default=None, max_length=1024)
    application: str | None = Field(default=None, max_length=1024)


class RealismAuditSpec(StrictModel):
    """Audit settings applied before evidence is published."""

    enabled: bool = True
    baseline: str | None = Field(default=None, min_length=1)
    fail_on: Literal["never", "error", "warning"] = "error"


class EvidenceSpec(StrictModel):
    """Evidence export and realism-audit settings."""

    realism_audit: RealismAuditSpec = Field(default_factory=RealismAuditSpec)


class CaptureSpec(StrictModel):
    """Student-facing capture declaration."""

    name: Identifier
    endpoint: CaptureEndpointSpec | None = None
    endpoints: tuple[CaptureEndpointSpec, ...] = ()
    segments: tuple[Identifier, ...] = ()
    data_link_type: Literal[
        "DLT_ATM_RFC1483",
        "DLT_EN10MB",
        "DLT_FRELAY",
        "DLT_C_HDLC",
        "DLT_PPP_SERIAL",
    ] = "DLT_EN10MB"
    output_format: Literal["pcap", "pcapng"] = Field(default="pcap", alias="format")
    timestamp_policy: Literal["retime", "preserve"] = "retime"
    deduplicate_packets: bool = Field(default=False, alias="deduplicate")
    metadata_policy: Literal["preserve", "minimal"] = "preserve"
    metadata: CaptureMetadataSpec = Field(default_factory=CaptureMetadataSpec)

    @model_validator(mode="after")
    def capture_endpoints_match_output_format(self) -> CaptureSpec:
        """Require one legacy endpoint or a non-empty endpoint collection."""
        if sum((self.endpoint is not None, bool(self.endpoints), bool(self.segments))) > 1:
            raise ValueError("capture must use one of endpoint, endpoints or segments")
        endpoint_count = len(self.endpoints) if self.endpoint is None else 1
        if endpoint_count == 0 and not self.segments:
            raise ValueError("capture requires endpoint, endpoints or segments")
        if self.output_format == "pcap" and (endpoint_count != 1 or self.segments):
            raise ValueError("multi-interface captures require format: pcapng")
        if len(set(self.segments)) != len(self.segments):
            raise ValueError("capture segments must be unique")
        if self.endpoints and any(item.name is None for item in self.endpoints):
            raise ValueError("each endpoint in endpoints requires a name")
        names = [item.name for item in self.resolved_endpoints()]
        if len(set(names)) != len(names):
            raise ValueError("capture endpoint names must be unique")
        return self

    def resolved_endpoints(self) -> tuple[CaptureEndpointSpec, ...]:
        """Return capture endpoints in their declared deterministic order."""
        if self.endpoint is not None:
            return (self.endpoint,)
        return self.endpoints


class ScenarioSpec(StrictModel):
    """Validated root model for the LabFoundry scenario DSL."""

    version: Literal["0.2"] = "0.2"
    scenario: ScenarioMeta
    clock: ClockSpec
    network: NetworkSpec
    traffic: TrafficSpec = Field(default_factory=TrafficSpec)
    background_traffic: BackgroundTrafficSpec | None = None
    provision: list[ProvisionActionSpec] = Field(default_factory=list)
    events: list[EventSpec] = Field(default_factory=list)
    captures: list[CaptureSpec] = Field(default_factory=list)
    evidence: EvidenceSpec = Field(default_factory=EvidenceSpec)

    @model_validator(mode="after")
    def validate_references(self) -> ScenarioSpec:
        """Validate cross-references between network and traffic sections."""
        segment_names = set(self.network.segments)
        node_names = set(self.network.nodes)
        for node_name, node in self.network.nodes.items():
            for interface_name, interface in node.interfaces.items():
                if interface.segment not in segment_names:
                    raise ValueError(
                        f"node {node_name}.{interface_name} references unknown segment "
                        f"{interface.segment!r}"
                    )
                if isinstance(interface.ip, IPv4Interface):
                    network = self.network.segments[interface.segment].subnet
                    if interface.ip.ip not in network:
                        raise ValueError(
                            f"node {node_name}.{interface_name} address {interface.ip} "
                            f"is outside segment {interface.segment!r} ({network})"
                        )
                    if interface.ip.network.prefixlen != network.prefixlen:
                        raise ValueError(
                            f"node {node_name}.{interface_name} prefix /"
                            f"{interface.ip.network.prefixlen} does not match segment "
                            f"{interface.segment!r} prefix /{network.prefixlen}"
                        )
                if interface.gateway is not None:
                    network = self.network.segments[interface.segment].subnet
                    if interface.gateway not in network:
                        raise ValueError(
                            f"node {node_name}.{interface_name} gateway {interface.gateway} "
                            f"is outside segment {interface.segment!r} ({network})"
                        )
                    if interface.gateway in {network.network_address, network.broadcast_address}:
                        raise ValueError(
                            f"node {node_name}.{interface_name} gateway {interface.gateway} "
                            "cannot be the network or broadcast address"
                        )
        for assignment in self.traffic.assignments:
            if assignment.profile not in self.traffic.profiles:
                raise ValueError(f"unknown traffic profile {assignment.profile!r}")
            if assignment.nodes not in node_names:
                raise ValueError(f"unknown traffic node group {assignment.nodes!r}")
        if self.background_traffic is not None:
            for activity in self.background_traffic.activities:
                if activity.from_ not in node_names:
                    raise ValueError(f"unknown background node group {activity.from_!r}")
                if activity.to is not None and activity.to not in node_names:
                    raise ValueError(f"unknown background target node group {activity.to!r}")
        for action in self.provision:
            if action.nodes not in node_names:
                raise ValueError(f"unknown provision node group {action.nodes!r}")
            if action.to is not None and action.to not in node_names:
                raise ValueError(f"unknown provision target node group {action.to!r}")
        return self
