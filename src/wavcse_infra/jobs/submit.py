"""Validated, exact-commit job submission against one explicit READY worker."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from wavcse_infra import __version__
from wavcse_infra.errors import (
    InfraError,
    JobPreconditionError,
    StorageObjectNotFoundError,
    StorageVerificationError,
)
from wavcse_infra.jobs.collect import failed_output_record, resolve_input_destination
from wavcse_infra.jobs.context import JobContext, require_ready_worker, require_storage
from wavcse_infra.jobs.models import (
    JobInput,
    JobInputRecord,
    JobProvenance,
    JobRecord,
    JobSpec,
    JobState,
)
from wavcse_infra.jobs.state import JobStateStore
from wavcse_infra.models import Worker
from wavcse_infra.state import WorkerRecord


def new_job_id() -> str:
    """Return a unique local job identifier independent of worker, commit, and MLflow."""

    return f"job-{uuid4().hex[:16]}"


def resolve_secrets(spec: JobSpec, environ: Mapping[str, str]) -> dict[str, str]:
    """Resolve declared secret values from the controller environment, never from a file."""

    secrets: dict[str, str] = {}
    missing: list[str] = []
    for name in spec.runtime.environment_secrets:
        value = environ.get(name)
        if not value:
            missing.append(name)
        else:
            secrets[name] = value
    if missing:
        raise JobPreconditionError(
            "these secret environment variables are required by the job specification but "
            "are not set on this controller: "
            + ", ".join(sorted(missing))
            + "; export them in this shell before submitting (values are never written to the "
            "job specification, local state, or logs)"
        )
    return secrets


def infra_environment(
    worker: Worker,
    record: WorkerRecord,
    *,
    job_id: str,
    commit: str,
) -> dict[str, str]:
    """Build the non-secret provenance environment described by the specification."""

    gpu = ", ".join(record.observed_gpu_models) or (worker.gpu_type or "")
    values = {
        "INFRA_PROVIDER": worker.provider,
        "INFRA_WORKER_ID": worker.id,
        "INFRA_JOB_ID": job_id,
        "INFRA_GIT_COMMIT": commit,
    }
    if gpu:
        values["INFRA_GPU"] = gpu
    if worker.gpu_count is not None:
        values["INFRA_GPU_COUNT"] = str(worker.gpu_count)
    if worker.name:
        values["INFRA_WORKER_NAME"] = worker.name
    return values


class JobSubmitter:
    """Materialize inputs and source, then start one job detached on an explicit worker."""

    def __init__(self, context: JobContext) -> None:
        self._context = context

    def submit(self, spec: JobSpec, *, worker_id: str) -> JobRecord:
        """Submit one recorded job, persisting FAILED with evidence on any failure."""

        context = self._context
        store: JobStateStore = context.job_store
        # Fail before recording anything when the controller lacks a declared secret.
        secrets = resolve_secrets(spec, context.environ)
        worker, worker_record = require_ready_worker(context, worker_id)

        job_id = new_job_id()
        job_directory = f"{context.jobs_config.worker_root}/{job_id}"
        record = JobRecord(
            job_id=job_id,
            name=spec.name,
            spec=spec,
            state=JobState.PENDING,
            state_reason="job recorded locally before any worker contact",
            worker_id=worker.id,
            job_directory=job_directory,
            log_path=f"{job_directory}/logs/job.log",
            requested_commit=spec.source.commit,
            created_at=context.now(),
            updated_at=context.now(),
            provenance=JobProvenance(
                provider=worker.provider,
                worker_id=worker.id,
                worker_name=worker.name,
                worker_bootstrap_version=worker_record.bootstrap_version,
                gpu_models=worker_record.observed_gpu_models,
                gpu_count=worker.gpu_count,
                known_hourly_price=worker_record.known_hourly_price,
                infra_version=__version__,
            ),
            inputs=tuple(
                JobInputRecord(
                    artifact=job_input.artifact,
                    destination=job_input.destination,
                    required=job_input.required,
                )
                for job_input in spec.inputs
            ),
            outputs=tuple(
                failed_output_record(output, "not persisted yet") for output in spec.outputs
            ),
        )
        store.create(record)
        try:
            record = store.transition(
                record,
                JobState.PREPARING,
                reason="installing the reviewed runner and materializing declared inputs",
            )
            context.executor.install_runner(worker.id)
            prepared = context.executor.prepare(
                worker.id,
                job_id=job_id,
                job_directory=job_directory,
                repository=spec.source.repository,
                commit=spec.source.commit,
                name=spec.name,
                input_destinations=tuple(job_input.destination for job_input in spec.inputs),
            )
            record = store.save(
                record.model_copy(update={"executed_commit": prepared.executed_commit})
            )
            record = self._materialize_inputs(record)
            started = context.executor.start(
                worker.id,
                job_id=job_id,
                job_directory=job_directory,
                repository=spec.source.repository,
                commit=prepared.executed_commit,
                argv=list(spec.command.argv),
                setup_argv=list(spec.setup.argv) if spec.setup is not None else None,
                working_directory=spec.command.working_directory,
                environment=dict(spec.runtime.environment),
                secrets=secrets,
                timeout_seconds=(
                    spec.runtime.timeout_seconds
                    if spec.runtime.timeout_seconds is not None
                    else context.jobs_config.default_timeout_seconds
                ),
                infra_environment=infra_environment(
                    worker,
                    worker_record,
                    job_id=job_id,
                    commit=prepared.executed_commit,
                ),
            )
            if started.executed_commit != spec.source.commit:
                raise JobPreconditionError(
                    f"worker {worker.id} acknowledged commit {started.executed_commit}, but "
                    f"job {job_id} requested {spec.source.commit}; refusing this start"
                )
        except InfraError as exc:
            self._fail(record, exc)
            raise
        return store.transition(
            record,
            JobState.RUNNING,
            reason="job command started on the worker",
            pid=started.pid,
            started_at=started.started_at,
            executed_commit=started.executed_commit,
            remote_status="running",
        )

    def _materialize_inputs(self, record: JobRecord) -> JobRecord:
        """Download every declared input, refusing to run when a required one fails."""

        context = self._context
        store: JobStateStore = context.job_store
        records: list[JobInputRecord] = []
        for job_input, existing in zip(record.spec.inputs, record.inputs, strict=True):
            try:
                updated = self._materialize_input(record, job_input, existing)
            except InfraError as exc:
                updated = existing.model_copy(
                    update={
                        "failure_reason": str(exc)[:1000],
                        "materialized": False,
                    }
                )
                records.append(updated)
                record = store.save(record.model_copy(update={"inputs": tuple(records)}))
                if job_input.required:
                    raise JobPreconditionError(
                        f"required input {job_input.artifact!r} could not be materialized: {exc}"
                    ) from exc
                continue
            records.append(updated)
            record = store.save(record.model_copy(update={"inputs": tuple(records)}))
        return record

    def _materialize_input(
        self,
        record: JobRecord,
        job_input: JobInput,
        existing: JobInputRecord,
    ) -> JobInputRecord:
        """Download one input with the strongest available size and checksum expectation."""

        context = self._context
        storage = require_storage(context)
        expected_size, expected_sha256 = self._expectations(job_input)
        destination = resolve_input_destination(record.job_directory, job_input)
        result = context.transfer.download(
            record.worker_id,
            storage=storage,
            key=job_input.artifact,
            destination=destination,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            overwrite=False,
        )
        return existing.model_copy(
            update={
                "worker_path": destination,
                "materialized": True,
                "size_bytes": result.size_bytes,
                "sha256": result.sha256,
                "failure_reason": None,
            }
        )

    def _expectations(self, job_input: JobInput) -> tuple[int | None, str | None]:
        """Resolve the size and digest an input must satisfy before the job may start."""

        context = self._context
        storage = require_storage(context)
        stored = storage.object_metadata(job_input.artifact)
        if stored is None:
            raise StorageObjectNotFoundError(
                f"s3://{storage.bucket}/{storage.object_key(job_input.artifact)} "
                "does not exist, so this declared input cannot be materialized"
            )
        if job_input.manifest is not None:
            manifest = storage.read_manifest(job_input.manifest)
            if manifest.object_key != job_input.artifact:
                raise StorageVerificationError(
                    f"manifest {job_input.manifest!r} describes {manifest.object_key!r}, not "
                    f"the declared input {job_input.artifact!r}"
                )
            if manifest.size_bytes != stored.size_bytes:
                raise StorageVerificationError(
                    f"manifest {job_input.manifest!r} records {manifest.size_bytes} bytes but "
                    f"the stored object is {stored.size_bytes} bytes"
                )
            return manifest.size_bytes, manifest.sha256
        size = job_input.size_bytes if job_input.size_bytes is not None else stored.size_bytes
        if job_input.size_bytes is not None and job_input.size_bytes != stored.size_bytes:
            raise StorageVerificationError(
                f"declared input {job_input.artifact!r} is {stored.size_bytes} bytes but "
                f"{job_input.size_bytes} bytes were declared"
            )
        return size, job_input.sha256

    def _fail(self, record: JobRecord, exc: InfraError) -> JobRecord:
        """Record an actionable failure without erasing the evidence already collected."""

        store = self._context.job_store
        # Re-read the durable record: a phase may have persisted new evidence (materialized
        # inputs, the verified commit) immediately before raising.
        latest = store.get(record.job_id) or record
        reason = str(exc)[:1000]
        return store.transition(
            latest,
            JobState.FAILED,
            reason=reason,
            failure_reason=reason,
        )
