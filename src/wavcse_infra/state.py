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
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wavcse_infra.errors import StateError
from wavcse_infra.models import (
    CloudType,
    Worker,
    WorkerConnectionInfo,
    WorkerCreationPlan,
    WorkerHealthReport,
    WorkerReadinessState,
    WorkerState,
    readiness_at_least,
)

DEFAULT_STATE_PATH = Path("~/.local/state/wavcse-infra/workers.json")

# The document lock is held for one read-decide-write transition, and one transition may
# call another (a create that upserts), so the descriptor is cached and its depth counted
# instead of taking a second flock on a second descriptor, which would block against itself.
_LOCK_DEPTH: dict[str, int] = {}
_LOCK_DESCRIPTORS: dict[str, int] = {}


def _serialized(method: Any) -> Any:
    """Run one worker-state mutation while holding the document's local lock."""

    @wraps(method)
    def wrapper(self: WorkerStateStore, *args: Any, **kwargs: Any) -> Any:
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

    provider: str = "runpod"
    provider_worker_id: str = Field(min_length=1)
    infra_identity: str = Field(min_length=1)
    name: str | None = None
    requested_gpu_type: str
    actual_gpu_type: str | None = None
    requested_gpu_count: int = Field(ge=1)
    actual_gpu_count: int | None = Field(default=None, ge=0)
    requested_cloud_type: CloudType
    actual_cloud_type: CloudType | None = None
    known_hourly_price: Decimal | None = Field(default=None, ge=0)
    image: str | None = None
    template_id: str | None = None
    container_disk_gb: int = Field(ge=1)
    volume_gb: int = Field(ge=0)
    network_volume_id: str | None = None
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


class _StateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = 1
    workers: dict[str, WorkerRecord] = Field(default_factory=dict)


class WorkerStateStore:
    """Read and atomically replace a small JSON worker-state document."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = (path or DEFAULT_STATE_PATH).expanduser()
        self._now = now or (lambda: datetime.now(UTC))

    def list_records(self) -> list[WorkerRecord]:
        return list(self._load().workers.values())

    def get(self, worker_id: str) -> WorkerRecord | None:
        return self._load().workers.get(worker_id)

    def lock_path(self) -> Path:
        """Return the local advisory lock path for the worker-state document."""

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
            raise StateError(f"Could not open the local worker-state lock {path}: {exc}") from exc
        _LOCK_DESCRIPTORS[key] = descriptor
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise StateError(
                    f"Could not take the local worker-state lock {path}: {exc}"
                ) from exc
            _LOCK_DEPTH[key] = 1
            yield
        finally:
            _LOCK_DEPTH.pop(key, None)
            held = _LOCK_DESCRIPTORS.pop(key, None)
            if held is not None:
                os.close(held)

    @_serialized
    def record_created(self, plan: WorkerCreationPlan, worker: Worker) -> WorkerRecord:
        now = self._now()
        connection = worker.ssh_direct or worker.ssh_proxy
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
    def reconcile(self, workers: Iterable[Worker]) -> None:
        """Update only tracked records; provider reads remain authoritative."""

        document = self._load()
        provider_workers = {worker.id: worker for worker in workers}
        if not document.workers:
            return
        now = self._now()
        updated_records = dict(document.workers)
        for worker_id, existing in document.workers.items():
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
                    "readiness_state": (
                        existing.readiness_state
                        if worker.state is WorkerState.RUNNING
                        else WorkerReadinessState.NOT_READY
                    ),
                    "provider_absent": False,
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


def _known_running_price(
    observed_price: Decimal | None,
    existing_price: Decimal | None,
) -> Decimal | None:
    if observed_price is not None and observed_price > 0:
        return observed_price
    return existing_price
