"""Reusable offline doubles for recorded-job tests.

These helpers never touch a network, a provider API, or the real S3 service. They exist
so the job specification, worker runner, controller executor, submitter, coordinator, and
CLI can each be exercised against deterministic evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.errors import SshCommandError
from wavcse_infra.jobs.context import JobContext
from wavcse_infra.jobs.execution import CancelResult, PrepareResult, RemoteJobStatus, StartResult
from wavcse_infra.jobs.state import JobStateStore
from wavcse_infra.models import (
    CloudType,
    Worker,
    WorkerConnectionInfo,
    WorkerReadinessState,
    WorkerState,
)
from wavcse_infra.state import WorkerRecord, WorkerStateStore
from wavcse_infra.storage.s3 import StoredObject
from wavcse_infra.workers.ssh import SshCommandResult, SshWaitResult

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
WORKER_ID = "pod-123"
CONNECTION = WorkerConnectionInfo(
    provider_worker_id=WORKER_ID,
    kind="direct",
    host="198.51.100.7",
    port=10341,
    username="root",
)


def runner_output(**rows: str) -> str:
    """Render the worker runner's tab-separated protocol for a scripted response."""

    lines = ["wavcse_job_schema\t1"]
    lines.extend(f"{key}\t{value}" for key, value in rows.items())
    return "\n".join(lines) + "\n"


class FakeWaiter:
    """Stand-in for WorkerSshWaiter that never opens a connection."""

    def __init__(self, connection: WorkerConnectionInfo = CONNECTION) -> None:
        self.connection = connection
        self.calls: list[str] = []

    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> SshWaitResult:
        self.calls.append(worker_id)
        return SshWaitResult(
            worker=worker(provider_worker_id=worker_id),
            connection=self.connection,
        )


