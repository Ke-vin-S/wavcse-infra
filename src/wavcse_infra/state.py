"""Atomic supplemental controller state for workers created by this project.

Every mutation replaces the whole document, so a read-decide-write transition must be
serialized: two controller processes that both read, decide, and write would otherwise let
the slower one overwrite the newer state. Each mutation therefore takes one local advisory
lock on the state document, re-reads it inside the lock, and merges its change into what
it finds there - the same single-machine concurrency model the per-job store uses.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wavcse_infra.errors import StateError, UnresolvedCreateError
from wavcse_infra.models import (
    CloudType,
    ColabBillingMode,
    ExecutionTransport,
    NetworkVolume,
    NetworkVolumeSpec,
    ProviderKind,
    VolumeType,
    Worker,
    WorkerConnectionInfo,
    WorkerCreationPlan,
    WorkerHealthReport,
    WorkerReadinessState,
    WorkerState,
    readiness_at_least,
)

DEFAULT_STATE_PATH = Path("~/.local/state/wavcse-infra/workers.json")
DEFAULT_VOLUME_STATE_PATH = Path("~/.local/state/wavcse-infra/volumes.json")

# The document lock is held for one read-decide-write transition, and one transition may
# call another (a create that upserts), so the descriptor is cached and its depth counted
# instead of taking a second flock on a second descriptor, which would block against itself.
_LOCK_DEPTH: dict[str, int] = {}
_LOCK_DESCRIPTORS: dict[str, int] = {}


def _serialized(method: Any) -> Any:
    """Run one state mutation while holding its document's local lock."""

    @wraps(method)
    def wrapper(self: _AtomicJsonDocumentStore, *args: Any, **kwargs: Any) -> Any:
        with self.locked():
            return method(self, *args, **kwargs)

    return wrapper


