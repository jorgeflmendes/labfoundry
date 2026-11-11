"""Immutable compiled scenario plan."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PlanModel(BaseModel):
    """Frozen strict base model for compiled scenario-plan records."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CompiledInterface(PlanModel):
    """Resolved guest addressing and GNS3 adapter coordinates."""

    name: str
    segment: str
    address: str
    mac: str | None = None
    gateway: str | None = None
    dns: tuple[str, ...] = ()
    adapter: int
    port: int


class CompiledNode(PlanModel):
    """A concrete node generated from a scenario node group."""

    name: str
    group: str
    template: str
    expected_template_type: str | None
    interfaces: tuple[CompiledInterface, ...]
    guest_profile: str | None = None
    ipv4_forwarding: bool = False
    x: int
    y: int


class CompiledSegment(PlanModel):
    """A logical network segment compiled to a GNS3 Ethernet switch."""

    name: str
    subnet: str
    switch_name: str
    port_count: int
    x: int
    y: int


class LinkEndpoint(PlanModel):
    """A concrete endpoint used by a compiled GNS3 link."""

    node: str
    adapter: int
    port: int


class CompiledLink(PlanModel):
    """A deterministic point-to-point connection in the deployment plan."""

    name: str
    segment: str
    a: LinkEndpoint
    b: LinkEndpoint


class CompiledEgress(PlanModel):
    """Explicit GNS3 NAT attachment that permits real-world egress."""

    mode: Literal["nat"]
    name: str
    segment: str
    switch_port: int


class CompiledEventCondition(PlanModel):
    """Result predicate that gates a compiled event."""

    event: str
    exit_code: int | None = None
    stdout_matches: str | None = None
    stderr_matches: str | None = None


class CompiledEvent(PlanModel):
    """A one-off semantic event resolved to an absolute scenario timestamp."""

    sequence: int
    id: str
    timestamp: datetime
    action: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    parameters: dict[str, Any]
    when: CompiledEventCondition | None = None


class CompiledProvisionAction(PlanModel):
    """One out-of-band guest action executed before evidence capture starts."""

    sequence: int
    node: str
    action: Literal["guest.exec", "guest.start", "guest.upload", "guest.copy", "guest.mkdir"]
    to: str | None = None
    parameters: dict[str, Any]


class CompiledTrafficAction(PlanModel):
    """A deterministic recurring background-traffic action."""

    assignment: str
    node: str
    action: str
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
    to: str | None = None
    parameters: dict[str, Any]
    seed: int


class CompiledCaptureEndpoint(PlanModel):
    """One logical PCAPNG interface resolved to a compiled GNS3 link."""

    name: str
    description: str | None = None
    link: str


class CompiledCaptureMetadata(PlanModel):
    """PCAPNG section metadata declared by a scenario author."""

    comment: str | None = None
    hardware: str | None = None
    os: str | None = None
    application: str | None = None


class CompiledCapture(PlanModel):
    """A capture definition resolved to one or more compiled links."""

    name: str
    endpoints: tuple[CompiledCaptureEndpoint, ...]
    data_link_type: str
    output_format: Literal["pcap", "pcapng"]
    timestamp_policy: Literal["retime", "preserve"]
    deduplicate_packets: bool = False
    metadata_policy: Literal["preserve", "minimal"] = "preserve"
    metadata: CompiledCaptureMetadata = Field(default_factory=CompiledCaptureMetadata)

    @property
    def link(self) -> str:
        """Return the sole link for legacy single-interface capture consumers."""
        if len(self.endpoints) != 1:
            raise ValueError(f"capture {self.name!r} has multiple links")
        return self.endpoints[0].link


class CompiledRealismAudit(PlanModel):
    """Audit policy carried from the scenario into a runtime plan."""

    enabled: bool = True
    baseline: str | None = None
    fail_on: Literal["never", "error", "warning"] = "error"


class ScenarioPlan(PlanModel):
    """Complete deterministic deployment and runtime plan produced by the compiler."""

    schema_version: Literal["0.2"] = "0.2"
    scenario_id: str
    root_seed: int
    scenario_start: datetime
    clock_speed: float
    warp_gaps: bool = False
    max_idle_gap_seconds: float | None = None
    sync_guest_clocks: bool = False
    settle_delay_seconds: float = 3.0
    duration_seconds: float | None = None
    allow_cloud_nodes: bool = False
    segments: tuple[CompiledSegment, ...]
    nodes: tuple[CompiledNode, ...]
    links: tuple[CompiledLink, ...]
    egress: CompiledEgress | None = None
    events: tuple[CompiledEvent, ...]
    traffic: tuple[CompiledTrafficAction, ...]
    provision: tuple[CompiledProvisionAction, ...] = ()
    captures: tuple[CompiledCapture, ...]
    realism_audit: CompiledRealismAudit = Field(default_factory=CompiledRealismAudit)
    aliases: dict[str, tuple[str, ...]]
