"""Atomic supplemental controller state for workers created by this project."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wavcse_infra.errors import StateError
from wavcse_infra.models import CloudType, Worker, WorkerCreationPlan, WorkerState

DEFAULT_STATE_PATH = Path("~/.local/state/wavcse-infra/workers.json")


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
        )
        self._upsert(record)
        return record

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
                "provider_absent": False,
            }
        )
        document.workers[worker.id] = updated
        self._write(document)
        return updated

    def mark_destroyed(self, worker_id: str) -> WorkerRecord | None:
        document = self._load()
        existing = document.workers.get(worker_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "last_observed_state": WorkerState.DESTROYED,
                "last_observed_at": self._now(),
                "provider_absent": True,
            }
        )
        document.workers[worker_id] = updated
        self._write(document)
        return updated

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
                    "provider_absent": False,
                }
            )
        self._write(document.model_copy(update={"workers": updated_records}))

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
        directory = self.path.parent
        temporary_path: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=directory,
                prefix=".workers-",
                suffix=".tmp",
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(
                    document.model_dump(mode="json"),
                    state_file,
                    indent=2,
                    sort_keys=True,
                )
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as exc:
            raise StateError(f"Could not atomically write worker state {self.path}: {exc}") from exc
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _known_running_price(
    observed_price: Decimal | None,
    existing_price: Decimal | None,
) -> Decimal | None:
    if observed_price is not None and observed_price > 0:
        return observed_price
    return existing_price