def write_json_atomically(path: Path, payload: object) -> None:
    """Replace one JSON document with a same-directory temporary file and atomic rename.

    The containing directory is created with mode 0700 and the document with mode 0600,
    because controller-side operational state may name workers, jobs, and paths but must
    never contain credentials. The temporary file is removed when the write fails.
    """

    directory = path.parent
    temporary_path: Path | None = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=directory,
            prefix=f".{path.stem}-",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
            json.dump(payload, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise StateError(f"Could not atomically write state {path}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class WorkerRecord(BaseModel):
    """Non-secret local metadata for one wavcse-infra-created worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderKind = ProviderKind.RUNPOD
    execution_transport: ExecutionTransport = ExecutionTransport.SSH
    provider_worker_id: str = Field(min_length=1)
    infra_identity: str = Field(min_length=1)
    name: str | None = None
    requested_gpu_type: str
    actual_gpu_type: str | None = None
    requested_gpu_count: int = Field(ge=1)
    actual_gpu_count: int | None = Field(default=None, ge=0)
    requested_cloud_type: CloudType | None = None
    actual_cloud_type: CloudType | None = None
    known_hourly_price: Decimal | None = Field(default=None, ge=0)
    observed_rate_cu_per_hour: Decimal | None = Field(default=None, ge=0)
    baseline_rate_cu_per_hour: Decimal | None = Field(default=None, ge=0)
    baseline_assignments_count: int | None = Field(default=None, ge=0)
    # Colab execution mode observed at allocation; the paid CU balance selected it.
    # Absent for RunPod records and for Colab records written before this field existed.
    billing_mode: ColabBillingMode | None = None
    image: str | None = None
    template_id: str | None = None
    container_disk_gb: int | None = Field(default=None, ge=1)
    volume_gb: int | None = Field(default=None, ge=0)
    network_volume_id: str | None = None
    # The mount path of the network volume, when the Pod has one. It is provider-reported at
    # creation and is the cache root the worker derives its rebuildable artifact cache from;
    # it is `None` for Pods with no network volume, which is what disables cache use.
    network_volume_mount_path: str | None = None
    creation_timestamp: datetime
    last_observed_state: WorkerState
    last_observed_at: datetime
    ssh_host: str | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_username: str | None = None
    ssh_kind: str | None = None
    readiness_state: WorkerReadinessState = WorkerReadinessState.NOT_READY
    last_ssh_ready_at: datetime | None = None
    bootstrap_version: str | None = None
    last_bootstrap_at: datetime | None = None
    health_status: str | None = None
    last_health_check_at: datetime | None = None
    observed_gpu_models: tuple[str, ...] = ()
    observed_gpu_memory_mib: tuple[int, ...] = ()
    observed_nvidia_driver_version: str | None = None
    observed_cuda_version: str | None = None
    disk_path: str | None = None
    disk_available_bytes: int | None = Field(default=None, ge=0)
    provider_absent: bool = False
    # Written before Colab allocation, so a lost response cannot invite another allocation.
    create_pending: bool = False


class _StateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    workers: dict[str, WorkerRecord] = Field(default_factory=dict)


class _AtomicJsonDocumentStore:
    """Read and atomically replace one small JSON document under a local advisory lock.

    Every mutation replaces the whole document, so a read-decide-write transition must be
    serialized: two controller processes that both read, decide, and write would otherwise
    let the slower one overwrite the newer state. Each mutation takes one local advisory
    lock on the document, re-reads it inside the lock, and merges its change into what it
    finds there - the same single-machine concurrency model the per-job store uses. It
    serializes processes on one controller; it is not, and does not need to be, distributed
    consensus.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        default_path: Path,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = (path or default_path).expanduser()
        self._now = now or (lambda: datetime.now(UTC))

    def lock_path(self) -> Path:
        """Return the local advisory lock path for this state document."""

        return self.path.with_name(f"{self.path.name}.lock")

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold this document's local lock for one read-decide-write transition.

        Every mutating operation takes it, so a weaker observation can never be written
        over a stronger one that another controller process recorded in between: the
        transition re-reads the document inside the lock. It serializes processes on one
        controller; it is not, and does not need to be, distributed consensus.
        """

        path = self.lock_path()
        # The reentrancy key includes the thread, so a second thread of this process - not
        # just a second process - must take the real lock instead of being mistaken for a
        # nested call by the same holder.
        key = f"{path}:{threading.get_ident()}"
        depth = _LOCK_DEPTH.get(key, 0)
        if depth:
            _LOCK_DEPTH[key] = depth + 1
            try:
                yield
            finally:
                _LOCK_DEPTH[key] = depth
            return
        try:
            directory = path.parent
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise StateError(f"Could not open the local state lock {path}: {exc}") from exc
        _LOCK_DESCRIPTORS[key] = descriptor
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise StateError(f"Could not take the local state lock {path}: {exc}") from exc
            _LOCK_DEPTH[key] = 1
            yield
        finally:
            _LOCK_DEPTH.pop(key, None)
            held = _LOCK_DESCRIPTORS.pop(key, None)
            if held is not None:
                os.close(held)


class WorkerStateStore(_AtomicJsonDocumentStore):
    """Read and atomically replace the supplemental created-worker document."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(path, default_path=DEFAULT_STATE_PATH, now=now)

    def list_records(self) -> list[WorkerRecord]:
        return list(self._load().workers.values())

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self._load().workers.get(worker_id)

    @_serialized
    def record_colab_intent(self, name: str, gpu: str) -> WorkerRecord:
        """Durably claim an exact generated identity before a potentially billable allocation."""

        if re.fullmatch(r"wavcse-[0-9a-f]{12,32}", name) is None:
            raise StateError("Colab allocation requires a generated wavcse- identity")
        document = self._load()
        if any(
            record.provider is ProviderKind.COLAB and not record.provider_absent
            for record in document.workers.values()
        ):
            raise UnresolvedCreateError(
                "One infra-owned Colab lease or unresolved allocation intent already exists; "
                "release it or reconcile its exact identity before allocating another"
            )
        if name in document.workers:
            raise UnresolvedCreateError(
                f"Colab session {name} already has a local allocation intent; "
                "reconcile it with infra worker list instead of allocating again"
            )
        now = self._now()
        record = WorkerRecord(
            provider=ProviderKind.COLAB,
            execution_transport=ExecutionTransport.COLAB_EXEC,
            provider_worker_id=name,
            infra_identity=name,
            name=name,
            requested_gpu_type=gpu,
            requested_gpu_count=1,
            creation_timestamp=now,
            last_observed_state=WorkerState.PROVISIONING,
            last_observed_at=now,
            create_pending=True,
        )
        document.workers[name] = record
        self._write(document)
        return record

    @_serialized
    def record_colab_created(self, worker: Worker) -> WorkerRecord:
        """Resolve an allocation intent only from a matching provider observation."""

        document = self._load()
        existing = document.workers.get(worker.id)
        if (
            worker.provider is not ProviderKind.COLAB
            or existing is None
            or existing.provider is not ProviderKind.COLAB
            or existing.infra_identity != worker.name
        ):
            raise StateError(f"Colab session {worker.id} has no matching allocation intent")
        updated = existing.model_copy(
            update={
                "actual_gpu_type": worker.gpu_type,
                "actual_gpu_count": worker.gpu_count,
                "last_observed_state": worker.state,
                "last_observed_at": self._now(),
                "provider_absent": False,
                "create_pending": False,
            }
        )
        document.workers[worker.id] = updated
        self._write(document)
        return updated

    @_serialized
    def record_colab_ready(
        self,
        worker_id: str,
        *,
        gpu_model: str,
        rate: Decimal,
        disk_bytes: int,
        baseline_rate: Decimal | None = None,
        baseline_assignments: int | None = None,
        billing_mode: ColabBillingMode | None = None,
    ) -> WorkerRecord:
        document = self._load()
        existing = document.workers.get(worker_id)
        if (
            existing is None
            or existing.provider is not ProviderKind.COLAB
            or existing.create_pending
        ):
            raise StateError(f"Colab session {worker_id} has no confirmed owned allocation")
        now = self._now()
        updated = existing.model_copy(
            update={
                "readiness_state": WorkerReadinessState.READY,
                "health_status": "READY",
                "bootstrap_version": "colab-1",
                "last_bootstrap_at": now,
                "last_health_check_at": now,
                "observed_gpu_models": (gpu_model,),
                "disk_path": "/content",
                "disk_available_bytes": disk_bytes,
                "observed_rate_cu_per_hour": rate,
                "baseline_rate_cu_per_hour": (
                    baseline_rate
                    if baseline_rate is not None
                    else existing.baseline_rate_cu_per_hour
                ),
                "baseline_assignments_count": (
                    baseline_assignments
                    if baseline_assignments is not None
                    else existing.baseline_assignments_count
                ),
                "billing_mode": (
                    billing_mode if billing_mode is not None else existing.billing_mode
                ),
            }
        )
        document.workers[worker_id] = updated
        self._write(document)
        return updated

    @_serialized
    def mark_colab_failed(self, worker_id: str) -> WorkerRecord | None:
        """A failed Colab recheck invalidates previous readiness on ephemeral scratch."""

        document = self._load()
        existing = document.workers.get(worker_id)
        if existing is None or existing.provider is not ProviderKind.COLAB:
            return None
        updated = existing.model_copy(
            update={
                "readiness_state": WorkerReadinessState.FAILED,
                "health_status": "FAILED",
                "last_health_check_at": self._now(),
            }
        )
        document.workers[worker_id] = updated
        self._write(document)
        return updated

    @_serialized
    def record_created(self, plan: WorkerCreationPlan, worker: Worker) -> WorkerRecord:
        now = self._now()
        connection = worker.ssh_direct or worker.ssh_proxy
        # Only a mount path the provider actually reported is recorded. Falling back to the
        # requested path would make a Pod whose create answer omitted `mounts` look like it
        # has a confirmed volume there, and a later job would treat an ordinary
        # container-disk directory as the cache root.
        network_mount_path = (
            worker.volume_mount_path if plan.spec.network_volume_id is not None else None
        )
        record = WorkerRecord(
            provider_worker_id=worker.id,
            infra_identity=plan.spec.name,
            name=worker.name,
            requested_gpu_type=plan.spec.gpu_type,
            actual_gpu_type=worker.gpu_type,
            requested_gpu_count=plan.spec.gpu_count,
            actual_gpu_count=worker.gpu_count,
            requested_cloud_type=plan.spec.cloud_type,
            actual_cloud_type=worker.cloud_type,
            known_hourly_price=(worker.hourly_cost or plan.offer.total_price_per_hour),
            image=plan.spec.image,
            template_id=plan.spec.template_id,
            container_disk_gb=plan.spec.container_disk_gb,
            volume_gb=plan.spec.volume_gb,
            network_volume_id=plan.spec.network_volume_id,
            network_volume_mount_path=network_mount_path,
            creation_timestamp=worker.created_at or now,
            last_observed_state=worker.state,
            last_observed_at=now,
            ssh_host=connection.host if connection is not None else None,
            ssh_port=connection.port if connection is not None else None,
            ssh_username=connection.username if connection is not None else None,
            ssh_kind=connection.kind if connection is not None else None,
        )
        self._upsert(record)
        return record

    @_serialized
    def observe(self, worker: Worker) -> WorkerRecord | None:
        document = self._load()
        existing = document.workers.get(worker.id)
        if existing is None:
            return None
        if existing.provider != worker.provider:
            raise StateError(f"Provider mismatch for worker {worker.id}")
        connection = worker.ssh_direct or worker.ssh_proxy
        updated = existing.model_copy(
            update={
                "name": worker.name,
                "actual_gpu_type": worker.gpu_type,
                "actual_gpu_count": worker.gpu_count,
                "actual_cloud_type": worker.cloud_type,
                "known_hourly_price": _known_running_price(
                    worker.hourly_cost,
                    existing.known_hourly_price,
                ),
                "last_observed_state": worker.state,
                "last_observed_at": self._now(),
                "ssh_host": connection.host if connection is not None else existing.ssh_host,
                "ssh_port": connection.port if connection is not None else existing.ssh_port,
                "ssh_username": (
                    connection.username if connection is not None else existing.ssh_username
                ),
                "ssh_kind": connection.kind if connection is not None else existing.ssh_kind,
                "network_volume_mount_path": (
                    worker.volume_mount_path
                    if worker.volume_mount_path is not None and worker.network_volume_id is not None
                    else existing.network_volume_mount_path
                ),
                "readiness_state": (
                    existing.readiness_state
                    if worker.state is WorkerState.RUNNING
                    else WorkerReadinessState.NOT_READY
                ),
                "provider_absent": False,
            }
        )
        document.workers[worker.id] = updated
        self._write(document)
        return updated

    @_serialized
    def mark_destroyed(self, worker_id: str) -> WorkerRecord | None:
        document = self._load()
        existing = document.workers.get(worker_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "last_observed_state": WorkerState.DESTROYED,
                "last_observed_at": self._now(),
                "readiness_state": WorkerReadinessState.NOT_READY,
                "provider_absent": True,
            }
        )
        document.workers[worker_id] = updated
        self._write(document)
        return updated

    @_serialized
    def reconcile(
        self, workers: Iterable[Worker], *, provider: ProviderKind = ProviderKind.RUNPOD
    ) -> None:
        """Update only this provider's tracked records; provider reads remain authoritative."""

        document = self._load()
        provider_workers = {worker.id: worker for worker in workers}
        if not document.workers:
            return
        now = self._now()
        updated_records = dict(document.workers)
        for worker_id, existing in document.workers.items():
            if existing.provider is not provider:
                continue
            if existing.create_pending and worker_id not in provider_workers:
                # A missing read is not proof that a pending paid allocation never happened.
                continue
            if existing.create_pending:
                candidate = provider_workers[worker_id]
                if (
                    candidate.provider is not existing.provider
                    or candidate.name != existing.infra_identity
                ):
                    raise StateError(
                        f"Provider observation for pending worker {worker_id} "
                        "does not match the exact allocation identity"
                    )
            worker = provider_workers.get(worker_id)
            if worker is None:
                updated_records[worker_id] = existing.model_copy(
                    update={
                        "last_observed_state": WorkerState.DESTROYED,
                        "last_observed_at": now,
                        "readiness_state": WorkerReadinessState.NOT_READY,
                        "provider_absent": True,
                    }
                )
                continue
            connection = worker.ssh_direct or worker.ssh_proxy
            updated_records[worker_id] = existing.model_copy(
                update={
                    "name": worker.name,
                    "actual_gpu_type": worker.gpu_type,
                    "actual_gpu_count": worker.gpu_count,
                    "actual_cloud_type": worker.cloud_type,
                    "known_hourly_price": _known_running_price(
                        worker.hourly_cost,
                        existing.known_hourly_price,
                    ),
                    "last_observed_state": worker.state,
                    "last_observed_at": now,
                    "ssh_host": (connection.host if connection is not None else existing.ssh_host),
                    "ssh_port": (connection.port if connection is not None else existing.ssh_port),
                    "ssh_username": (
                        connection.username if connection is not None else existing.ssh_username
                    ),
                    "ssh_kind": connection.kind if connection is not None else existing.ssh_kind,
                    "network_volume_mount_path": (
                        worker.volume_mount_path
                        if worker.volume_mount_path is not None
                        and worker.network_volume_id is not None
                        else existing.network_volume_mount_path
                    ),
                    "readiness_state": (
                        existing.readiness_state
                        if worker.state is WorkerState.RUNNING
                        else WorkerReadinessState.NOT_READY
                    ),
                    "provider_absent": False,
                    "create_pending": False,
                }
            )
        self._write(document.model_copy(update={"workers": updated_records}))

    @_serialized
    def mark_ssh_ready(
        self,
        worker: Worker,
        connection: WorkerConnectionInfo,
    ) -> WorkerRecord | None:
        """Persist a successful SSH probe for an already tracked worker.

        A probe proves only that the endpoint answers. It must therefore never replace a
        stronger readiness that a bootstrap or health inspection already established: a
        read-only SSH operation on a READY worker leaves it READY. `last_ssh_ready_at` and
        the endpoint are always refreshed, because they are facts about this observation.
        """

        document = self._load()
        existing = document.workers.get(worker.id)
        if existing is None:
            return None
        now = self._now()
        readiness = existing.readiness_state
        if not readiness_at_least(readiness, WorkerReadinessState.SSH_READY):
            readiness = WorkerReadinessState.SSH_READY
        updated = existing.model_copy(
            update={
                "last_observed_state": worker.state,
                "last_observed_at": now,
                "ssh_host": connection.host,
                "ssh_port": connection.port,
                "ssh_username": connection.username,
                "ssh_kind": connection.kind,
                "readiness_state": readiness,
                "last_ssh_ready_at": now,
                "provider_absent": False,
            }
        )
        document.workers[worker.id] = updated
        self._write(document)
        return updated

    @_serialized
    def mark_bootstrapped(self, worker_id: str, bootstrap_version: str) -> WorkerRecord | None:
        """Persist completion of the idempotent bootstrap script."""

        document = self._load()
        existing = document.workers.get(worker_id)
        if existing is None:
            return None
        now = self._now()
        updated = existing.model_copy(
            update={
                "readiness_state": WorkerReadinessState.BOOTSTRAPPED,
                "bootstrap_version": bootstrap_version,
                "last_bootstrap_at": now,
            }
        )
        document.workers[worker_id] = updated
        self._write(document)
        return updated

    @_serialized
    def mark_gpu_healthy(self, worker_id: str) -> WorkerRecord | None:
        """Persist the successful accelerator checkpoint before final readiness.

        Like an SSH probe, this is one weaker observation. It records at least
        `GPU_HEALTHY`, and the authoritative `record_health` call that follows in the same
        health inspection still decides the final state (including a failure).
        """

        document = self._load()
        existing = document.workers.get(worker_id)
        if existing is None:
            return None
        readiness = existing.readiness_state
        if not readiness_at_least(readiness, WorkerReadinessState.GPU_HEALTHY):
            readiness = WorkerReadinessState.GPU_HEALTHY
        updated = existing.model_copy(update={"readiness_state": readiness})
        document.workers[worker_id] = updated
        self._write(document)
        return updated

    @_serialized
    def record_health(self, report: WorkerHealthReport) -> WorkerRecord | None:
        """Persist non-secret health facts without replacing provider authority."""

        document = self._load()
        existing = document.workers.get(report.provider_worker_id)
        if existing is None:
            return None
        gpu = report.gpu
        updated = existing.model_copy(
            update={
                "readiness_state": report.readiness_state,
                "bootstrap_version": report.bootstrap_version_observed,
                "health_status": "READY" if report.ready else "FAILED",
                "last_health_check_at": self._now(),
                "observed_gpu_models": gpu.models if gpu is not None else (),
                "observed_gpu_memory_mib": gpu.memory_mib if gpu is not None else (),
                "observed_nvidia_driver_version": (gpu.driver_version if gpu is not None else None),
                "observed_cuda_version": gpu.cuda_version if gpu is not None else None,
                "disk_path": report.disk_path,
                "disk_available_bytes": report.disk_available_bytes,
            }
        )
        document.workers[report.provider_worker_id] = updated
        self._write(document)
        return updated

    @_serialized
    def _upsert(self, record: WorkerRecord) -> None:
        document = self._load()
        records = dict(document.workers)
        records[record.provider_worker_id] = record
        self._write(document.model_copy(update={"workers": records}))

    def provider_for(self, worker_id: str) -> ProviderKind:
        """Route a tracked exact ID without guessing from its spelling."""

        record = self.get(worker_id)
        if record is None:
            raise StateError(
                f"Worker {worker_id} is not tracked locally; cannot infer its provider"
            )
        return record.provider

    def _load(self) -> _StateDocument:
        if not self.path.exists():
            return _StateDocument()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _StateDocument.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise StateError(f"Could not read worker state {self.path}: {exc}") from exc

    def _write(self, document: _StateDocument) -> None:
        write_json_atomically(self.path, document.model_dump(mode="json"))


class VolumeLifecycleState(StrEnum):
    """Local lifecycle of one tracked network volume.

    This is operational bookkeeping, not provider truth. `PENDING_CREATE` is the state that
    makes an ambiguous paid create recoverable; `AVAILABLE` means the provider has confirmed
    the volume; `DESTROYED` means this tool explicitly destroyed it.
    """

    PENDING_CREATE = "PENDING_CREATE"
    AVAILABLE = "AVAILABLE"
    DESTROYED = "DESTROYED"


class VolumeRecord(BaseModel):
    """Non-secret local metadata for one wavcse-infra-created network volume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "runpod"
    # The generated infra identity is both the document key and the provider-side name: the
    # provider does not require unique names, and a pending intent has no ID to key on.
    infra_identity: str = Field(min_length=1)
    provider_volume_id: str | None = None
    name: str | None = None
    requested_size_gb: int = Field(ge=1)
    requested_data_center: str = Field(min_length=1)
    requested_volume_type: VolumeType | None = None
    observed_size_gb: int | None = Field(default=None, ge=0)
    observed_data_center: str | None = None
    observed_volume_type: VolumeType | None = None
    creation_timestamp: datetime
    last_observed_at: datetime
    lifecycle_state: VolumeLifecycleState
    provider_absent: bool = False


class _VolumeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    volumes: dict[str, VolumeRecord] = Field(default_factory=dict)


class VolumeStateStore(_AtomicJsonDocumentStore):
    """Read and atomically replace the supplemental network-volume document.

    Records are keyed by infra identity rather than by provider ID, because the reason this
    document exists is an ambiguous create: the identity is known before the paid request is
    issued, while the provider ID is only known after a response arrives. Reconcile therefore
    matches the provider's own list against the identity, and never trusts the local copy as
    authority about what exists.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(path, default_path=DEFAULT_VOLUME_STATE_PATH, now=now)

    def list_records(self) -> list[VolumeRecord]:
        return list(self._load().volumes.values())

    def get(self, provider_volume_id: str) -> VolumeRecord | None:
        """Return the tracked record for one exact provider volume ID."""

        for record in self._load().volumes.values():
            if record.provider_volume_id == provider_volume_id:
                return record
        return None

    def get_by_identity(self, infra_identity: str) -> VolumeRecord | None:
        return self._load().volumes.get(infra_identity)

    def unresolved_intents(self) -> list[VolumeRecord]:
        """Return every paid create whose outcome the provider has not confirmed.

        A pending record with no provider ID is the visible form of an ambiguous create: the
        request was issued, and whether a billable volume exists is not yet known. Callers
        must treat it as a reason to reconcile rather than as a reason to try again.
        """

        return [
            record
            for record in self._load().volumes.values()
            if record.lifecycle_state is VolumeLifecycleState.PENDING_CREATE
            and record.provider_volume_id is None
        ]

    @_serialized
    def forget(self, infra_identity: str) -> VolumeRecord | None:
        """Remove one local record without contacting the provider.

        This deletes bookkeeping, never a resource. An untracked provider volume is still
        listed by `infra volume list`; what is lost is the local link that would have let a
        later reconciliation adopt it, which is why it is a separate, deliberate action
        rather than something a create does implicitly.
        """

        document = self._load()
        existing = document.volumes.get(infra_identity)
        if existing is None:
            return None
        volumes = {key: record for key, record in document.volumes.items() if key != infra_identity}
        self._write(document.model_copy(update={"volumes": volumes}))
        return existing

    @_serialized
    def record_create_intent(self, spec: NetworkVolumeSpec) -> VolumeRecord:
        """Persist the exact request before the paid create is issued.

        This is the durable half of duplicate-create safety. If the create response is lost
        and bounded reconciliation finds nothing, the identity and the requested placement
        still exist locally, so a later `infra volume list` can match the provider's own
        listing against it instead of the operator guessing whether the volume exists.
        """

        document = self._load()
        existing = document.volumes.get(spec.name)
        if existing is not None:
            raise StateError(
                f"A network volume record already exists for infra identity {spec.name!r} "
                f"in state {existing.lifecycle_state.value}; reconcile or destroy it before "
                "creating another"
            )
        # This guard must be evaluated inside the same locked transition that writes the
        # intent, not merely before the confirmation prompt: two overlapping
        # `infra volume create` invocations would otherwise both observe no unresolved
        # intent, both reach the provider, and each open a billable volume whose identity is
        # unrelated to the other's intent.
        unresolved_elsewhere = [
            record
            for record in document.volumes.values()
            if record.lifecycle_state is VolumeLifecycleState.PENDING_CREATE
            and record.provider_volume_id is None
        ]
        if unresolved_elsewhere:
            identities = ", ".join(sorted(record.infra_identity for record in unresolved_elsewhere))
            raise UnresolvedCreateError(
                "Refusing to record another network volume create intent while an earlier "
                f"create is unresolved: {identities}. Run `infra volume list` to reconcile it "
                "against the provider, or `infra volume forget <infra-identity>` if the "
                "provider really has no such volume, before creating another"
            )
        now = self._now()
        record = VolumeRecord(
            infra_identity=spec.name,
            requested_size_gb=spec.size_gb,
            requested_data_center=spec.datacenter,
            requested_volume_type=spec.volume_type,
            creation_timestamp=now,
            last_observed_at=now,
            lifecycle_state=VolumeLifecycleState.PENDING_CREATE,
        )
        volumes = dict(document.volumes)
        volumes[record.infra_identity] = record
        self._write(document.model_copy(update={"volumes": volumes}))
        return record

    @_serialized
    def record_created(self, record: VolumeRecord, volume: NetworkVolume) -> VolumeRecord:
        """Attach the provider's answer to the intent that produced it."""

        document = self._load()
        existing = document.volumes.get(record.infra_identity) or record
        updated = _observed_volume(existing, volume, now=self._now()).model_copy(
            update={"lifecycle_state": VolumeLifecycleState.AVAILABLE}
        )
        volumes = dict(document.volumes)
        volumes[updated.infra_identity] = updated
        self._write(document.model_copy(update={"volumes": volumes}))
        return updated

    @_serialized
    def observe(self, volume: NetworkVolume) -> VolumeRecord | None:
        """Refresh one tracked record from a provider read, matched by exact identity."""

        document = self._load()
        identity = _identity_for_volume(document.volumes.values(), volume)
        if identity is None:
            return None
        existing = document.volumes[identity]
        # A provider read is confirmation that the volume exists, so a pending intent stops
        # being pending. Only that promotion is applied: a record this tool believes it
        # destroyed is not silently revived by an observation, which would hide a deletion
        # the provider has not caught up with.
        promotion = (
            {"lifecycle_state": VolumeLifecycleState.AVAILABLE}
            if existing.lifecycle_state is VolumeLifecycleState.PENDING_CREATE
            else {}
        )
        updated = _observed_volume(existing, volume, now=self._now()).model_copy(update=promotion)
        volumes = dict(document.volumes)
        volumes[identity] = updated
        self._write(document.model_copy(update={"volumes": volumes}))
        return updated

    @_serialized
    def reconcile(self, volumes: Iterable[NetworkVolume]) -> list[VolumeRecord]:
        """Merge local records with the provider's authoritative list.

        A pending intent whose exact identity now exists is adopted. A tracked volume the
        provider no longer lists is marked absent without being deleted locally, so the
        history of what this tool created survives an out-of-band deletion. An identity
        matching more than one provider volume is left untouched: the provider does not
        require unique names, and adopting one of several would be a guess.
        """

        document = self._load()
        if not document.volumes:
            return []
        provider_volumes = list(volumes)
        now = self._now()
        updated_records: dict[str, VolumeRecord] = {}
        for identity, existing in document.volumes.items():
            # Match by the name this tool requested, and also by the exact provider ID this
            # record already linked. A provider response that omits the name, or reports a
            # name that differs from the requested one, must not be read as "absent".
            matches = [
                volume
                for volume in provider_volumes
                if volume.name == identity
                or (
                    existing.provider_volume_id is not None
                    and volume.id == existing.provider_volume_id
                )
            ]
            if len(matches) > 1:
                updated_records[identity] = existing
                continue
            if len(matches) == 1:
                updated_records[identity] = _observed_volume(
                    existing,
                    matches[0],
                    now=now,
                ).model_copy(
                    update={
                        "lifecycle_state": VolumeLifecycleState.AVAILABLE,
                        "provider_absent": False,
                    }
                )
                continue
            if existing.provider_volume_id is None:
                # A pending intent with no provider match yet: nothing to conclude.
                updated_records[identity] = existing
                continue
            updated_records[identity] = existing.model_copy(
                update={
                    "last_observed_at": now,
                    "provider_absent": True,
                }
            )
        self._write(document.model_copy(update={"volumes": updated_records}))
        return list(updated_records.values())

    @_serialized
    def mark_destroyed(self, provider_volume_id: str) -> VolumeRecord | None:
        """Record explicit destruction of one exact provider volume.

        It never touches worker records: volume and Pod lifecycle stay independent, so
        destroying a volume cannot imply destroying the compute that mounted it.
        """

        document = self._load()
        identity = next(
            (
                key
                for key, record in document.volumes.items()
                if record.provider_volume_id == provider_volume_id
            ),
            None,
        )
        if identity is None:
            return None
        updated = document.volumes[identity].model_copy(
            update={
                "lifecycle_state": VolumeLifecycleState.DESTROYED,
                "last_observed_at": self._now(),
                "provider_absent": True,
            }
        )
        volumes = dict(document.volumes)
        volumes[identity] = updated
        self._write(document.model_copy(update={"volumes": volumes}))
        return updated

    def _load(self) -> _VolumeDocument:
        if not self.path.exists():
            return _VolumeDocument()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _VolumeDocument.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise StateError(f"Could not read network volume state {self.path}: {exc}") from exc

    def _write(self, document: _VolumeDocument) -> None:
        write_json_atomically(self.path, document.model_dump(mode="json"))


def _identity_for_volume(
    records: Iterable[VolumeRecord],
    volume: NetworkVolume,
) -> str | None:
    """Return the infra identity of the record that describes one provider volume."""

    if volume.name is not None:
        for record in records:
            if record.infra_identity == volume.name:
                return record.infra_identity
    for record in records:
        if record.provider_volume_id == volume.id:
            return record.infra_identity
    return None


def _observed_volume(
    existing: VolumeRecord,
    volume: NetworkVolume,
    *,
    now: datetime,
) -> VolumeRecord:
    return existing.model_copy(
        update={
            "provider_volume_id": volume.id,
            "name": volume.name,
            "observed_size_gb": volume.size_gb,
            "observed_data_center": volume.datacenter,
            "observed_volume_type": volume.volume_type,
            "last_observed_at": now,
            "provider_absent": False,
        }
    )


def _known_running_price(
    observed_price: Decimal | None,
    existing_price: Decimal | None,
) -> Decimal | None:
    if observed_price is not None and observed_price > 0:
        return observed_price
    return existing_price
