"""Domain exceptions exposed by the control plane."""


class InfraError(Exception):
    """Base class for actionable, user-facing infrastructure failures."""


class ReconcilableOperationError(InfraError):
    """An operation did not complete, and that is not evidence of failure.

    This is the single semantic distinction Phase 6.1 turns on. Every subclass means
    "remote/canonical truth is currently unknown, or the operation can be retried from
    evidence that still exists" — never "the job failed". Callers must keep the affected
    job or worker in a reconcilable nonterminal state and re-derive its outcome from
    evidence; recording FAILED from this class alone is forbidden.
    """


class RemoteOperationInterruptedError(ReconcilableOperationError):
    """Raised when a bounded remote operation ended without reporting an outcome.

    The controller's SSH command may have been cut short by its own bound or by a dropped
    connection while the remote process kept running. The remote state is unknown, so this
    must never be turned into a terminal job or transfer failure on its own.
    """


class ArtifactTransferTransientError(ReconcilableOperationError):
    """Raised when a bounded transfer attempt exhausted its retries, or could not start.

    The worker keeps resumable state next to the destination, so a later attempt with
    freshly issued credentials continues the work. The attempt's own retries stay bounded;
    the recovery opportunity is the next job-level attempt, not an unbounded loop.
    """


class ConfigurationError(InfraError):
    """Raised when configuration cannot be loaded or validated safely."""


class CredentialError(ConfigurationError):
    """Raised when a configured runtime credential cannot be resolved safely."""


class ProviderError(InfraError):
    """Base class for sanitized provider failures."""


class ProviderAuthenticationError(ProviderError):
    """Raised when a provider rejects configured credentials."""


class ColabCliMissingError(ProviderError):
    """The pinned controller-side Colab CLI is not installed."""


class ColabAuthenticationRequiredError(ProviderAuthenticationError):
    """ADC is absent, expired, or missing the Colab scopes."""


class ColabQuotaError(ProviderError):
    """The account cannot currently allocate more Colab compute."""


class ColabAcceleratorUnavailableError(ProviderError):
    """The requested accelerator cannot be allocated on this account."""


class UnsupportedProviderOperationError(ProviderError):
    """An operation would misrepresent the provider's actual lifecycle."""


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


class UnresolvedCreateError(InfraError):
    """Raised when a new billable create is refused while an earlier one is unresolved.

    The provider gives no idempotency key and no unique-name constraint, so issuing a second
    create after a lost response is exactly how a duplicate paid resource appears. The
    recovery is to reconcile the recorded intent against the provider, never to try again.
    """


class CostGuardError(InfraError):
    """Raised when provider pricing cannot satisfy an operator cost guard."""


class ResourceUnavailableError(InfraError):
    """Raised when an explicitly requested provider resource has no capacity."""


class LifecycleError(InfraError):
    """Raised when a worker cannot complete a requested lifecycle transition."""


class LifecycleTimeoutError(LifecycleError):
    """Raised when a bounded lifecycle wait expires."""


class WorkerPlacementError(LifecycleError):
    """Raised when a created Pod is not placed where an attached resource requires.

    A network volume exists in exactly one data center, so a Pod that mounts it must be
    placed there. The provider accepted the create request and produced a Pod, which is why
    this is a distinct, high-severity outcome: the Pod exists, costs money, and cannot use
    the volume it was created for. It is never raised before the create request, because
    placement is only knowable from the provider's answer.
    """


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


class StorageUnavailableError(StorageError, ReconcilableOperationError):
    """Raised when canonical storage cannot be observed or reached right now.

    A transport failure, a throttled or server-side error, or a controller credential that
    is about to expire says nothing about the artifact: the same request is expected to
    succeed later. The object being definitively absent, an authorization failure, and a
    failed integrity check are different classes on purpose and stay definitive.
    """


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


class ArtifactTransferInProgressError(ArtifactTransferError, ReconcilableOperationError):
    """Raised when another transfer already owns the requested destination.

    The worker serializes transfers to one destination with an advisory lock, so this is
    positive evidence that a previous invocation is still running. It is never evidence
    that the operation failed, so it is reconcilable like every other non-outcome.
    """


class ArtifactDestinationExistsError(ArtifactTransferError):
    """Raised when a destination already holds a file this transfer must not replace.

    The caller must decide from worker evidence whether that file is the verified artifact
    it wanted (a previous attempt completed in the background) or something else.
    """


class ArtifactSizeMismatchError(ArtifactTransferError):
    """Raised when an artifact does not match its expected byte size."""


class ArtifactChecksumMismatchError(ArtifactTransferError):
    """Raised when an artifact does not match its expected SHA-256 digest."""


class CacheError(StorageError):
    """Raised when a worker's rebuildable artifact cache cannot be used as intended.

    The cache is an optimization over canonical storage. Every caller must treat this class
    as "the shortcut is unavailable", never as "the artifact failed": the canonical download
    path remains correct and is the fallback.
    """


class JobError(InfraError):
    """Base class for sanitized job specification and execution failures."""


class JobSpecError(JobError):
    """Raised when a versioned job specification cannot be used as written."""


class JobStateError(JobError):
    """Raised when durable local job state cannot be read or transitioned safely."""


class JobPreconditionError(JobError):
    """Raised when an explicit worker cannot accept a recorded job yet."""


class JobExecutionError(JobError):
    """Raised when a remote job phase fails in a way the operator must fix."""


class JobCancellationError(JobError):
    """Raised when a running job process cannot be cancelled safely."""


class JobLaunchExcludedError(JobExecutionError):
    """Raised when the worker refuses to launch a job because cancellation already won.

    The worker records that decision durably before answering, so every later launch of the
    same job refuses too. This is affirmative evidence that the command did not and will
    not run, which is exactly what recording CANCELLED requires.
    """
