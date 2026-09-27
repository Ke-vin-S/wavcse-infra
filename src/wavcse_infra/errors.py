"""Domain exceptions exposed by the control plane."""


class InfraError(Exception):
    """Base class for actionable, user-facing infrastructure failures."""


class ConfigurationError(InfraError):
    """Raised when configuration cannot be loaded or validated safely."""


class ProviderError(InfraError):
    """Base class for sanitized provider failures."""


class ProviderAuthenticationError(ProviderError):
    """Raised when a provider rejects configured credentials."""


class ProviderNotFoundError(ProviderError):
    """Raised when a requested provider resource does not exist."""


class ProviderResponseError(ProviderError):
    """Raised when a provider response does not match its documented contract."""


class ProviderUnavailableError(ProviderError):
    """Raised after a transient provider failure exhausts safe retries."""
