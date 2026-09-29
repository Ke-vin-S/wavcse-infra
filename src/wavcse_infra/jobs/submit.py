"""Validated, exact-commit job submission against one explicit READY worker.

Submission is one bounded, idempotent preparation pass:

    install the reviewed runner -> materialize the exact commit -> materialize declared
    inputs -> start the detached command

Every step is safe to repeat, which is what makes reconciliation possible: the same pass
drives a first submission and a later retry of one that the controller stopped watching.
A controller-side interruption records evidence and leaves the job in PREPARING; it never
writes a terminal state, because the worker-side phase may still be running.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from wavcse_infra import __version__
from wavcse_infra.errors import (
    ArtifactDestinationExistsError,
    ArtifactTransferError,
    ConfigurationError,
    InfraError,
    JobLaunchExcludedError,
    JobPreconditionError,
    ReconcilableOperationError,
    SshError,
    StorageObjectNotFoundError,
    StorageVerificationError,
)
from wavcse_infra.jobs.collect import failed_output_record, resolve_input_destination
from wavcse_infra.jobs.context import JobContext, require_ready_worker, require_storage
from wavcse_infra.jobs.models import (
    JobInput,
    JobInputRecord,
    JobPreparationPhase,
    JobProvenance,
    JobRecord,
    JobSpec,
    JobState,
)
from wavcse_infra.jobs.state import JobStateStore
from wavcse_infra.models import Worker
from wavcse_infra.state import WorkerRecord
from wavcse_infra.workers.ssh import remote_outcome_is_unknown


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
        """Submit one recorded job, persisting FAILED with evidence on any failure.

        A returned record may be `PREPARING` with `reconciliation_required` set. That means
        the controller stopped watching a remote phase that may still be running: the job
        was neither failed nor lost, and `infra job status` reconciles it.
        """

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
        with store.locked(job_id):
            store.create(record)
            record = store.transition(
                record,
                JobState.PREPARING,
                reason="installing the reviewed runner and materializing declared inputs",
            )
            try:
                return self.advance(record, secrets=secrets)
            except InfraError as exc:
                self._fail(record, exc)
                raise

    def advance(
        self,
        record: JobRecord,
        *,
        secrets: Mapping[str, str] | None = None,
        workspace_prepared: bool = False,
    ) -> JobRecord:
        """Run one bounded, idempotent preparation pass over a durable record.

        `workspace_prepared` is a worker-evidence hint: when the worker has already reported
        a matching prepared workspace for this job, the runner installation and the source
        checkout are skipped, because the `start` phase verifies the commit again before it
        launches anything. It never skips input materialization, which is the step that may
        still be running or may have completed in the background.

        Returns the record in `RUNNING`, in `PREPARING` with reconciliation evidence when a
        remote phase was interrupted, or raises for a definitive failure the caller must
        record as `FAILED`.
        """

        context = self._context
        store: JobStateStore = context.job_store
        if record.state not in {JobState.PENDING, JobState.PREPARING}:
            # Never re-drive a record that already has, or may have, a running command.
            return record
        if record.state is JobState.PENDING:
            record = store.transition(
                record,
                JobState.PREPARING,
                reason="installing the reviewed runner and materializing declared inputs",
            )
        resolved = (
            dict(secrets) if secrets is not None else resolve_secrets(record.spec, context.environ)
        )
        worker = context.provider.get_worker(record.worker_id)
        worker_record = context.worker_state.get(record.worker_id)
        if worker_record is None:
            raise JobPreconditionError(
                f"Worker {record.worker_id} is no longer tracked locally, so job "
                f"{record.job_id} cannot be resumed; its readiness is unknown"
            )
        try:
            if not (workspace_prepared and record.executed_commit is not None):
                record = self._install_and_prepare(record)
            record = self._materialize_inputs(record)
            return self._start(
                record,
                worker=worker,
                worker_record=worker_record,
                secrets=resolved,
            )
        except ReconcilableOperationError as exc:
            return self._record_interruption(record, exc)
        except JobLaunchExcludedError as exc:
            return self._record_launch_excluded(record, exc)
        except SshError as exc:
            # Any SSH failure that reaches this point came from a step that could not
            # classify it (a readiness wait, for example). A controller that lost contact
            # is never evidence about the job, so it keeps the record reconcilable.
            if not remote_outcome_is_unknown(exc):
                raise
            return self._record_interruption(record, exc)

    def _install_and_prepare(self, record: JobRecord) -> JobRecord:
        """Install the reviewed runner and verify the exact requested commit."""

        store = self._context.job_store
        executor = self._context.executor
        record = self._set_phase(
            record,
            JobPreparationPhase.INSTALLING_RUNNER,
            "installing the reviewed job runner on the worker",
        )
        executor.install_runner(record.worker_id)
        record = self._set_phase(
            record,
            JobPreparationPhase.PREPARING_SOURCE,
            "creating the job workspace and verifying the requested commit",
        )
        prepared = executor.prepare(
            record.worker_id,
            job_id=record.job_id,
            job_directory=record.job_directory,
            repository=record.spec.source.repository,
            commit=record.requested_commit,
            name=record.name,
            input_destinations=tuple(job_input.destination for job_input in record.spec.inputs),
        )
        return store.save(record.model_copy(update={"executed_commit": prepared.executed_commit}))

    def _materialize_inputs(self, record: JobRecord) -> JobRecord:
        """Ensure every declared input is present and satisfies its integrity contract.

        An input established by a transfer in this same pass carries that transfer's own
        end-to-end size and digest verification, which is why it is not hashed again. Every
        other declared input - including one an earlier attempt recorded as materialized -
        is verified from the worker's evidence before the command may start, because a
        controller-side flag is a record of a past observation, not proof of the present
        one. Absent or partial state is materialized (resuming where possible); an artifact
        that does not match its declared size or digest is replaced with a verified one
        instead of being trusted.
        """

        store: JobStateStore = self._context.job_store
        records: list[JobInputRecord] = list(record.inputs)
        if record.spec.inputs:
            record = self._set_phase(
                record,
                JobPreparationPhase.MATERIALIZING_INPUTS,
                "materializing declared inputs on the worker",
            )
        for index, job_input in enumerate(record.spec.inputs):
            existing = records[index]
            try:
                updated = self._ensure_input(record, job_input, existing)
            except ReconcilableOperationError:
                # An interrupted, unstartable, or exhausted attempt is not a job failure.
                raise
            except ConfigurationError:
                # Keep the type: a controller that is not configured to materialize this
                # job is a precondition for a first submission and uncertainty for a
                # reconciliation, and only the caller knows which one this is.
                raise
            except InfraError as exc:
                records[index] = existing.model_copy(
                    update={
                        "failure_reason": str(exc)[:1000],
                        "materialized": False,
                    }
                )
                record = store.save(record.model_copy(update={"inputs": tuple(records)}))
                if job_input.required:
                    raise JobPreconditionError(
                        f"required input {job_input.artifact!r} could not be materialized: {exc}"
                    ) from exc
                continue
            records[index] = updated
            record = store.save(record.model_copy(update={"inputs": tuple(records)}))
        return record

    def _ensure_input(
        self,
        record: JobRecord,
        job_input: JobInput,
        existing: JobInputRecord,
    ) -> JobInputRecord:
        """Return one declared input as materialized, verifying anything already recorded."""

        context = self._context
        storage = require_storage(context)
        expected_size, expected_sha256 = self._expectations(job_input)
        destination = resolve_input_destination(record.job_directory, job_input)
        replace = False
        if existing.materialized:
            state, verified = self._input_state(record, destination, expected_size, expected_sha256)
            if state == "complete" and verified is not None:
                # The evidence is re-derived from the worker, but where those bytes originally
                # came from is a fact from the pass that materialized them.
                return verified.model_copy(update={"source": existing.source})
            replace = state == "mismatch"
        # A rebuildable cache is content-addressed, so it has nothing to say about an input
        # whose content is not identified by a declared digest. Deciding that here keeps the
        # rule visible at the call site instead of relying on the cache to decline.
        cache = context.cache if expected_sha256 is not None else None
        cache_root = context.cache_root(record.worker_id) if cache is not None else None
        try:
            if cache is not None and cache_root is not None:
                outcome = cache.materialize(
                    record.worker_id,
                    cache_root=cache_root,
                    storage=storage,
                    key=job_input.artifact,
                    destination=destination,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    overwrite=replace,
                    artifact_label=job_input.artifact,
                )
                result = outcome.result
                source = outcome.source
            else:
                result = context.transfer.download(
                    record.worker_id,
                    storage=storage,
                    key=job_input.artifact,
                    destination=destination,
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    overwrite=replace,
                )
                source = "canonical"
        except ArtifactDestinationExistsError:
            # Something is already placed at the destination. Only worker evidence may
            # decide whether it is the artifact that was wanted, and it is never replaced
            # on a guess.
            verified = context.transfer.verify(
                record.worker_id,
                destination=destination,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            if verified is None:
                raise ArtifactTransferError(
                    f"worker {record.worker_id} has an incomplete file at {destination} that is "
                    f"not the materialized input {job_input.artifact!r}; it was left in place"
                ) from None
            return existing.model_copy(
                update={
                    "worker_path": destination,
                    "materialized": True,
                    "size_bytes": verified.size_bytes,
                    "sha256": verified.sha256,
                    "failure_reason": None,
                    "source": existing.source,
                }
            )
        return existing.model_copy(
            update={
                "worker_path": destination,
                "materialized": True,
                "size_bytes": result.size_bytes,
                "sha256": result.sha256,
                "failure_reason": None,
                "source": source,
            }
        )

    def _input_state(
        self,
        record: JobRecord,
        destination: str,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> tuple[str, JobInputRecord | None]:
        """Report one input as "complete", "absent", or "mismatch" from worker evidence."""

        try:
            verified = self._context.transfer.verify(
                record.worker_id,
                destination=destination,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
        except ArtifactTransferError:
            # Present but not the declared artifact. It is replaced, not trusted.
            return "mismatch", None
        if verified is None:
            return "absent", None
        return (
            "complete",
            JobInputRecord(
                artifact=destination,
                destination=destination,
                required=True,
                worker_path=destination,
                size_bytes=verified.size_bytes,
                sha256=verified.sha256,
                materialized=True,
            ),
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

    def _start(
        self,
        record: JobRecord,
        *,
        worker: Worker,
        worker_record: WorkerRecord,
        secrets: Mapping[str, str],
    ) -> JobRecord:
        """Start the job detached in the worker's own session and record RUNNING."""

        store: JobStateStore = self._context.job_store
        commit = record.executed_commit or record.requested_commit
        record = self._set_phase(
            record,
            JobPreparationPhase.STARTING_COMMAND,
            "starting the job command on the worker",
        )
        started = self._context.executor.start(
            record.worker_id,
            job_id=record.job_id,
            job_directory=record.job_directory,
            repository=record.spec.source.repository,
            commit=commit,
            argv=list(record.spec.command.argv),
            setup_argv=list(record.spec.setup.argv) if record.spec.setup is not None else None,
            working_directory=record.spec.command.working_directory,
            environment=dict(record.spec.runtime.environment),
            secrets=dict(secrets),
            timeout_seconds=(
                record.spec.runtime.timeout_seconds
                if record.spec.runtime.timeout_seconds is not None
                else self._context.jobs_config.default_timeout_seconds
            ),
            infra_environment=infra_environment(
                worker,
                worker_record,
                job_id=record.job_id,
                commit=commit,
            ),
        )
        if started.executed_commit != record.requested_commit:
            raise JobPreconditionError(
                f"worker {worker.id} acknowledged commit {started.executed_commit}, but "
                f"job {record.job_id} requested {record.requested_commit}; refusing this start"
            )
        return store.transition(
            record,
            JobState.RUNNING,
            reason="job command started on the worker",
            pid=started.pid,
            started_at=started.started_at,
            executed_commit=started.executed_commit,
            remote_status="running",
            reconciliation_required=False,
        )

    def _set_phase(
        self,
        record: JobRecord,
        phase: JobPreparationPhase,
        reason: str,
    ) -> JobRecord:
        """Record which preparation step the controller is about to issue."""

        return self._context.job_store.save(
            record.model_copy(update={"preparation_phase": phase, "state_reason": reason})
        )

    def _record_interruption(self, record: JobRecord, exc: InfraError) -> JobRecord:
        """Keep PREPARING and persist why the controller stopped watching.

        A bounded controller wait or a dropped connection is not evidence about the worker's
        phase, so no terminal state and no `failure_reason` is written. The record carries
        the interruption time and a flag that tells an operator and an autonomous caller
        that reconciliation, not retrying, is the next step.
        """

        reason = (
            "the controller stopped watching a worker phase that may still be running; "
            f"reconciliation required: {exc}"
        )
        # Re-read the durable record: a phase may have persisted new evidence (the verified
        # commit, the preparation phase it had reached) immediately before raising.
        latest = self._context.job_store.get(record.job_id) or record
        return self._context.job_store.save(
            latest.model_copy(
                update={
                    "state_reason": reason[:1000],
                    "reconciliation_required": True,
                    "interrupted_at": latest.interrupted_at or self._context.now(),
                    "failure_reason": None,
                }
            )
        )

    def _record_launch_excluded(self, record: JobRecord, exc: InfraError) -> JobRecord:
        """Record CANCELLED from the worker's own proof that the launch is excluded.

        This is the only way a cancellation is recorded from the worker side, and it is
        affirmative evidence: the worker durably refused the launch, so no later launch can
        happen either.
        """

        store = self._context.job_store
        latest = store.get(record.job_id) or record
        reason = f"the worker excluded this launch because the job was cancelled: {exc}"[:1000]
        return store.transition(
            latest,
            JobState.CANCELLED,
            reason=reason,
            finished_at=latest.finished_at or self._context.now(),
            reconciliation_required=False,
        )

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
            reconciliation_required=False,
        )
