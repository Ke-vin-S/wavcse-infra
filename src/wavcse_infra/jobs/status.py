"""Job status reconciliation, durable output persistence, and cancellation.

Status is derived from the worker's own recorded evidence, never from a stale local
assumption. A job reaches SUCCEEDED only when the command exited zero *and* every
required declared output was persisted and verified through Phase 5.

Reconciliation is deliberately conservative about what it may conclude. A job is failed
only from evidence — a reported command failure, a process that is gone with no outcome,
a workspace that is demonstrably absent, or a launch that provably never started. Silence
from a bounded controller wait, an interrupted SSH session, or a worker phase that may
still be running keeps the job in PREPARING with the reason recorded, and a later
`infra job status` picks the work back up because every preparation step is idempotent.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from wavcse_infra.errors import (
    ConfigurationError,
    InfraError,
    JobCancellationError,
    JobExecutionError,
    JobPreconditionError,
    ProviderError,
    ProviderNotFoundError,
    ProviderUnavailableError,
    ReconcilableOperationError,
    SshError,
    StateError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StorageVerificationError,
)
from wavcse_infra.jobs.collect import (
    failed_output_record,
    persist_output,
    resolve_output_source,
)
from wavcse_infra.jobs.context import JobContext, require_storage
from wavcse_infra.jobs.execution import RemoteJobStatus
from wavcse_infra.jobs.models import (
    TERMINAL_JOB_STATES,
    JobOutput,
    JobOutputRecord,
    JobPreparationPhase,
    JobRecord,
    JobState,
)
from wavcse_infra.jobs.submit import JobSubmitter, resolve_secrets
from wavcse_infra.models import Worker, WorkerState
from wavcse_infra.workers.ssh import remote_outcome_is_unknown

# A provider state says whether the worker can be reached, not whether the command failed.
# The workspace survives a stop/start cycle, so every state a started worker can return from
# keeps the job reconcilable; only states that mean the workspace is gone for good can make
# the recorded outcome unreadable.
_UNRECOVERABLE_WORKER_STATES = frozenset(
    {
        WorkerState.TERMINATING,
        WorkerState.DESTROYED,
        WorkerState.ERROR,
    }
)
# Errors a reconciliation pass reports as uncertainty instead of writing a terminal state:
# they describe the controller's own reach or the state of the work in progress, never the
# remote job's outcome. The reconcilable family covers an interrupted observation, a
# transfer attempt that exhausted its retries, an unfinished competing transfer, and a
# canonical-storage observation that failed for service reasons.
_RECONCILIATION_UNCERTAINTY = (
    ReconcilableOperationError,
    ConfigurationError,
    ProviderError,
    StateError,
)
# Seconds between reconciliation polls while a job is still preparing. Preparation work is
# long-running, so polling it as often as a RUNNING job would only add SSH traffic.
PREPARATION_POLL_INTERVAL_SECONDS = 15.0


@dataclass(frozen=True)
class RefreshResult:
    """Reconciled job record plus any uncertainty the operator must see."""

    record: JobRecord
    warning: str | None = None


class JobCoordinator:
    """Reconcile durable job records with the worker that actually ran them."""

    def __init__(
        self,
        context: JobContext,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = 2.0,
        preparation_poll_interval_seconds: float = PREPARATION_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._context = context
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_interval = poll_interval_seconds
        self._preparation_poll_interval = preparation_poll_interval_seconds

    def refresh(self, job_id: str) -> RefreshResult:
        """Reconcile one job under its local lock, so concurrent controllers cannot race.

        Every mutating operation on one job takes the same local lock and re-reads the
        record inside it; a decision made from a stale read is therefore never written over
        newer state.
        """

        with self._context.job_store.locked(job_id):
            return self._refresh_locked(job_id)

    def _refresh_locked(self, job_id: str) -> RefreshResult:
        """Reconcile one job's durable state with real worker evidence."""

        store = self._context.job_store
        record = store.require(job_id)
        if record.state in TERMINAL_JOB_STATES:
            # A finished job is never re-opened; its recorded evidence is the history.
            return RefreshResult(record)
        try:
            worker = self._context.provider.get_worker(record.worker_id)
        except ProviderNotFoundError:
            return RefreshResult(
                self._mark_failed(
                    record,
                    f"RunPod worker {record.worker_id} no longer exists, so the job outcome "
                    "is unknown; the worker was destroyed or terminated before it reported "
                    "completion",
                    worker_absent=True,
                    remote_status="worker_absent",
                )
            )
        except ProviderUnavailableError as exc:
            return RefreshResult(
                record,
                warning=(
                    f"provider state for worker {record.worker_id} is unavailable; last "
                    f"recorded job state is {record.state.value}: {exc}"
                ),
            )

        if worker.state is not WorkerState.RUNNING:
            if worker.state in _UNRECOVERABLE_WORKER_STATES:
                return RefreshResult(
                    self._mark_failed(
                        record,
                        f"RunPod worker {record.worker_id} reached {worker.state.value}, so the "
                        "workspace that held this job is gone and its outcome can no longer be "
                        "read; the job cannot be verified as successful",
                        worker_absent=worker.state
                        in {WorkerState.TERMINATING, WorkerState.DESTROYED},
                        remote_status=f"worker_{worker.state.value.lower()}",
                    )
                )
            # A stopped or restarting worker is not evidence about the job: its disk, and
            # any outcome the command already recorded there, survive a stop/start cycle.
            return RefreshResult(
                self._mark_reconcilable(
                    record,
                    f"worker {record.worker_id} is {worker.state.value}; the job stays "
                    f"{record.state.value} because the outcome it recorded on the worker "
                    "cannot be read until that worker is RUNNING again",
                ),
                warning=(
                    f"worker {record.worker_id} is {worker.state.value}; job {record.job_id} "
                    f"stays {record.state.value} because the outcome it recorded on the worker "
                    "cannot be read until that worker is RUNNING again — start it explicitly, "
                    "then run `infra job status`"
                ),
            )

        self._observe_worker(worker)
        try:
            status = self._context.executor.inspect(
                record.worker_id,
                job_id=record.job_id,
                job_directory=record.job_directory,
            )
        except InfraError as exc:
            return RefreshResult(
                record,
                warning=(
                    f"worker {record.worker_id} could not be inspected; last recorded job "
                    f"state is {record.state.value}: {exc}"
                ),
            )
        if status.status == "unknown":
            return self._reconcile_unknown(record, status)
        updated = self._apply_status(record, status)
        if updated.reconciliation_required:
            return RefreshResult(
                updated,
                warning=(
                    f"job {updated.job_id} requires reconciliation before it can be judged: "
                    f"{updated.state_reason or 'no reason recorded'}"
                ),
            )
        return RefreshResult(updated)

    def wait(self, job_id: str, *, timeout_seconds: float) -> RefreshResult:
        """Poll one job until it reaches a terminal state or the bound expires."""

        if timeout_seconds <= 0:
            raise JobCancellationError("job wait timeout must be greater than zero")
        deadline = self._monotonic() + timeout_seconds
        result = self.refresh(job_id)
        while result.record.state not in TERMINAL_JOB_STATES:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return RefreshResult(
                    result.record,
                    warning=(
                        f"job {job_id} did not finish within {timeout_seconds:g} seconds; it "
                        f"may still be running. Last state: {result.record.state.value}"
                    ),
                )
            self._sleep(min(self._poll_interval_for(result.record), remaining))
            result = self.refresh(job_id)
        return result

    def _poll_interval_for(self, record: JobRecord) -> float:
        """Poll a preparing job less often than a job that is already executing."""

        if record.state is JobState.RUNNING:
            return self._poll_interval
        return max(self._poll_interval, self._preparation_poll_interval)

    def logs(self, job_id: str, *, tail_bytes: int | None = None) -> str:
        """Return a bounded tail of the job's remote log."""

        record = self._context.job_store.require(job_id)
        limit = tail_bytes if tail_bytes is not None else self._context.jobs_config.log_tail_bytes
        try:
            return self._context.executor.logs(
                record.worker_id,
                job_id=record.job_id,
                job_directory=record.job_directory,
                tail_bytes=limit,
            )
        except ProviderNotFoundError as exc:
            raise JobExecutionError(
                f"RunPod worker {record.worker_id} no longer exists, so the remote log for job "
                f"{job_id} cannot be read: {exc}"
            ) from exc

    def cancel(self, job_id: str) -> JobRecord:
        """Terminate the job's own process tree; never touches the worker lifecycle."""

        with self._context.job_store.locked(job_id):
            return self._cancel_locked(job_id)

    def _cancel_locked(self, job_id: str) -> JobRecord:
        """Apply one cancellation, holding the job's local lock for the whole decision."""

        store = self._context.job_store
        record = store.require(job_id)
        if record.state in TERMINAL_JOB_STATES:
            return record
        try:
            worker = self._context.provider.get_worker(record.worker_id)
        except ProviderNotFoundError:
            return self._mark_failed(
                record,
                f"RunPod worker {record.worker_id} no longer exists; nothing was running to "
                "cancel and the job outcome is unknown",
                worker_absent=True,
                remote_status="worker_absent",
            )
        if worker.state is not WorkerState.RUNNING:
            raise JobCancellationError(
                f"worker {record.worker_id} is {worker.state.value}; cancellation targets the "
                "job process on a RUNNING worker. Wait for the provider state to settle, then "
                "run `infra job status`"
            )
        if record.state in {JobState.PENDING, JobState.PREPARING}:
            return self._cancel_before_start(record)
        result = self._context.executor.cancel(
            record.worker_id,
            job_id=record.job_id,
            job_directory=record.job_directory,
        )
        if result.already_finished:
            return self.refresh(job_id).record
        if not result.cancelled:
            raise JobCancellationError(
                f"worker {record.worker_id} did not confirm cancellation of job {job_id}"
            )
        store.save(
            record.model_copy(
                update={
                    "cancellation_requested_at": self._context.now(),
                    "state_reason": "operator requested cancellation; awaiting worker outcome",
                }
            )
        )
        return self.refresh(job_id).record

    def _cancel_before_start(self, record: JobRecord) -> JobRecord:
        """Cancel a job whose command has not been recorded as running.

        The decision belongs to the worker, because only the worker can exclude a launch
        that has not happened yet: its runner serializes creating the process against this
        cancellation, so exactly one of them wins. A "pre-start" answer is affirmative
        evidence that the command did not and cannot run, which is what recording CANCELLED
        requires; a "launch in progress" answer is the opposite and is reported instead of
        being papered over.
        """

        store = self._context.job_store
        try:
            result = self._context.executor.cancel(
                record.worker_id,
                job_id=record.job_id,
                job_directory=record.job_directory,
            )
        except InfraError as exc:
            raise JobCancellationError(
                f"worker {record.worker_id} did not establish the cancellation of job "
                f"{record.job_id}, so nothing was recorded; run `infra job status` and retry: "
                f"{exc}"
            ) from exc
        if result.already_finished:
            return self.refresh(record.job_id).record
        if result.launch_in_progress:
            raise JobCancellationError(
                f"the command launch for job {record.job_id} is in progress on worker "
                f"{record.worker_id} and owns the job; retry `infra job cancel` to terminate "
                "the process it is creating"
            )
        if not result.cancelled:
            raise JobCancellationError(
                f"worker {record.worker_id} did not confirm cancellation of job {record.job_id}"
            )
        now = self._context.now()
        if result.pre_start:
            reason = (
                "the worker excluded this job's launch before it started, so the command did "
                "not run and cannot run"
            )
        else:
            reason = (
                "the worker terminated this job's recorded process group; the command was "
                "cancelled before it produced a result"
            )
        # The worker's confirmation is the evidence; the job is never reconciled afterwards,
        # because reconciling a cancelled job is what would launch it.
        return store.transition(
            record,
            JobState.CANCELLED,
            reason=reason,
            cancellation_requested_at=record.cancellation_requested_at or now,
            finished_at=record.finished_at or now,
            reconciliation_required=False,
        )

    def _cancel_running(self, record: JobRecord) -> JobRecord:
        """Forward cancellation to the worker's own recorded process identity."""

        store = self._context.job_store
        result = self._context.executor.cancel(
            record.worker_id,
            job_id=record.job_id,
            job_directory=record.job_directory,
        )
        if result.already_finished:
            return self.refresh(record.job_id).record
        if not result.cancelled:
            raise JobCancellationError(
                f"worker {record.worker_id} did not confirm cancellation of job {record.job_id}"
            )
        store.save(
            record.model_copy(
                update={
                    "cancellation_requested_at": self._context.now(),
                    "state_reason": "operator requested cancellation; awaiting worker outcome",
                }
            )
        )
        return self.refresh(record.job_id).record

    def _reconcile_unknown(self, record: JobRecord, status: RemoteJobStatus) -> RefreshResult:
        """Reconcile a worker that reports no live process and no recorded outcome."""

        if status.prepared is None or status.started is None:
            # The installed runner predates this evidence. Reinstalling it changes nothing
            # about the job, so it is safe to ask again with the current runner.
            refreshed = self._reinstall_runner_evidence(record)
            if refreshed is not None:
                status = refreshed
                if status.status != "unknown":
                    return RefreshResult(self._apply_status(record, status))
        if record.state is JobState.RUNNING:
            return RefreshResult(
                self._mark_failed(
                    record,
                    _NO_OUTCOME_RUNNING.format(worker=record.worker_id, job=record.job_id),
                    remote_status="unknown",
                )
            )
        if status.prepared is None or status.started is None:
            return self._keep_reconcilable(
                record,
                f"worker {record.worker_id} did not report preparation evidence for job "
                f"{record.job_id}; it is not known whether a command started",
            )
        if status.started:
            return self._reconcile_started(record, status)
        # A workspace that is gone after the controller issued preparation steps may have
        # held a running command whose process is now unreachable. It is never re-created,
        # because doing so could execute the command a second time.
        issued_preparation = record.executed_commit is not None or (
            record.preparation_phase is JobPreparationPhase.STARTING_COMMAND
        )
        if status.job_directory_exists is False and issued_preparation:
            return RefreshResult(
                self._mark_failed(
                    record,
                    f"the job workspace {record.job_directory} is absent from worker "
                    f"{record.worker_id} after this controller had already issued preparation "
                    "steps for it: the workspace was removed or the worker was rebuilt. "
                    "Whether the command ever started cannot be determined from this worker, "
                    "and it is never re-run implicitly",
                    remote_status="workspace_absent",
                )
            )
        # Nothing has been launched, so preparation may simply be resumed. Every step is
        # idempotent, and a completed background transfer is verified before it is trusted.
        return self._resume_preparation(
            record,
            workspace_prepared=status.prepared is True,
        )

    def _reconcile_started(self, record: JobRecord, status: RemoteJobStatus) -> RefreshResult:
        """Reconcile a job whose launch slot the worker has already claimed."""

        if status.group_pid is not None:
            # A descendant of this job outlived the stage leader it was created under, so
            # the job is still executing even though no recorded leader is alive.
            return self._keep_reconcilable(
                record,
                f"a process group of job {record.job_id} (group {status.group_pid}) is still "
                f"running on worker {record.worker_id}, so the command has not finished",
            )
        if status.pid is not None:
            return RefreshResult(
                self._mark_failed(
                    record,
                    f"worker {record.worker_id} recorded a job process for {record.job_id} that "
                    "is no longer running, and recorded no outcome; the command result cannot "
                    "be verified",
                    remote_status="unknown",
                )
            )
        if status.launch_alive is True:
            return self._keep_reconcilable(
                record,
                f"the command launch for job {record.job_id} on worker {record.worker_id} is "
                "still in progress",
            )
        if status.launch_alive is False:
            return RefreshResult(
                self._mark_failed(
                    record,
                    f"the command launch for job {record.job_id} on worker {record.worker_id} "
                    "did not complete: the runner recorded no process, and a command it never "
                    "recorded can never run. No command started",
                    remote_status="unknown",
                )
            )
        return self._keep_reconcilable(
            record,
            f"worker {record.worker_id} recorded a start slot for job {record.job_id} without a "
            "verifiable launch identity; it is not known whether a command started",
        )

    def _resume_preparation(
        self,
        record: JobRecord,
        *,
        workspace_prepared: bool = False,
    ) -> RefreshResult:
        """Drive one bounded preparation pass, reporting uncertainty instead of guessing.

        This may start the job's command: the command never started, so completing
        preparation is the reconciliation the record calls for. `infra job cancel` is the
        explicit way to stop that from happening.
        """

        try:
            secrets = resolve_secrets(record.spec, self._context.environ)
        except JobPreconditionError as exc:
            return self._keep_reconcilable(
                record,
                f"job {record.job_id} cannot be resumed on this controller: {exc}",
            )
        try:
            updated = JobSubmitter(self._context).advance(
                record,
                secrets=secrets,
                workspace_prepared=workspace_prepared,
            )
        except _RECONCILIATION_UNCERTAINTY as exc:
            return self._keep_reconcilable(record, f"job {record.job_id}: {exc}")
        except SshError as exc:
            if not remote_outcome_is_unknown(exc):
                raise
            return self._keep_reconcilable(record, f"job {record.job_id}: {exc}")
        except InfraError as exc:
            return RefreshResult(
                self._mark_failed(
                    record,
                    f"preparation of job {record.job_id} on worker {record.worker_id} failed: "
                    f"{exc}",
                    remote_status="preparation_failed",
                )
            )
        if updated.state in {*TERMINAL_JOB_STATES, JobState.RUNNING}:
            return RefreshResult(updated)
        return RefreshResult(
            updated,
            warning=(
                f"job {record.job_id} preparation was resumed on worker {record.worker_id} and "
                f"is still PREPARING: {updated.state_reason or 'no reason recorded'}"
            ),
        )

    def _reinstall_runner_evidence(self, record: JobRecord) -> RemoteJobStatus | None:
        """Reinstall the current runner and inspect again, or report nothing when unsure."""

        executor = self._context.executor
        try:
            executor.install_runner(record.worker_id)
            return executor.inspect(
                record.worker_id,
                job_id=record.job_id,
                job_directory=record.job_directory,
            )
        except InfraError:
            return None

    def _mark_reconcilable(self, record: JobRecord, reason: str) -> JobRecord:
        """Keep a job nonterminal with durable evidence that reconciliation is required.

        The state itself is preserved: a preparing job stays preparing and a job whose
        command already finished stays where it is, because what is missing is an
        observation, not an outcome. The durable record is re-read first, because the step
        that produced the uncertainty may already have persisted evidence of its own.
        """

        store = self._context.job_store
        latest = store.get(record.job_id) or record
        return store.save(
            latest.model_copy(
                update={
                    "state_reason": reason[:1000],
                    "reconciliation_required": True,
                    "interrupted_at": latest.interrupted_at or self._context.now(),
                }
            )
        )

    def _keep_reconcilable(self, record: JobRecord, reason: str) -> RefreshResult:
        """Report a job that stays nonterminal, with a warning naming what is unresolved."""

        updated = self._mark_reconcilable(record, reason)
        return RefreshResult(
            updated,
            warning=(
                f"job {record.job_id} is still {updated.state.value} and requires "
                f"reconciliation: {reason}"
            ),
        )

    def _apply_status(self, record: JobRecord, status: RemoteJobStatus) -> JobRecord:
        """Update durable state from one worker status report."""

        store = self._context.job_store
        if record.state in TERMINAL_JOB_STATES:
            return record
        if status.cancelled and status.pre_start:
            # The worker durably recorded that this job's launch is excluded, which is
            # affirmative evidence that the command never ran and cannot run. That evidence
            # needs no executed commit, because a launch that never happened has no
            # provenance to verify; requiring one here would report a lost acknowledgement
            # as a failure instead of the cancellation the worker actually established.
            return store.transition(
                record,
                JobState.CANCELLED,
                reason=(
                    "the worker durably established that this job's launch was excluded "
                    "before it started, so the command did not run and cannot run"
                ),
                finished_at=record.finished_at or status.finished_at or self._context.now(),
                reconciliation_required=False,
            )
        if status.status == "running":
            return self._ensure_running(
                record,
                reason="worker reports the job process is running",
                pid=status.pid,
                log_bytes=status.log_bytes,
                remote_status="running",
            )
        if status.status == "unknown":
            # Fallback for a direct caller; `refresh` routes this through reconciliation,
            # which has the preparation evidence needed to say *why* nothing is recorded.
            return self._mark_failed(
                record,
                f"worker {record.worker_id} recorded no process and no outcome for job "
                f"{record.job_id}",
                remote_status="unknown",
            )
        record = self._ensure_running(
            record,
            reason="worker reports the job has finished",
            pid=status.pid,
            log_bytes=status.log_bytes,
            remote_status=status.status,
        )
        if status.executed_commit != record.requested_commit or (
            record.executed_commit is not None and status.executed_commit != record.executed_commit
        ):
            return self._mark_failed(
                record,
                f"worker {record.worker_id} reported executed commit "
                f"{status.executed_commit or 'unknown'}, but job {record.job_id} requested "
                f"{record.requested_commit}; result provenance cannot be verified",
                remote_status=status.status,
            )
        # Record what the worker reported before acting on it, so the command's own outcome
        # is durable even when the work that follows is interrupted.
        updates = {
            "exit_code": status.exit_code,
            "finished_at": status.finished_at,
            "log_bytes": status.log_bytes,
            "remote_status": status.status,
            "reconciliation_required": False,
        }
        if status.executed_commit is not None:
            updates["executed_commit"] = status.executed_commit
        record = store.save(record.model_copy(update=updates))
        try:
            record = self._persist_outputs(record)
        except ReconcilableOperationError as exc:
            # Losing sight of a required output's upload is not evidence that persistence
            # failed. The job stays reconcilable and the canonical object is verified on the
            # next pass.
            return self._mark_reconcilable(
                record,
                "the command finished, but required output persistence could not be "
                f"confirmed: {exc}",
            )
        self._capture_log(record)

        if status.cancelled and status.exit_code != 0:
            return store.transition(
                record,
                JobState.CANCELLED,
                reason="the job process was cancelled before it produced a successful exit",
            )
        if status.exit_code != 0:
            timed_out = (
                " The job command exceeded its configured timeout." if status.timed_out else ""
            )
            stage = f" during the {status.stage} stage" if status.stage else ""
            return store.transition(
                record,
                JobState.FAILED,
                reason=f"the job command exited {status.exit_code}{stage}.{timed_out}".strip(),
                failure_reason=(
                    f"exit code {status.exit_code}{stage}"
                    + (", command timed out" if status.timed_out else "")
                ),
            )
        missing = [
            slot.path for slot in _output_slots(record) if slot.required and not slot.persisted
        ]
        if missing:
            reason = (
                "the job command exited 0, but these required outputs were not persisted and "
                "verified: " + ", ".join(missing)
            )
            return store.transition(
                record,
                JobState.FAILED,
                reason=reason,
                failure_reason=reason,
            )
        reason = "the job command exited 0 and every required output was persisted"
        if status.cancelled:
            reason += " (a cancellation request arrived after the command had finished)"
        return store.transition(record, JobState.SUCCEEDED, reason=reason)

    def _persist_outputs(self, record: JobRecord) -> JobRecord:
        """Persist every declared output, recording failures instead of hiding them.

        The durable record always carries exactly one slot per declared output, in
        declaration order. An interrupted pass therefore leaves a short *progress* marker
        rather than a short *structure*: every slot a pass has not reached yet keeps its
        previous record, so a reload after an interruption still knows which outputs are
        declared and which one is unresolved.
        """

        store = self._context.job_store
        if not record.spec.outputs:
            return record
        slots = _output_slots(record)
        try:
            storage = require_storage(self._context)
        except InfraError as exc:
            return store.save(
                record.model_copy(
                    update={
                        "outputs": tuple(
                            existing
                            if existing.persisted
                            else failed_output_record(declared, str(exc))
                            for declared, existing in zip(record.spec.outputs, slots, strict=True)
                        )
                    }
                )
            )
        for index, output in enumerate(record.spec.outputs):
            if slots[index].persisted:
                continue
            try:
                slots[index] = persist_output(
                    self._context.transfer,
                    storage,
                    record.worker_id,
                    job_directory=record.job_directory,
                    output=output,
                )
            except StorageObjectExistsError as exc:
                # An object is already at the declared key. It may be this output's own
                # upload completing after the controller stopped watching, so the canonical
                # object is checked before anything is concluded.
                recovered = self._recover_output_object(record, output)
                slots[index] = (
                    recovered
                    if recovered is not None
                    else failed_output_record(
                        output,
                        f"{exc}; the stored object is not this run's output",
                    )
                )
            except ReconcilableOperationError:
                # Unknown, not failed: the caller keeps the job reconcilable. The slot
                # keeps whatever it already held, and the full slot list is durable.
                record = store.save(record.model_copy(update={"outputs": tuple(slots)}))
                raise
            except InfraError as exc:
                slots[index] = failed_output_record(output, str(exc))
            record = store.save(record.model_copy(update={"outputs": tuple(slots)}))
        return record

    def _recover_output_object(
        self,
        record: JobRecord,
        output: JobOutput,
    ) -> JobOutputRecord | None:
        """Accept an already-stored output object only when its own bytes are proven.

        The upload may have completed after the controller lost sight of it, so an object
        exists at this job's declared key. Existing is not the same as being this output:
        the object is read back and hashed, and it is accepted only when canonical storage
        itself yields exactly the bytes the worker file reports. Any other object at that
        key - an older run's artifact, a different file of the same size - is rejected,
        because recording its existence as this run's digest would misattribute provenance.
        """

        storage = require_storage(self._context)
        source = resolve_output_source(record.job_directory, output)
        source_evidence = self._context.transfer.verify(
            record.worker_id,
            destination=source,
        )
        if source_evidence is None:
            return None
        try:
            verified = storage.verify_object_content(
                output.artifact,
                expected_size=source_evidence.size_bytes,
                expected_sha256=source_evidence.sha256,
            )
        except (StorageObjectNotFoundError, StorageVerificationError):
            # The object vanished, or its bytes are not this output's bytes.
            return None
        return JobOutputRecord(
            path=output.path,
            artifact=output.artifact,
            required=output.required,
            persisted=True,
            size_bytes=verified.size_bytes,
            sha256=verified.content_sha256,
            verified_size_bytes=verified.size_bytes,
        )

    def _capture_log(self, record: JobRecord) -> None:
        """Store a bounded local copy of the job's own output for later inspection."""

        try:
            text = self._context.executor.logs(
                record.worker_id,
                job_id=record.job_id,
                job_directory=record.job_directory,
                tail_bytes=self._context.jobs_config.log_tail_bytes,
            )
        except InfraError:
            return
        try:
            self._context.job_store.write_log(record.job_id, text)
        except InfraError:
            return

    def _ensure_running(
        self,
        record: JobRecord,
        *,
        reason: str,
        remote_status: str,
        pid: int | None = None,
        log_bytes: int | None = None,
    ) -> JobRecord:
        """Advance a record that missed its own start acknowledgement to RUNNING."""

        store = self._context.job_store
        if record.state is JobState.PENDING:
            record = store.transition(record, JobState.PREPARING, reason=reason)
        if record.state is JobState.PREPARING:
            record = store.transition(
                record,
                JobState.RUNNING,
                reason=reason,
                started_at=record.started_at or self._context.now(),
                reconciliation_required=False,
            )
        updates = {"remote_status": remote_status, "reconciliation_required": False}
        if pid is not None:
            updates["pid"] = pid
        if log_bytes is not None:
            updates["log_bytes"] = log_bytes
        return store.save(record.model_copy(update=updates))

    def _mark_failed(
        self,
        record: JobRecord,
        reason: str,
        *,
        worker_absent: bool = False,
        remote_status: str,
    ) -> JobRecord:
        store = self._context.job_store
        if record.state in TERMINAL_JOB_STATES:
            return record
        return store.transition(
            record,
            JobState.FAILED,
            reason=reason,
            failure_reason=reason,
            worker_absent=worker_absent,
            remote_status=remote_status,
            finished_at=record.finished_at or self._context.now(),
            reconciliation_required=False,
        )

    def _observe_worker(self, worker: Worker) -> None:
        """Refresh tracked worker metadata without letting a state error break status."""

        try:
            self._context.worker_state.observe(worker)
        except InfraError:
            return


_NO_OUTCOME_RUNNING = (
    "worker {worker} recorded a job process for {job} that is no longer running, and recorded "
    "no outcome; the command result cannot be verified"
)


def _output_slots(record: JobRecord) -> list[JobOutputRecord]:
    """Return one durable slot per declared output, padding any slot that is missing.

    A record written by an interrupted pass, or by an older controller, may carry fewer
    records than the specification declares. Padding keeps every declared output
    addressable, so a missing required output can never become invisible and let a job
    succeed without it; the next save writes the full structure back.
    """

    slots = list(record.outputs)
    for index in range(len(slots), len(record.spec.outputs)):
        slots.append(failed_output_record(record.spec.outputs[index], "not persisted yet"))
    return slots[: len(record.spec.outputs)]
