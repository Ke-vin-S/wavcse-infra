"""Domain exceptions exposed by the control plane."""


class InfraError(Exception):
    """Base class for actionable, user-facing infrastructure failures."""


class ConfigurationError(InfraError):
    """Raised when configuration cannot be loaded or validated safely."""


class CredentialError(ConfigurationError):
    """Raised when a configured runtime credential cannot be resolved safely."""


class ProviderError(InfraError):
    """Base class for sanitized provider failures."""


class ProviderAuthenticationError(ProviderError):
    """Raised when a provider rejects configured credentials."""


class ProviderPermissionError(ProviderError):
    """Raised when valid provider credentials lack an operation permission."""


class ProviderNotFoundError(ProviderError):
    """Raised when a requested provider resource does not exist."""


class ProviderResponseError(ProviderError):
    """Raised when a provider response does not match its documented contract."""


class ProviderUnavailableError(ProviderError):
    """Raised after a transient provider failure exhausts safe retries."""


class ProviderValidationError(ProviderError):
    """Raised when a provider rejects an invalid resource request."""


class ProviderConflictError(ProviderError):
    """Raised when an operation is invalid for the provider resource state."""


class ProviderOperationAmbiguousError(ProviderError):
    """Raised when a mutation may have succeeded despite losing its response."""


class AmbiguousCreateError(ProviderOperationAmbiguousError):
    """Raised when a paid create cannot be safely retried or reconciled."""


class CostGuardError(InfraError):
    """Raised when provider pricing cannot satisfy an operator cost guard."""


class ResourceUnavailableError(InfraError):
    """Raised when an explicitly requested provider resource has no capacity."""


class LifecycleError(InfraError):
    """Raised when a worker cannot complete a requested lifecycle transition."""


class LifecycleTimeoutError(LifecycleError):
    """Raised when a bounded lifecycle wait expires."""


class SshError(InfraError):
    """Base class for sanitized worker SSH failures."""


class SshConfigurationError(SshError):
    """Raised when controller SSH configuration cannot be used safely."""


class SshEndpointUnavailableError(SshError):
    """Raised when a running worker has no usable provider SSH endpoint."""


class SshConnectionError(SshError):
    """Raised when the controller cannot establish a worker SSH session."""


class SshAuthenticationError(SshConnectionError):
    """Raised when the configured controller key is not accepted by a worker."""


class SshHostKeyError(SshConnectionError):
    """Raised when dedicated known-hosts verification rejects a worker host key."""


class SshCommandError(SshError):
    """Raised when a remote worker command exits unsuccessfully."""


class SshCommandTimeoutError(SshCommandError):
    """Raised when a bounded remote command does not finish in time."""


class SshReadinessTimeoutError(SshError):
    """Raised when a worker does not become SSH-ready within the configured bound."""


class WorkerBootstrapError(InfraError):
    """Raised when an idempotent worker bootstrap cannot complete."""


class WorkerHealthError(InfraError):
    """Raised when required worker health checks do not pass."""


class UnsupportedAcceleratorError(WorkerHealthError):
    """Raised when Phase 4 has no safe health check for a worker accelerator."""


class StateError(InfraError):
    """Raised when supplemental local operational state cannot be handled safely."""


class StorageError(InfraError):
    """Base class for sanitized canonical-storage failures."""


class StorageKeyError(StorageError):
    """Raised when an artifact key is unsafe, ambiguous, or outside the namespace."""


class StorageObjectNotFoundError(StorageError):
    """Raised when a required S3 object does not exist."""


class StorageObjectExistsError(StorageError):
    """Raised when an operation would replace an existing S3 object unexpectedly."""


class StoragePermissionError(StorageError):
    """Raised when the controller identity lacks permission for a storage operation."""


class StorageVerificationError(StorageError):
    """Raised when stored object metadata contradicts the expected artifact."""


class ArtifactTransferError(StorageError):
    """Raised when a worker artifact transfer fails or violates its protocol."""


class ArtifactSizeMismatchError(ArtifactTransferError):
    """Raised when an artifact does not match its expected byte size."""


class ArtifactChecksumMismatchError(ArtifactTransferError):
    """Raised when an artifact does not match its expected SHA-256 digest."""
