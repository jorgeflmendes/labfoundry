"""Typed projections of documented GNS3 Controller API objects."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GNS3Model(BaseModel):
    # Controller responses may acquire fields across 2.2 patch releases. We model
    # the contract we consume and preserve forward compatibility within 2.2.x.
    """Strict base model for data returned by the supported GNS3 Controller API."""

    model_config = ConfigDict(extra="allow")


class GNS3Version(GNS3Model):
    """Controller version response."""

    version: str
    local: bool | None = None


class GNS3Capabilities(GNS3Model):
    """Capabilities reported by a GNS3 compute."""

    node_types: list[str] = Field(default_factory=list)
    platform: str | None = None
    version: str | None = None


class GNS3Compute(GNS3Model):
    """A compute registered with the GNS3 controller."""

    compute_id: str
    name: str
    host: str
    port: int
    protocol: Literal["http", "https"]
    connected: bool | None = None
    capabilities: GNS3Capabilities = Field(default_factory=GNS3Capabilities)


class GNS3Template(GNS3Model):
    """Template metadata used when resolving scenario nodes."""

    template_id: str
    name: str
    template_type: str
    compute_id: str | None = None
    builtin: bool | None = None
    vmname: str | None = None
    adapters: int | None = None
    linked_clone: bool | None = None


class GNS3Project(GNS3Model):
    """Project metadata returned by the controller."""

    project_id: str
    name: str | None = None
    status: str | None = None


class GNS3Port(GNS3Model):
    """A concrete adapter and port exposed by a GNS3 node."""

    adapter_number: int
    port_number: int
    name: str | None = None
    short_name: str | None = None


class GNS3Node(GNS3Model):
    """Node metadata returned by the controller."""

    node_id: str
    name: str
    node_type: str
    compute_id: str
    project_id: str | None = None
    template_id: str | None = None
    status: str | None = None
    ports: list[GNS3Port] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)


class GNS3LinkEndpoint(GNS3Model):
    """One endpoint of a GNS3 link."""

    node_id: str
    adapter_number: int
    port_number: int


class GNS3Link(GNS3Model):
    """Link metadata returned by the controller."""

    link_id: str
    project_id: str
    nodes: list[GNS3LinkEndpoint] = Field(default_factory=list)
    capturing: bool = False
    capture_file_name: str | None = None
    capture_file_path: str | None = None
    capture_compute_id: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    suspend: bool = False


class GNS3FilterParameter(GNS3Model):
    """One parameter advertised for a GNS3 link filter."""

    name: str
    type: Literal["int", "text"]
    minimum: int | None = None
    maximum: int | None = None
    unit: str | None = None


class GNS3AvailableFilter(GNS3Model):
    """Filter capability returned by the link available-filters endpoint."""

    type: str
    name: str
    description: str | None = None
    parameters: list[GNS3FilterParameter] = Field(default_factory=list)


class GNS3Snapshot(GNS3Model):
    """Project snapshot metadata."""

    snapshot_id: str
    project_id: str
    name: str
    created_at: int


class VirtualBoxCustomAdapter(BaseModel):
    """Custom adapter fields accepted by the upstream GNS3 schema."""

    model_config = ConfigDict(extra="forbid")

    adapter_number: int
    port_name: str | None = Field(default=None, min_length=1)
    adapter_type: str | None = Field(default=None, min_length=1)
    mac_address: str | None = Field(
        default=None,
        pattern=r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$",
    )


class VirtualBoxTemplateCreate(BaseModel):
    """VirtualBox template payload aligned with the GNS3 2.2 server schema."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    vmname: str = Field(min_length=1)
    compute_id: str = "local"
    template_type: Literal["virtualbox"] = "virtualbox"
    usage: str = ""
    ram: int = Field(default=256, ge=0, le=65535)
    linked_clone: bool = False
    adapters: int = Field(default=1, ge=0, le=36)
    use_any_adapter: bool = False
    adapter_type: Literal[
        "PCnet-PCI II (Am79C970A)",
        "PCNet-FAST III (Am79C973)",
        "Intel PRO/1000 MT Desktop (82540EM)",
        "Intel PRO/1000 T Server (82543GC)",
        "Intel PRO/1000 MT Server (82545EM)",
        "Paravirtualized Network (virtio-net)",
    ] = "Intel PRO/1000 MT Desktop (82540EM)"
    first_port_name: str = ""
    port_name_format: str = "Ethernet{0}"
    port_segment_size: int = 0
    headless: bool = False
    on_close: Literal["power_off", "shutdown_signal", "save_vm_state"] = "power_off"
    console_type: Literal["telnet", "none"] = "none"
    console_auto_start: bool = False
    custom_adapters: list[VirtualBoxCustomAdapter] = Field(default_factory=list)
    category: Literal["router", "switch", "guest", "firewall"] = "guest"
    default_name_format: str = Field(default="{name}-{0}", min_length=1)
    symbol: str = Field(default=":/symbols/vbox_guest.svg", min_length=1)

    @model_validator(mode="after")
    def validate_custom_adapters(self) -> VirtualBoxTemplateCreate:
        """Ensure custom VirtualBox adapter overrides reference valid adapter indexes."""
        seen: set[int] = set()
        for custom in self.custom_adapters:
            if custom.adapter_number in seen:
                raise ValueError(f"duplicate custom adapter {custom.adapter_number}")
            if not 0 <= custom.adapter_number < self.adapters:
                valid_range = "empty" if self.adapters == 0 else f"0..{self.adapters - 1}"
                raise ValueError(
                    f"custom adapter {custom.adapter_number} is outside configured adapter range "
                    f"{valid_range}"
                )
            seen.add(custom.adapter_number)
        return self
