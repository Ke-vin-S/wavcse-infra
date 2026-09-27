"""Domain exceptions exposed by the control plane."""


class InfraError(Exception):
    """Base class for actionable, user-facing infrastructure failures."""


class ConfigurationError(InfraError):
    """Raised when configuration cannot be loaded or validated safely."""
