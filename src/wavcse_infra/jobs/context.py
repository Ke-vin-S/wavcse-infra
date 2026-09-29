"""Shared wiring and worker preconditions for recorded jobs.

Submission needs a READY worker, while status, logs, and cancellation only need a
reachable one. Both share the same collaborators, so they are assembled once here
instead of being rebuilt in every command.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from wavcse_infra.config import JobsConfig
from wavcse_infra.errors import ConfigurationError, JobPreconditionError, ProviderNotFoundError
from wavcse_infra.jobs.execution import JobExecutor
from wavcse_infra.jobs.state import JobStateStore
from wavcse_infra.models import Worker, WorkerReadinessState, WorkerState
from wavcse_infra.state import WorkerRecord, WorkerStateStore
from wavcse_infra.storage.cache import WorkerArtifactCache
from wavcse_infra.storage.s3 import S3Storage
from wavcse_infra.storage.transfer import WorkerArtifactTransfer


class WorkerReader(Protocol):
    """Provider read surface needed to resolve one explicit worker."""

    def get_worker(self, worker_id: str) -> Worker: ...


@dataclass(frozen=True)
class JobContext:
    """Collaborators shared by job submission, status, logs, and cancellation."""

    provider: WorkerReader
    worker_state: WorkerStateStore
    job_store: JobStateStore
    executor: JobExecutor
    transfer: WorkerArtifactTransfer
    storage: S3Storage | None
    jobs_config: JobsConfig
    environ: Mapping[str, str] = field(default_factory=dict)
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    cache: WorkerArtifactCache | None = None

    def cache_root(self, worker_id: str) -> str | None:
        """Return the worker's rebuildable cache root, or `None` when it has no volume.

        The root is the mount path the provider reported for the Pod's network volume: it is
        provider state, not a path this tool invents. A worker without a network volume has
        no cache root at all, which is what keeps a Pod without persistent storage on exactly
        the canonical download path it used before.
        """

        if self.cache is None:
            return None
        record = self.worker_state.get(worker_id)
        return None if record is None else record.network_volume_mount_path


def require_storage(context: JobContext) -> S3Storage:
    """Return configured canonical storage or fail with an actionable reason."""

    if context.storage is None:
        raise ConfigurationError(
            "storage.bucket and aws.region are required to materialize declared inputs or "
            "persist declared outputs; set them in the user configuration file or omit "
            "inputs/outputs from the job specification"
        )
    return context.storage


def require_ready_worker(context: JobContext, worker_id: str) -> tuple[Worker, WorkerRecord]:
    """Return a provider-running, locally READY worker or fail with an actionable reason.

    Phase 6 never bootstraps, starts, or creates a worker implicitly. The recorded local
    readiness is what `infra worker bootstrap`/`health` last proved; submission re-proves
    SSH and the bootstrap marker when it installs the runner and prepares the source.
    """

    try:
        worker = context.provider.get_worker(worker_id)
    except ProviderNotFoundError as exc:
        raise JobPreconditionError(
            f"RunPod worker {worker_id} does not exist; create the worker explicitly and "
            "run `infra worker bootstrap <worker-id>` before submitting a job"
        ) from exc
    if worker.state is not WorkerState.RUNNING:
        raise JobPreconditionError(
            f"RunPod worker {worker_id} is {worker.state.value}; a recorded job requires a "
            "RUNNING worker. Start it explicitly and re-check readiness"
        )
    record = context.worker_state.get(worker_id)
    if record is None:
        raise JobPreconditionError(
            f"Worker {worker_id} is not tracked locally, so its readiness is unknown; run "
            "`infra worker bootstrap <worker-id>` on this controller first"
        )
    if record.readiness_state is not WorkerReadinessState.READY:
        raise JobPreconditionError(
            f"Worker {worker_id} local readiness is {record.readiness_state.value}, not READY; "
            "run `infra worker bootstrap <worker-id>` (or `infra worker health <worker-id>`) "
            "and retry"
        )
    return worker, record
