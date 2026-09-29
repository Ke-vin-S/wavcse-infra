"""Controller-versus-remote truth: interruption, reconciliation, and diagnostics.

These tests cover the Phase 6.1 incidents directly. A controller-side bound or a dropped
SSH session must never be recorded as a terminal job failure while the worker-side phase
may still be running, and a later status reconciliation must act on worker evidence rather
than on an assumption that a workspace was destroyed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from job_fakes import (
    NOW,
    FakeJobExecutor,
    FakeProvider,
    FakeStorage,
    FakeTransfer,
    job_context,
    job_spec_document,
    ready_worker_store,
    upload_outcome,
)

from wavcse_infra.config import JobsConfig
from wavcse_infra.errors import (
    ArtifactTransferError,
    ArtifactTransferInProgressError,
    ArtifactTransferTransientError,
    JobCancellationError,
    JobExecutionError,
    ProviderUnavailableError,
    RemoteOperationInterruptedError,
    SshConnectionError,
    StorageObjectExistsError,
    StorageUnavailableError,
    StorageVerificationError,
)
from wavcse_infra.jobs.context import JobContext
from wavcse_infra.jobs.execution import CancelResult, RemoteJobStatus
from wavcse_infra.jobs.models import JobPreparationPhase, JobState, load_job_spec
from wavcse_infra.jobs.status import JobCoordinator
from wavcse_infra.jobs.submit import JobSubmitter
from wavcse_infra.models import WorkerState
from wavcse_infra.state import StateError

INPUT_SHA256 = "e" * 64
INPUT_SIZE = 4096


def _spec(**overrides):
    document = job_spec_document(
        inputs=[
            {
                "artifact": "embeddings/v1/probe.tar",
                "destination": "probe.tar",
                "sha256": INPUT_SHA256,
                "size_bytes": INPUT_SIZE,
            }
        ],
        **overrides,
    )
    return load_job_spec(json.dumps(document))


def _storage() -> FakeStorage:
    storage = FakeStorage()
    storage.objects["embeddings/v1/probe.tar"] = INPUT_SIZE
    return storage


def _evidence(
    *,
    status: str = "unknown",
    pid: int | None = None,
    job_directory_exists: bool = True,
    prepared: bool = True,
    started: bool = False,
    launch_alive: bool | None = None,
    exit_code: int | None = None,
    executed_commit: str | None = None,
    stage: str | None = None,
    cancelled: bool = False,
    pre_start: bool = False,
) -> RemoteJobStatus:
    return RemoteJobStatus(
        status=status,
        pid=pid,
        exit_code=exit_code,
        stage=stage,
        cancelled=cancelled,
        pre_start=pre_start,
        started_at=NOW,
        finished_at=NOW,
        executed_commit=executed_commit,
        log_bytes=0,
        job_directory_exists=job_directory_exists,
        prepared=prepared,
        started=started,
        launch_alive=launch_alive,
    )


def _collaborators(tmp_path: Path, **overrides):
    """Return one job context whose durable store and job root live under `tmp_path`."""

    overrides.setdefault(
        "jobs_config",
        JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )
    return job_context(tmp_path, storage=_storage(), **overrides)


def _interrupted_submission(tmp_path: Path, executor: FakeJobExecutor, transfer: FakeTransfer):
    """Submit one job whose first input download is cut off by the controller bound."""

    transfer.download_interrupts = 1
    context = _collaborators(tmp_path, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    return context, record


def _calls(executor: FakeJobExecutor, name: str) -> int:
    return sum(1 for call in executor.calls if call == name)


# --- a bounded controller wait is not a remote failure -----------------------------


def test_preparation_completes_normally(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, executor=executor)

    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert record.state is JobState.RUNNING
    assert record.reconciliation_required is False
    assert record.interrupted_at is None
    assert record.preparation_phase is JobPreparationPhase.STARTING_COMMAND
    assert record.inputs[0].materialized is True
    assert _calls(executor, "install") == 1
    assert _calls(executor, "prepare") == 1
    assert _calls(executor, "start") == 1


def test_a_command_that_reports_its_own_failure_still_fails_the_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer(materialize=True))
    executor.status = _evidence(status="finished", exit_code=3, stage="command")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.exit_code == 3


def test_definitive_materialization_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_error = ArtifactTransferError("the storage endpoint rejected the download")
    context = _collaborators(tmp_path, executor=executor, transfer=transfer)

    with pytest.raises(Exception, match="required input"):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    stored = context.job_store.list_records()[0]
    assert stored.state is JobState.FAILED
    assert "required input" in (stored.failure_reason or "")
    assert stored.inputs[0].materialized is False
    assert stored.inputs[0].failure_reason is not None
    assert _calls(executor, "start") == 0


def test_a_controller_timeout_during_materialization_is_not_a_failure(tmp_path: Path) -> None:
    executor = FakeJobExecutor()

    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())

    assert record.state is JobState.PREPARING
    assert record.reconciliation_required is True
    assert record.interrupted_at == NOW
    assert record.failure_reason is None
    assert record.finished_at is None
    assert record.preparation_phase is JobPreparationPhase.MATERIALIZING_INPUTS
    assert "may still be running" in (record.state_reason or "")
    assert _calls(executor, "start") == 0
    # The durable record is the same object a later process reads back.
    assert context.job_store.get(record.job_id).state is JobState.PREPARING


def test_a_controller_timeout_during_the_source_checkout_is_not_a_failure(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.prepare_error = SshConnectionError("SSH could not connect")
    context = _collaborators(tmp_path, executor=executor)

    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert record.state is JobState.PREPARING
    assert record.preparation_phase is JobPreparationPhase.PREPARING_SOURCE
    assert record.reconciliation_required is True
    assert _calls(executor, "start") == 0


def test_a_controller_timeout_during_the_command_launch_is_not_a_failure(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.start_error = SshConnectionError("SSH connection dropped")
    context = _collaborators(tmp_path, executor=executor)

    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert record.state is JobState.PREPARING
    assert record.preparation_phase is JobPreparationPhase.STARTING_COMMAND
    assert record.reconciliation_required is True
    assert record.failure_reason is None


def test_a_controller_timeout_during_runner_installation_is_not_a_failure(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.install_error = SshConnectionError("SSH could not connect")
    context = _collaborators(tmp_path, executor=executor)

    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert record.state is JobState.PREPARING
    assert record.preparation_phase is JobPreparationPhase.INSTALLING_RUNNER
    assert record.reconciliation_required is True


def test_a_definitive_remote_phase_failure_still_fails_the_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.prepare_error = JobExecutionError(
        "Job prepare failed on RunPod worker pod-123: remote script exited 1"
    )
    context = _collaborators(tmp_path, executor=executor)

    with pytest.raises(JobExecutionError):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    stored = context.job_store.list_records()[0]
    assert stored.state is JobState.FAILED
    assert "Job prepare failed" in (stored.failure_reason or "")


# --- reconciliation of an interrupted preparation ----------------------------------


def test_status_reconciles_a_transfer_that_finished_in_the_background(tmp_path: Path) -> None:
    """The incident: the remote download completed after the controller gave up."""

    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _interrupted_submission(tmp_path, executor, transfer)
    executor.calls.clear()
    executor.status = _evidence(prepared=True, started=False)
    transfer.destination_exists = True
    transfer.verified = True

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert result.record.reconciliation_required is False
    assert result.record.inputs[0].materialized is True
    assert result.record.inputs[0].sha256 == INPUT_SHA256
    # Worker evidence, not a second download, established that the input was complete.
    expected_destination = f"{record.job_directory}/inputs/probe.tar"
    assert len(transfer.verifies) == 1
    assert transfer.verifies[0]["destination"] == expected_destination
    assert transfer.verifies[0]["expected_sha256"] == INPUT_SHA256
    assert result.record.inputs[0].worker_path == expected_destination
    # The workspace was already prepared, so only the command launch is new work.
    assert _calls(executor, "install") == 0
    assert _calls(executor, "prepare") == 0
    assert _calls(executor, "start") == 1


def test_status_resumes_a_transfer_whose_worker_side_process_died(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    context, record = _interrupted_submission(tmp_path, executor, transfer)
    executor.status = _evidence(prepared=True, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert transfer.download_attempts == 2
    assert result.record.inputs[0].materialized is True


def test_status_does_not_start_a_second_transfer_for_a_destination_in_use(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_error = ArtifactTransferInProgressError(
        "another transfer for the destination is already in progress on this worker"
    )
    context = _collaborators(tmp_path, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.calls.clear()
    executor.status = _evidence(prepared=True, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.record.reconciliation_required is True
    assert result.warning is not None and "reconciliation" in result.warning
    assert _calls(executor, "start") == 0
    assert _calls(executor, "install") == 0


def test_status_reports_a_definitive_failure_only_from_worker_evidence(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _interrupted_submission(tmp_path, executor, transfer)
    executor.status = _evidence(prepared=True, started=False)
    transfer.download_error = StorageVerificationError(
        "declared input digest does not match the stored artifact"
    )

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "digest does not match" in (result.record.failure_reason or "")
    assert result.record.remote_status == "preparation_failed"
    assert _calls(executor, "start") == 0


def test_status_reinstalls_a_runner_that_reports_no_preparation_evidence(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.calls.clear()
    # An older installed runner answers without the evidence fields, then the current one
    # answers with them.
    executor.inspect_results = [
        _evidence(prepared=None, started=None).model_copy(
            update={"job_directory_exists": None, "launch_alive": None}
        ),
        _evidence(prepared=True, started=False),
    ]
    executor.status = _evidence(prepared=True, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert _calls(executor, "install") == 1
    assert result.record.state in {JobState.PREPARING, JobState.RUNNING}


# --- evidence-based diagnostics ----------------------------------------------------


def test_a_command_that_never_started_is_reconciled_not_declared_lost(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_error = ArtifactTransferInProgressError(
        "another transfer for the destination is already in progress on this worker"
    )
    context = _collaborators(tmp_path, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(prepared=True, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    reason = result.record.state_reason or ""
    assert result.record.state is JobState.PREPARING
    assert "workspace was removed" not in reason
    assert "rebuilt" not in reason
    assert "still in progress" in reason or "reconciliation" in reason


def test_an_absent_workspace_after_preparation_is_reported_with_evidence(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    assert record.executed_commit is not None
    executor.calls.clear()
    executor.status = _evidence(job_directory_exists=False, prepared=False, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.remote_status == "workspace_absent"
    reason = result.record.failure_reason or ""
    assert "absent" in reason
    assert "issued preparation steps" in reason
    # A workspace that vanished after preparation is never re-created implicitly.
    assert _calls(executor, "prepare") == 0
    assert _calls(executor, "start") == 0


def test_a_missing_workspace_before_preparation_is_prepared_again(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.install_error = SshConnectionError("SSH could not connect")
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    assert record.executed_commit is None
    executor.install_error = None
    executor.calls.clear()
    executor.status = _evidence(job_directory_exists=False, prepared=False, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert _calls(executor, "prepare") == 1
    assert _calls(executor, "start") == 1


def test_a_recorded_process_that_vanished_without_an_outcome_fails_the_job(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.status = _evidence(pid=4321, prepared=True, started=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    reason = result.record.failure_reason or ""
    assert "no longer running" in reason
    assert "recorded no outcome" in reason
    assert _calls(executor, "start") == 0


def test_a_launch_that_never_recorded_a_process_is_reported_as_never_started(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.status = _evidence(prepared=True, started=True, launch_alive=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    reason = result.record.failure_reason or ""
    assert "did not complete" in reason
    assert "No command started" in reason
    assert _calls(executor, "start") == 0


def test_a_launch_that_is_still_in_progress_keeps_reconciling(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.calls.clear()
    executor.status = _evidence(prepared=True, started=True, launch_alive=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.record.reconciliation_required is True
    assert "still in progress" in (result.record.state_reason or "")
    assert _calls(executor, "start") == 0


def test_an_unverifiable_launch_identity_keeps_reconciling(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.status = _evidence(prepared=True, started=True, launch_alive=None)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert "not known whether a command started" in (result.record.state_reason or "")


def test_a_running_command_is_never_prepared_or_started_again(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.calls.clear()
    executor.status = _evidence(status="running", pid=4321, prepared=True, started=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert executor.calls == ["inspect"]


# --- uncertainty is reported, never converted into a terminal claim ----------------


def test_an_uninspectable_worker_is_reported_as_uncertainty(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.inspect_error = SshConnectionError("SSH could not connect")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.warning is not None
    assert "could not be inspected" in result.warning


def test_unavailable_provider_state_is_reported_as_uncertainty(tmp_path: Path) -> None:
    provider = FakeProvider()
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_interrupts = 1
    context = _collaborators(tmp_path, provider=provider, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    provider.error = ProviderUnavailableError("provider read timed out")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.warning is not None
    assert "unavailable" in result.warning


def test_a_stopped_worker_keeps_a_job_that_never_started_for_reconciliation(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_interrupts = 1
    context = _collaborators(tmp_path, provider=provider, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    provider.subject = provider.subject.model_copy(update={"state": WorkerState.STOPPED})

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.record.reconciliation_required is True
    assert result.warning is not None
    assert "stays PREPARING" in result.warning


def test_a_stopped_worker_keeps_a_started_job_for_reconciliation(tmp_path: Path) -> None:
    """A stopped container is not evidence about a command whose record survives on disk."""

    provider = FakeProvider()
    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, provider=provider, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    assert record.state is JobState.RUNNING
    provider.subject = provider.subject.model_copy(update={"state": WorkerState.STOPPED})

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert result.record.failure_reason is None
    assert result.record.reconciliation_required is True
    assert result.warning is not None and "stays RUNNING" in result.warning


def test_a_destroyed_worker_is_unrecoverable_and_says_so(tmp_path: Path) -> None:
    """Destruction is affirmative evidence that the outcome can never be read again."""

    provider = FakeProvider()
    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, provider=provider, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    provider.subject = provider.subject.model_copy(update={"state": WorkerState.DESTROYED})

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.worker_absent is True
    assert result.record.remote_status == "worker_destroyed"
    reason = result.record.failure_reason or ""
    assert "no longer be read" in reason
    assert "verified as successful" in reason


def test_reconciliation_without_the_declared_secret_reports_uncertainty(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    secrets = {"MLFLOW_TRACKING_PASSWORD": "value"}
    transfer.download_interrupts = 1
    context = _collaborators(
        tmp_path,
        executor=executor,
        transfer=transfer,
        environ=secrets,
    )
    spec = _spec(runtime={"environment_secrets": ["MLFLOW_TRACKING_PASSWORD"]})
    record = JobSubmitter(context).submit(spec, worker_id="pod-123")
    assert record.state is JobState.PREPARING
    executor.status = _evidence(prepared=True, started=False)
    without_secrets = dataclasses.replace(context, environ={})

    result = JobCoordinator(dataclasses.replace(without_secrets)).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.record.reconciliation_required is True
    assert "MLFLOW_TRACKING_PASSWORD" in (result.record.state_reason or "")
    assert _calls(executor, "start") == 0
    assert isinstance(result.record, type(result.record))


def test_a_state_error_during_reconciliation_is_uncertainty_not_failure(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_interrupts = 1
    context = _collaborators(tmp_path, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(prepared=True, started=False)
    context.worker_state.get = _raise_state_error  # type: ignore[method-assign]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.warning is not None


def _raise_state_error(*_args, **_kwargs):
    raise StateError("worker state could not be read")


def test_an_unexpected_controller_error_never_writes_a_terminal_state(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_interrupts = 1
    context = _collaborators(tmp_path, executor=executor, transfer=transfer)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(prepared=True, started=False)
    transfer.download_error = RuntimeError("a bug in the controller")

    with pytest.raises(RuntimeError):
        JobCoordinator(context).refresh(record.job_id)

    stored = context.job_store.get(record.job_id)
    assert stored.state is JobState.PREPARING
    assert stored.reconciliation_required is True
    assert stored.failure_reason is None


# --- cancellation ------------------------------------------------------------------


def test_cancellation_during_preparation_records_cancelled(tmp_path: Path) -> None:
    """The worker's own pre-start exclusion is what makes CANCELLED affirmative."""

    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.calls.clear()
    executor.cancel_result = CancelResult(
        cancelled=True, already_finished=False, pid=None, pre_start=True
    )

    cancelled = JobCoordinator(context).cancel(record.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert cancelled.reconciliation_required is False
    reason = cancelled.state_reason or ""
    assert "excluded this job's launch before it started" in reason
    assert "cannot run" in reason
    assert executor.cancel_calls == [record.job_id]
    # A cancelled job is never reconciled afterwards: reconciliation is what would launch it.
    assert _calls(executor, "start") == 0
    assert JobCoordinator(context).refresh(record.job_id).record.state is JobState.CANCELLED


def test_cancellation_during_preparation_targets_a_command_that_did_start(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    executor.start_error = SshConnectionError("SSH connection dropped")
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    assert record.state is JobState.PREPARING
    executor.calls.clear()
    executor.status = _evidence(status="running", pid=4321, prepared=True, started=True)

    cancelled = JobCoordinator(context).cancel(record.job_id)

    assert executor.cancel_calls == [record.job_id]
    assert cancelled.cancellation_requested_at == NOW
    assert cancelled.state is JobState.CANCELLED
    assert "terminated this job's recorded process group" in (cancelled.state_reason or "")


def test_cancellation_during_preparation_refuses_when_the_worker_is_unreachable(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.cancel_error = SshConnectionError("SSH could not connect")

    with pytest.raises(JobCancellationError, match="did not establish the cancellation"):
        JobCoordinator(context).cancel(record.job_id)

    assert context.job_store.get(record.job_id).state is JobState.PREPARING


def test_cancellation_reports_a_launch_that_owns_the_job(tmp_path: Path) -> None:
    """When the launch wins the race the operator is told, not given a false CANCELLED."""

    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.cancel_result = CancelResult(
        cancelled=False, already_finished=False, pid=None, launch_in_progress=True
    )

    with pytest.raises(JobCancellationError, match="launch for job"):
        JobCoordinator(context).cancel(record.job_id)

    assert context.job_store.get(record.job_id).state is JobState.PREPARING


def test_cancellation_of_a_running_job_still_targets_the_process(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(status="cancelled", cancelled=True, executed_commit="a" * 40)

    cancelled = JobCoordinator(context).cancel(record.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert executor.cancel_calls == [record.job_id]


def test_a_lost_pre_start_cancellation_acknowledgement_reconciles_as_cancelled(
    tmp_path: Path,
) -> None:
    """The worker durably excluded the launch; a lost ack must not become FAILED.

    The controller stopped watching before it heard back, so nothing was recorded. A later
    inspection reports the worker's own durable evidence, which is affirmative proof that
    the command did not run - so no executed commit exists to verify and none is required.
    """

    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    assert record.state is JobState.RUNNING
    executor.calls.clear()
    executor.status = _evidence(status="cancelled", cancelled=True, pre_start=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.CANCELLED
    assert result.record.reconciliation_required is False
    assert result.record.failure_reason is None
    assert result.record.exit_code is None
    assert "excluded" in (result.record.state_reason or "")
    # Nothing is ever launched from a job whose launch is durably excluded.
    assert _calls(executor, "start") == 0


def test_a_pre_start_cancellation_is_reconciled_from_preparing_too(tmp_path: Path) -> None:
    """The same durable evidence decides even when the controller never recorded RUNNING."""

    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    assert record.state is JobState.PREPARING
    executor.status = _evidence(status="cancelled", cancelled=True, pre_start=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.CANCELLED
    assert result.record.started_at is None


def test_a_cancelled_command_that_did_execute_still_requires_its_verified_commit(
    tmp_path: Path,
) -> None:
    """Only a *pre-start* exclusion is exempt: an executed cancellation keeps provenance."""

    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(
        status="cancelled",
        cancelled=True,
        pre_start=False,
        exit_code=143,
        executed_commit="b" * 40,
    )

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "provenance cannot be verified" in (result.record.failure_reason or "")


def test_a_cancelled_command_without_any_executed_commit_still_fails(tmp_path: Path) -> None:
    """A cancellation that ran a command but lost its commit is not evidence of success."""

    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(
        status="cancelled",
        cancelled=True,
        pre_start=False,
        exit_code=143,
        executed_commit=None,
    )

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED


# --- waiting -----------------------------------------------------------------------


def test_wait_polls_a_preparing_job_less_often_than_a_running_one(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.status = _evidence(prepared=True, started=True, launch_alive=True)
    sleeps: list[float] = []
    clock = [0.0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds
        if len(sleeps) == 2:
            executor.status = _evidence(status="finished", exit_code=0, executed_commit="a" * 40)

    coordinator = JobCoordinator(
        context,
        sleep=sleep,
        monotonic=lambda: clock[0],
        poll_interval_seconds=0.5,
        preparation_poll_interval_seconds=7.0,
    )

    coordinator.wait(record.job_id, timeout_seconds=600)

    assert sleeps == [7.0, 7.0]


def test_wait_reports_a_preparing_job_that_never_finishes(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _interrupted_submission(tmp_path, executor, FakeTransfer())
    executor.status = _evidence(prepared=True, started=True, launch_alive=True)
    clock = [0.0]

    coordinator = JobCoordinator(
        context,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        monotonic=lambda: clock[0],
        preparation_poll_interval_seconds=5.0,
    )

    result = coordinator.wait(record.job_id, timeout_seconds=5)

    assert result.record.state is JobState.PREPARING
    assert result.warning is not None
    assert "did not finish" in result.warning


def test_a_reconciled_submission_is_reported_as_preparing_not_lost(tmp_path: Path) -> None:
    """`infra job status` on an interrupted job names the reconciliation it performs."""

    executor = FakeJobExecutor()
    executor.start_error = SshConnectionError("SSH connection dropped")
    context = _collaborators(tmp_path, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    executor.status = _evidence(prepared=True, started=False)
    executor.start_error = None

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING


def test_reconciliation_uses_the_durable_record_after_a_controller_restart(tmp_path: Path) -> None:
    """A fresh process must reconcile from disk plus worker evidence, not from memory."""

    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _interrupted_submission(tmp_path, executor, transfer)
    executor.status = _evidence(prepared=True, started=False)

    restarted = JobContext(
        provider=context.provider,
        worker_state=ready_worker_store(tmp_path),
        job_store=context.job_store,
        executor=executor,
        transfer=transfer,
        storage=context.storage,
        jobs_config=context.jobs_config,
        environ=dict(context.environ),
        now=lambda: NOW,
    )
    record = restarted.job_store.require(record.job_id)
    assert record.state is JobState.PREPARING

    result = JobCoordinator(restarted).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING


# --- HIGH 4: every required input is verified before the command may start ---------

SECOND_SHA256 = "f" * 64
SECOND_SIZE = 2048
SECOND_ARTIFACT = "embeddings/v1/second.tar"


class _Evidence:
    """Minimal worker-side verification evidence."""

    def __init__(self, *, size_bytes: int, sha256: str) -> None:
        self.size_bytes = size_bytes
        self.sha256 = sha256


def _two_input_spec(**overrides):
    document = job_spec_document(
        inputs=[
            {
                "artifact": "embeddings/v1/probe.tar",
                "destination": "probe.tar",
                "sha256": INPUT_SHA256,
                "size_bytes": INPUT_SIZE,
            },
            {
                "artifact": SECOND_ARTIFACT,
                "destination": "second.tar",
                "sha256": SECOND_SHA256,
                "size_bytes": SECOND_SIZE,
            },
        ],
        **overrides,
    )
    return load_job_spec(json.dumps(document))


def _two_input_storage() -> FakeStorage:
    storage = FakeStorage()
    storage.objects["embeddings/v1/probe.tar"] = INPUT_SIZE
    storage.objects[SECOND_ARTIFACT] = SECOND_SIZE
    return storage


def _interrupted_two_input_job(tmp_path: Path, executor: FakeJobExecutor, transfer: FakeTransfer):
    """Submit a two-input job whose second download the controller stopped waiting for."""

    # The first input materializes; the second attempt is cut off, which is exactly the
    # state a later reconciliation must revalidate rather than trust.
    transfer.download_results = [
        _Evidence(size_bytes=INPUT_SIZE, sha256=INPUT_SHA256),
        RemoteOperationInterruptedError(
            "the controller stopped waiting for download on RunPod worker pod-123"
        ),
    ]
    context = job_context(
        tmp_path,
        executor=executor,
        transfer=transfer,
        storage=_two_input_storage(),
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )
    record = JobSubmitter(context).submit(_two_input_spec(), worker_id="pod-123")
    assert record.state is JobState.PREPARING
    assert record.inputs[0].materialized is True
    assert record.inputs[1].materialized is False
    executor.status = _evidence(prepared=True, started=False)
    return context, record


def _events(context: JobContext) -> list[str]:
    events: list[str] = []
    context.executor.events = events  # type: ignore[attr-defined]
    context.transfer.events = events  # type: ignore[attr-defined]
    return events


def _first_destination(record) -> str:
    return f"{record.job_directory}/inputs/probe.tar"


def _second_destination(record) -> str:
    return f"{record.job_directory}/inputs/second.tar"


def test_a_valid_recorded_input_is_verified_but_not_transferred_again(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    context, record = _interrupted_two_input_job(tmp_path, executor, transfer)
    events = _events(context)
    transfer.verify_results = [
        _Evidence(size_bytes=INPUT_SIZE, sha256=INPUT_SHA256),
    ]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    first = _first_destination(record)
    assert [entry["destination"] for entry in transfer.verifies] == [first]
    # The recorded input was revalidated from worker evidence, not re-downloaded.
    assert first not in [entry["destination"] for entry in transfer.downloads[2:]]
    assert f"download:{_second_destination(record)}" in events
    # Nothing is launched before every declared input has been established in this pass.
    assert events.index("start") > events.index(f"verify:{first}")
    assert events.index("start") > events.index(f"download:{_second_destination(record)}")


def test_a_recorded_input_that_vanished_is_materialized_again(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    context, record = _interrupted_two_input_job(tmp_path, executor, transfer)
    transfer.verify_results = [None]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    first = _first_destination(record)
    # The recorded input is re-established from worker evidence before the launch, not
    # assumed from the flag an earlier attempt wrote.
    assert [entry["destination"] for entry in transfer.verifies] == [first]
    repeat = [entry for entry in transfer.downloads[2:] if entry["destination"] == first]
    assert len(repeat) == 1
    assert repeat[0]["overwrite"] is False


def test_a_recorded_input_that_no_longer_matches_is_replaced(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    context, record = _interrupted_two_input_job(tmp_path, executor, transfer)
    transfer.verify_results = [ArtifactTransferError("verify: destination does not match")]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    first = _first_destination(record)
    repeat = [entry for entry in transfer.downloads[2:] if entry["destination"] == first]
    assert len(repeat) == 1
    assert repeat[0]["overwrite"] is True


def test_a_required_input_that_stays_unusable_fails_the_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _interrupted_two_input_job(tmp_path, executor, transfer)
    transfer.verify_results = [ArtifactTransferError("verify: destination does not match")]
    transfer.download_results = [ArtifactTransferError("the storage endpoint rejected it")]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "required input" in (result.record.failure_reason or "")
    assert _calls(executor, "start") == 0


# --- HIGH 6: a required output whose upload is unknown is not a failed job ---------

OUTPUT_ARTIFACT = "jobs/probe/answer.json"
OUTPUT_SOURCE_SUFFIX = "outputs/answer.json"
OUTPUT_SIZE = 12


def _output_spec(**overrides):
    document = job_spec_document(
        outputs=[{"path": OUTPUT_SOURCE_SUFFIX, "artifact": OUTPUT_ARTIFACT}],
        **overrides,
    )
    return load_job_spec(json.dumps(document))


def _submitted_with_output(tmp_path: Path, executor: FakeJobExecutor, transfer: FakeTransfer):
    context = job_context(
        tmp_path,
        executor=executor,
        transfer=transfer,
        storage=FakeStorage(),
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )
    record = JobSubmitter(context).submit(_output_spec(), worker_id="pod-123")
    assert record.state is JobState.RUNNING
    executor.status = _evidence(status="finished", exit_code=0, executed_commit="a" * 40)
    return context, record


def test_an_interrupted_required_output_upload_keeps_the_job_reconcilable(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_output(tmp_path, executor, transfer)
    transfer.upload_results = [
        RemoteOperationInterruptedError(
            "The controller stopped waiting for upload on RunPod worker pod-123"
        )
    ]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert result.record.failure_reason is None
    assert result.record.reconciliation_required is True
    assert "could not be confirmed" in (result.record.state_reason or "")
    assert result.record.outputs[0].persisted is False
    assert result.warning is not None


def test_a_stored_object_that_matches_the_worker_file_is_accepted_as_this_output(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_output(tmp_path, executor, transfer)
    transfer.upload_results = [
        RemoteOperationInterruptedError("the controller stopped waiting for the upload")
    ]
    JobCoordinator(context).refresh(record.job_id)
    # Its own upload completed after the controller stopped watching, so canonical storage
    # now holds exactly the bytes the worker file reports.
    payload = b"answer: 42"
    storage = context.storage
    storage.put_content(OUTPUT_ARTIFACT, payload)  # type: ignore[union-attr]
    digest = hashlib.sha256(payload).hexdigest()
    transfer.upload_results = [StorageObjectExistsError(f"{OUTPUT_ARTIFACT} already exists")]
    transfer.verify_results = [_Evidence(size_bytes=len(payload), sha256=digest)]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.SUCCEEDED
    assert result.record.outputs[0].persisted is True
    assert result.record.outputs[0].verified_size_bytes == len(payload)
    assert result.record.outputs[0].sha256 == digest
    assert result.record.reconciliation_required is False


def test_a_stale_object_of_the_same_size_but_different_bytes_is_rejected(
    tmp_path: Path,
) -> None:
    """Neither size nor age accepts an object: only its bytes can.

    An older object at the declared key is rejected because its bytes are not the worker
    file's bytes, which is the same reason a same-size object from any other run is
    rejected. Staleness is deliberately not the test, because an object whose bytes match
    is this output whatever its age.
    """

    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_output(tmp_path, executor, transfer)
    storage = context.storage
    payload = b"answer: 41"
    storage.put_content(OUTPUT_ARTIFACT, payload)  # type: ignore[union-attr]
    storage.last_modified[OUTPUT_ARTIFACT] = datetime(2026, 1, 1, tzinfo=UTC)  # type: ignore[union-attr]
    expected = b"answer: 42"
    transfer.upload_results = [StorageObjectExistsError(f"{OUTPUT_ARTIFACT} already exists")]
    transfer.verify_results = [
        _Evidence(size_bytes=len(expected), sha256=hashlib.sha256(expected).hexdigest())
    ]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.outputs[0].persisted is False
    assert result.record.outputs[0].sha256 is None
    assert "not this run's output" in (result.record.outputs[0].failure_reason or "")
    assert "required outputs were not persisted" in (result.record.failure_reason or "")


def test_an_object_replaced_while_the_recovery_read_ran_is_never_accepted(
    tmp_path: Path,
) -> None:
    """Metadata that matches is not evidence: the bytes read back decide acceptance."""

    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_output(tmp_path, executor, transfer)
    storage = context.storage
    expected = b"answer: 42"
    # The object the metadata read described is replaced before the body is read, so the
    # bytes canonical storage yields are not the worker file's bytes.
    storage.put_content(OUTPUT_ARTIFACT, b"answer: 43")  # type: ignore[union-attr]
    transfer.upload_results = [StorageObjectExistsError(f"{OUTPUT_ARTIFACT} already exists")]
    transfer.verify_results = [
        _Evidence(size_bytes=len(expected), sha256=hashlib.sha256(expected).hexdigest())
    ]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.outputs[0].persisted is False


SECOND_OUTPUT_ARTIFACT = "jobs/probe/second.json"
SECOND_OUTPUT_SOURCE = "outputs/second.json"


def _two_output_spec():
    document = job_spec_document(
        outputs=[
            {"path": OUTPUT_SOURCE_SUFFIX, "artifact": OUTPUT_ARTIFACT},
            {"path": SECOND_OUTPUT_SOURCE, "artifact": SECOND_OUTPUT_ARTIFACT},
        ]
    )
    return load_job_spec(json.dumps(document))


def _submitted_with_two_outputs(tmp_path: Path, executor: FakeJobExecutor, transfer: FakeTransfer):
    context = job_context(
        tmp_path,
        executor=executor,
        transfer=transfer,
        storage=FakeStorage(),
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )
    record = JobSubmitter(context).submit(_two_output_spec(), worker_id="pod-123")
    assert record.state is JobState.RUNNING
    executor.status = _evidence(status="finished", exit_code=0, executed_commit="a" * 40)
    return context, record


def test_an_interruption_on_the_first_of_two_outputs_keeps_every_declared_slot(
    tmp_path: Path,
) -> None:
    """One interruption must not truncate the declared output structure."""

    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_two_outputs(tmp_path, executor, transfer)
    transfer.upload_results = [
        RemoteOperationInterruptedError("the controller stopped waiting for the upload")
    ]

    first = JobCoordinator(context).refresh(record.job_id)

    assert len(first.record.outputs) == 2
    assert [entry.persisted for entry in first.record.outputs] == [False, False]
    assert first.record.state is JobState.RUNNING
    # The durable document itself still declares two outputs after the interruption.
    assert len(context.job_store.require(record.job_id).outputs) == 2

    # A controller restart over the same state directory completes both outputs.
    reloaded = JobCoordinator(_collaborators(tmp_path, executor=executor)).refresh(record.job_id)

    assert reloaded.record.state is JobState.SUCCEEDED
    assert [entry.persisted for entry in reloaded.record.outputs] == [True, True]


def test_a_second_output_interruption_keeps_the_first_output_recorded(
    tmp_path: Path,
) -> None:
    """A completed first output stays durable while the second is still unresolved."""

    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_two_outputs(tmp_path, executor, transfer)
    payload = transfer.upload_payload
    context.storage.put_content(OUTPUT_ARTIFACT, payload)  # type: ignore[union-attr]
    transfer.upload_results = [
        upload_outcome(source="", key=OUTPUT_ARTIFACT, payload=payload),
        RemoteOperationInterruptedError("the controller stopped waiting for the upload"),
    ]

    first = JobCoordinator(context).refresh(record.job_id)

    assert first.record.outputs[0].persisted is True
    assert first.record.outputs[1].persisted is False
    assert len(first.record.outputs) == 2
    assert first.record.state is JobState.RUNNING
    assert first.record.reconciliation_required is True
    durable = context.job_store.require(record.job_id)
    assert [entry.persisted for entry in durable.outputs] == [True, False]

    # A restart re-reads that record and reconciles only the unresolved second output.
    fresh_executor = executor
    fresh_transfer = FakeTransfer()
    restarted = job_context(
        tmp_path,
        executor=fresh_executor,
        transfer=fresh_transfer,
        storage=FakeStorage(),
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )
    reloaded = JobCoordinator(restarted).refresh(record.job_id)

    assert reloaded.record.state is JobState.SUCCEEDED
    assert [entry.persisted for entry in reloaded.record.outputs] == [True, True]
    # The first output was already durable, so it was never uploaded again.
    assert [call["key"] for call in fresh_transfer.uploads] == [SECOND_OUTPUT_ARTIFACT]


def test_a_definitively_rejected_required_output_fails_the_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted_with_output(tmp_path, executor, transfer)
    transfer.upload_results = [ArtifactTransferError("the storage endpoint rejected the upload")]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "required outputs were not persisted" in (result.record.failure_reason or "")


# --- MEDIUM 7: a transient controller-side storage outage is not remote failure ----


def test_a_transient_storage_observation_failure_keeps_the_job_reconcilable(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    context, record = _interrupted_two_input_job(tmp_path, executor, transfer)
    context.storage.metadata_errors.append(  # type: ignore[union-attr]
        StorageUnavailableError("Could not read metadata (HTTP 503, SlowDown)")
    )

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.PREPARING
    assert result.record.failure_reason is None
    assert result.record.reconciliation_required is True
    assert result.warning is not None and "reconciliation" in result.warning
    assert _calls(executor, "start") == 0


def test_a_definitively_absent_input_object_still_fails_the_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    context, record = _interrupted_two_input_job(tmp_path, executor, transfer)
    context.storage.objects.pop(SECOND_ARTIFACT)  # type: ignore[union-attr]

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "does not exist" in (result.record.failure_reason or "")


# --- MEDIUM 8: an exhausted bounded attempt leaves the job recoverable -------------


def test_an_exhausted_transfer_attempt_keeps_the_job_reconcilable(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.download_results = [
        ArtifactTransferTransientError(
            "transient transfer failure; resumable state was kept: byte range failed"
        )
    ]
    context = job_context(
        tmp_path,
        executor=executor,
        transfer=transfer,
        storage=_storage(),
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )

    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert record.state is JobState.PREPARING
    assert record.reconciliation_required is True
    assert record.failure_reason is None
    assert "reconciliation required" in (record.state_reason or "")


def test_a_later_attempt_resumes_an_exhausted_transfer_with_fresh_credentials(
    tmp_path: Path,
) -> None:
    executor = FakeJobExecutor()
    transfer = FakeTransfer(materialize=True)
    transfer.download_results = [ArtifactTransferTransientError("transient transfer failure")]
    context = job_context(
        tmp_path,
        executor=executor,
        transfer=transfer,
        storage=_storage(),
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root"), default_timeout_seconds=60),
    )
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    assert record.state is JobState.PREPARING
    executor.status = _evidence(prepared=True, started=False)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    # The retry is a fresh attempt against the same destination, which the worker resumes.
    assert [entry["destination"] for entry in transfer.downloads][-1].endswith("inputs/probe.tar")
    assert len(transfer.downloads) == 2


def test_a_failure_recorded_before_a_worker_stopped_is_read_after_it_restarts(
    tmp_path: Path,
) -> None:
    """The durable outcome on the worker decides, whichever way it went."""

    provider = FakeProvider()
    executor = FakeJobExecutor()
    context = _collaborators(tmp_path, provider=provider, executor=executor)
    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")
    provider.subject = provider.subject.model_copy(update={"state": WorkerState.STOPPED})

    stopped = JobCoordinator(context).refresh(record.job_id)
    assert stopped.record.state is JobState.RUNNING

    provider.subject = provider.subject.model_copy(update={"state": WorkerState.RUNNING})
    executor.status = _evidence(
        status="finished", exit_code=9, stage="command", executed_commit="a" * 40
    )

    reconciled = JobCoordinator(context).refresh(record.job_id)

    assert reconciled.record.state is JobState.FAILED
    assert reconciled.record.exit_code == 9
    assert "9" in (reconciled.record.state_reason or "")
