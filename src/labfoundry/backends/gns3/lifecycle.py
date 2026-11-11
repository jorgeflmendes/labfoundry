"""Node-type hooks used by the GNS3 deployment adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from labfoundry.backends.gns3.models import GNS3Node, GNS3Template
from labfoundry.compiler.plan import CompiledNode

if TYPE_CHECKING:
    from labfoundry.backends.gns3.backend import DeploymentState


class NodeLifecycle(Protocol):
    """Validate node types and handle host-specific lifecycle operations."""

    def validate_template(self, node: CompiledNode, template: GNS3Template) -> None:
        """Reject incompatible template settings before project creation."""

    def runtime_id(self, node: GNS3Node, template: GNS3Template) -> str:
        """Resolve the identifier used by the guest transport."""
        ...

    async def prepare_node(self, node: GNS3Node, template: GNS3Template) -> GNS3Node:
        """Configure per-node resources before the first start."""
        ...

    def start_priority(self, compute_id: str) -> int:
        """Return zero for normal startup or a positive value for ordered startup."""
        ...

    async def before_start(self, deployment: DeploymentState, compute_id: str) -> None:
        """Prepare host resources before starting nodes."""

    async def after_start(self, deployment: DeploymentState, compute_id: str) -> None:
        """Verify started resources; failures stop the project nodes."""

    async def after_destroy(self, deployment: DeploymentState, compute_id: str) -> None:
        """Clean up host resources after project deletion."""