class RecordingExecutor:
    """Record every remote argv/stdin pair and replay scripted responses."""

    def __init__(
        self,
        *,
        responses: Mapping[str, tuple[int, str, str]] | None = None,
        default: tuple[int, str, str] | None = None,
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []

    def install(self, key: str, stdout: str) -> None:
        self.responses[key] = (0, stdout, "")

    def run(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        argv = tuple(remote_argv)
        self.calls.append((argv, input_text, timeout_seconds))
        key = argv[-1] if argv else ""
        if key not in self.responses and "-c" in argv:
            key = "-c"
        if key in self.responses:
            exit_code, stdout, stderr = self.responses[key]
        elif self.default is not None:
            exit_code, stdout, stderr = self.default
        else:
            raise AssertionError(f"unscripted remote command: {argv}")
        assert connection is CONNECTION or connection.provider_worker_id
        return SshCommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        result = self.run(
            connection,
            remote_argv,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )
        if result.exit_code != 0:
            raise SshCommandError(
                f"Remote command on worker {connection.provider_worker_id} exited "
                f"{result.exit_code}: {result.stderr.strip()}"
            )
        return result


def prepare_output(commit: str, job_directory: str) -> str:
    return runner_output(
        executed_commit=commit,
        job_directory=job_directory,
        source_directory=f"{job_directory}/source",
    )


def start_output(
    pid: int = 4321,
    started_at: str = "2026-09-28T12:00:00Z",
    commit: str = "a" * 40,
) -> str:
    return runner_output(pid=pid, started_at=started_at, executed_commit=commit)


def inspect_output(
    status: str = "finished",
    *,
    pid: int | None = 4321,
    exit_code: int | None = 0,
    stage: str = "command",
    timed_out: bool = False,
    cancelled: bool = False,
    started_at: str = "2026-09-28T12:00:00Z",
    finished_at: str = "2026-09-28T12:05:00Z",
    commit: str = "a" * 40,
    log_bytes: int = 128,
) -> str:
    return runner_output(
        status=status,
        pid="" if pid is None else pid,
        exit_code="" if exit_code is None else exit_code,
        stage=stage,
        timed_out="true" if timed_out else "false",
        cancelled="true" if cancelled else "false",
        started_at=started_at,
        finished_at=finished_at,
        executed_commit=commit,
        log_bytes=log_bytes,
    )


def cancel_output(
    *,
    cancelled: bool = True,
    already_finished: bool = False,
    pid: int | None = 4321,
    cancelled_at: str = "2026-09-28T12:01:00Z",
    exit_code: int | None = None,
) -> str:
    return runner_output(
        cancelled="true" if cancelled else "false",
        already_finished="true" if already_finished else "false",
        pid="" if pid is None else pid,
        cancelled_at=cancelled_at,
        exit_code="" if exit_code is None else exit_code,
    )


class FakeJobExecutor:
    """Scripted stand-in for JobExecutor used by submit and coordinator tests."""

    def __init__(self, *, commit: str = "a" * 40) -> None:
        self.commit = commit
        self.calls: list[str] = []
        self.install_calls: list[str] = []
        self.prepare_calls: list[dict[str, Any]] = []
        self.start_calls: list[dict[str, Any]] = []
        self.inspect_calls: list[tuple[str, str]] = []
        self.log_calls: list[int | None] = []
        self.cancel_calls: list[str] = []
        self.install_error: Exception | None = None
        self.prepare_error: Exception | None = None
        self.start_error: Exception | None = None
        self.inspect_error: Exception | None = None
        self.log_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.status = RemoteJobStatus(status="running", pid=4321, log_bytes=10)
        self.report_missing_commit = False
        self.log_text = "job output\n"
        self.cancel_result = CancelResult(cancelled=True, already_finished=False, pid=4321)

    def install_runner(self, worker_id: str) -> str:
        self.calls.append("install")
        if self.install_error is not None:
            raise self.install_error
        self.install_calls.append(worker_id)
        return "installed"

    def prepare(self, worker_id: str, **kwargs: Any) -> PrepareResult:
        self.calls.append("prepare")
        if self.prepare_error is not None:
            raise self.prepare_error
        self.prepare_calls.append({"worker_id": worker_id, **kwargs})
        return PrepareResult(
            executed_commit=self.commit,
            job_directory=kwargs["job_directory"],
            source_directory=f"{kwargs['job_directory']}/source",
        )

    def start(self, worker_id: str, **kwargs: Any) -> StartResult:
        self.calls.append("start")
        if self.start_error is not None:
            raise self.start_error
        self.start_calls.append({"worker_id": worker_id, **kwargs})
        return StartResult(pid=4321, started_at=NOW, executed_commit=self.commit)

    def inspect(self, worker_id: str, *, job_id: str, job_directory: str) -> RemoteJobStatus:
        self.calls.append("inspect")
        self.inspect_calls.append((job_id, job_directory))
        if self.inspect_error is not None:
            raise self.inspect_error
        if (
            self.status.status == "finished"
            and self.status.executed_commit is None
            and not self.report_missing_commit
        ):
            return self.status.model_copy(update={"executed_commit": self.commit})
        return self.status

    def logs(self, worker_id: str, *, job_id: str, job_directory: str, tail_bytes: int) -> str:
        self.calls.append("logs")
        self.log_calls.append(tail_bytes)
        if self.log_error is not None:
            raise self.log_error
        return self.log_text

    def cancel(self, worker_id: str, *, job_id: str, job_directory: str) -> CancelResult:
        self.calls.append("cancel")
        self.cancel_calls.append(job_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_result


class FakeTransfer:
    """Record artifact transfers without contacting S3 or a worker.

    With `materialize=True` a download also writes a file of the expected size at the
    destination, so a real worker-side command can read what it declared as an input.
    """

    def __init__(self, *, materialize: bool = False) -> None:
        self.materialize = materialize
        self.downloads: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.download_error: Exception | None = None
        self.upload_error: Exception | None = None
        self.download_digest = "b" * 64
        self.upload_digest = "c" * 64

    def download(self, worker_id: str, **kwargs: Any) -> Any:
        self.downloads.append({"worker_id": worker_id, **kwargs})
        if self.download_error is not None:
            raise self.download_error
        size = kwargs.get("expected_size") or 11
        if self.materialize:
            destination = Path(kwargs["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x" * size)
        return _TransferResult(
            operation="download",
            path=kwargs["destination"],
            size_bytes=size,
            sha256=self.download_digest,
        )

    def upload(self, worker_id: str, **kwargs: Any) -> Any:
        self.uploads.append({"worker_id": worker_id, **kwargs})
        if self.upload_error is not None:
            raise self.upload_error
        size = 7
        return _UploadOutcome(
            result=_TransferResult(
                operation="upload",
                path=kwargs["source"],
                size_bytes=size,
                sha256=self.upload_digest,
            ),
            verification=_Verification(key=kwargs["key"], size_bytes=size),
        )


class _TransferResult:
    def __init__(self, *, operation: str, path: str, size_bytes: int, sha256: str) -> None:
        self.operation = operation
        self.path = path
        self.size_bytes = size_bytes
        self.sha256 = sha256

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


class _Verification:
    def __init__(self, *, key: str, size_bytes: int) -> None:
        self.key = key
        self.size_bytes = size_bytes


class _UploadOutcome:
    def __init__(self, *, result: Any, verification: Any) -> None:
        self.result = result
        self.verification = verification


class FakeStorage:
    """Minimal canonical-storage double for declared inputs."""

    bucket = "wavcse-test-bucket"
    prefix = "wavcse"

    def __init__(self) -> None:
        self.objects: dict[str, int] = {}
        self.manifests: dict[str, Any] = {}

    def object_key(self, key: str) -> str:
        return f"{self.prefix}/{key}"

    def object_metadata(self, key: str) -> StoredObject | None:
        size = self.objects.get(key)
        if size is None:
            return None
        return StoredObject(key=self.object_key(key), size_bytes=size)

    def read_manifest(self, key: str) -> Any:
        return self.manifests[key]


class FakeProvider:
    """Provider double that returns one scripted worker."""

    def __init__(self, subject: Worker | None = None) -> None:
        self.subject = subject if subject is not None else worker()
        self.error: Exception | None = None
        self.calls: list[str] = []

    def get_worker(self, worker_id: str) -> Worker:
        self.calls.append(worker_id)
        if self.error is not None:
            raise self.error
        return self.subject


def worker(
    *,
    state: WorkerState = WorkerState.RUNNING,
    provider_worker_id: str = WORKER_ID,
    gpu_type: str = "NVIDIA RTX A5000",
    gpu_count: int | None = 1,
) -> Worker:
    return Worker(
        id=provider_worker_id,
        name="wavcse-training-abc123",
        state=state,
        native_status="RUNNING" if state is WorkerState.RUNNING else "EXITED",
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        cloud_type=CloudType.COMMUNITY,
        hourly_cost=Decimal("0.16") if state is WorkerState.RUNNING else Decimal("0"),
        ssh_port=10341,
        ssh_direct=CONNECTION,
    )


def worker_record(
    *,
    provider_worker_id: str = WORKER_ID,
) -> WorkerRecord:
    return WorkerRecord(
        provider_worker_id=provider_worker_id,
        infra_identity="wavcse-training-abc123",
        name="wavcse-training-abc123",
        requested_gpu_type="NVIDIA RTX A5000",
        actual_gpu_type="NVIDIA RTX A5000",
        requested_gpu_count=1,
        actual_gpu_count=1,
        requested_cloud_type=CloudType.COMMUNITY,
        actual_cloud_type=CloudType.COMMUNITY,
        known_hourly_price=Decimal("0.16"),
        image="runpod/pytorch:example",
        container_disk_gb=20,
        volume_gb=0,
        creation_timestamp=NOW,
        last_observed_state=WorkerState.RUNNING,
        last_observed_at=NOW,
        ssh_host=CONNECTION.host,
        ssh_port=CONNECTION.port,
        ssh_username=CONNECTION.username,
        ssh_kind=CONNECTION.kind,
        readiness_state=WorkerReadinessState.READY,
        bootstrap_version="1",
        observed_gpu_models=("NVIDIA RTX A5000",),
        observed_gpu_memory_mib=(24564,),
    )


def ready_worker_store(tmp_path: Path, record: WorkerRecord | None = None) -> WorkerStateStore:
    """Write a tracked READY worker document directly, as `infra worker health` would."""

    path = tmp_path / "state" / "workers.json"
    subject = record if record is not None else worker_record()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "workers": {subject.provider_worker_id: subject.model_dump(mode="json")},
            }
        ),
        encoding="utf-8",
    )
    return WorkerStateStore(path, now=lambda: NOW)


UNSET: Any = object()


def job_context(
    tmp_path: Path,
    *,
    provider: Any | None = None,
    executor: Any | None = None,
    transfer: Any | None = None,
    storage: Any = UNSET,
    environ: Mapping[str, str] | None = None,
    worker_store: WorkerStateStore | None = None,
    jobs_config: JobsConfig | None = None,
) -> JobContext:
    return JobContext(
        provider=provider if provider is not None else FakeProvider(),
        worker_state=worker_store if worker_store is not None else ready_worker_store(tmp_path),
        job_store=JobStateStore(tmp_path / "jobs", now=lambda: NOW),
        executor=executor if executor is not None else FakeJobExecutor(),
        transfer=transfer if transfer is not None else FakeTransfer(),
        storage=FakeStorage() if storage is UNSET else storage,
        jobs_config=jobs_config if jobs_config is not None else JobsConfig(),
        environ=dict(environ or {}),
        now=lambda: NOW,
    )


def job_spec_document(**overrides: Any) -> dict[str, Any]:
    """Return a valid version 1 job specification document with optional overrides."""

    document: dict[str, Any] = {
        "schema_version": 1,
        "name": "dg-0004-seed-42",
        "source": {
            "repository": "https://github.com/Synergy-io/wavCSE.git",
            "commit": "a" * 40,
        },
        "command": {"argv": ["uv", "run", "python", "train.py", "--seed", "42"]},
    }
    document.update(overrides)
    return document


def write_spec(path: Path, document: Mapping[str, Any] | None = None) -> Path:
    payload = dict(document if document is not None else job_spec_document())
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


SSH_CONFIG = SshConfig()
