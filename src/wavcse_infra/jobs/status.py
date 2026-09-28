"""Job status reconciliation, durable output persistence, and cancellation.

Status is derived from the worker's own recorded evidence, never from a stale local
assumption. A job reaches SUCCEEDED only when the command exited zero *and* every
required declared output was persisted and verified through Phase 5.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from wavcse_infra.errors import (
    InfraError,
    JobCancellationError,
    JobExecutionError,
    ProviderNotFoundError,
    ProviderUnavailableError,
)
from wavcse_infra.jobs.collect import failed_output_record, persist_output
from wavcse_infra.jobs.context import JobContext, require_storage
from wavcse_infra.jobs.execution import RemoteJobStatus
from wavcse_infra.jobs.models import TERMINAL_JOB_STATES, JobOutputRecord, JobRecord, JobState
from wavcse_infra.models import Worker, WorkerState


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
    ) -> None:
        self._context = context
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_interval = poll_interval_seconds

    def refresh(self, job_id: str) -> RefreshResult:
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
            if worker.state in {WorkerState.PROVISIONING, WorkerState.STARTING}:
                return RefreshResult(
                    record,
                    warning=(
                        f"worker {record.worker_id} is {worker.state.value}; a submitted job "
                        "cannot run until it is RUNNING"
                    ),
                )
            return RefreshResult(
                self._mark_failed(
                    record,
                    f"worker {record.worker_id} left RUNNING for {worker.state.value} before "
                    "the job reported completion; the job outcome is unknown",
                    worker_absent=worker.state in {WorkerState.TERMINATING, WorkerState.DESTROYED},
                    remote_status=f"worker_{worker.state.value.lower()}",
                )
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
        return RefreshResult(self._apply_status(record, status))

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
            self._sleep(min(self._poll_interval, remaining))
            result = self.refresh(job_id)
        return result

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

    def _apply_status(self, record: JobRecord, status: RemoteJobStatus) -> JobRecord:
        """Update durable state from one worker status report."""

        store = self._context.job_store
        if record.state in TERMINAL_JOB_STATES:
            return record
        if status.status == "running":
            return self._ensure_running(
                record,
                reason="worker reports the job process is running",
                pid=status.pid,
                log_bytes=status.log_bytes,
                remote_status="running",
            )
        if status.status == "unknown":
            return self._mark_failed(
                record,
                f"worker {record.worker_id} has no recorded state for job {record.job_id}; "
                "the job workspace was removed or the worker was rebuilt, so the outcome is "
                "unknown",
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
        record = self._persist_outputs(record)
        self._capture_log(record)
        updates = {
            "exit_code": status.exit_code,
            "finished_at": status.finished_at,
            "log_bytes": status.log_bytes,
            "remote_status": status.status,
        }
        if status.executed_commit is not None:
            updates["executed_commit"] = status.executed_commit
        record = store.save(record.model_copy(update=updates))

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
            output.path for output in record.outputs if output.required and not output.persisted
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
        """Persist every declared output, recording failures instead of hiding them."""

        store = self._context.job_store
        if not record.spec.outputs:
            return record
        try:
            storage = require_storage(self._context)
        except InfraError as exc:
            return store.save(
                record.model_copy(
                    update={
                        "outputs": tuple(
                            existing
                            if existing.persisted
                            else failed_output_record(output, str(exc))
                            for output, existing in zip(
                                record.spec.outputs, record.outputs, strict=True
                            )
                        )
                    }
                )
            )
        records: list[JobOutputRecord] = []
        for output, existing in zip(record.spec.outputs, record.outputs, strict=True):
            if existing.persisted:
                records.append(existing)
                continue
            try:
                updated = persist_output(
                    self._context.transfer,
                    storage,
                    record.worker_id,
                    job_directory=record.job_directory,
                    output=output,
                )
            except InfraError as exc:
                updated = failed_output_record(output, str(exc))
            records.append(updated)
            record = store.save(record.model_copy(update={"outputs": tuple(records)}))
        return record

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
            )
        updates = {"remote_status": remote_status}
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
        )

    def _observe_worker(self, worker: Worker) -> None:
        """Refresh tracked worker metadata without letting a state error break status."""

        try:
            self._context.worker_state.observe(worker)
        except InfraError:
            return
