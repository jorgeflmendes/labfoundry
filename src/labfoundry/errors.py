"""LabFoundry exception hierarchy."""


class LabFoundryError(Exception):
    """Base class for all LabFoundry errors."""


class ScenarioValidationError(LabFoundryError):
    """Raised when a scenario is semantically invalid."""


class CompilationError(LabFoundryError):
    """Raised when a valid DSL cannot be compiled."""


class BackendError(LabFoundryError):
    """Raised by infrastructure backends."""


class GNS3APIError(BackendError):
    """Raised when the GNS3 controller returns an unsuccessful response."""

    def __init__(self, status_code: int, message: str, *, method: str, path: str) -> None:
        self.status_code = status_code
        self.message = message
        self.method = method
        self.path = path
        super().__init__(f"GNS3 API {method} {path} failed ({status_code}): {message}")


class GNS3TransportError(BackendError):
    """Raised when the GNS3 controller cannot be reached reliably."""

    def __init__(self, message: str, *, method: str, path: str) -> None:
        self.message = message
        self.method = method
        self.path = path
        super().__init__(f"GNS3 transport {method} {path} failed: {message}")


class GNS3CompatibilityError(BackendError):
    """Raised when the connected GNS3 server is outside the supported contract."""


class GNS3ProtocolError(BackendError):
    """Raised when a successful GNS3 response violates the expected API contract."""

    def __init__(self, message: str, *, method: str, path: str) -> None:
        self.message = message
        self.method = method
        self.path = path
        super().__init__(f"GNS3 protocol {method} {path} failed: {message}")


class GuestError(LabFoundryError):
    """Base class for guest-control failures."""


class GuestConfigurationError(GuestError):
    """Raised when guest profiles or transport configuration are incomplete."""


class GuestExecutionError(GuestError):
    """Raised when a command, transfer or process operation fails inside a guest."""


class RuntimeOrchestrationError(LabFoundryError):
    """Raised when a full scenario lifecycle cannot complete safely."""


class EvidenceError(LabFoundryError):
    """Raised when capture/export orchestration cannot complete safely."""


class PcapError(LabFoundryError):
    """Raised for malformed or unsupported packet capture files."""
