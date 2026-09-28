"""Submission, status reconciliation, output persistence, and cancellation."""

from __future__ import annotations

import json
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
    worker,
    worker_record,
)

from wavcse_infra.errors import (
    ArtifactTransferError,
    ConfigurationError,
    JobCancellationError,
    JobExecutionError,
    JobPreconditionError,
    ProviderNotFoundError,
    ProviderUnavailableError,
    StorageVerificationError,
)
from wavcse_infra.jobs.context import require_storage
from wavcse_infra.jobs.execution import CancelResult, RemoteJobStatus
from wavcse_infra.jobs.models import JobState, load_job_spec
from wavcse_infra.jobs.status import JobCoordinator
from wavcse_infra.jobs.submit import JobSubmitter, infra_environment, resolve_secrets
from wavcse_infra.models import WorkerReadinessState, WorkerState

SECRET = "super-secret-value"


def _spec(**overrides):
    return load_job_spec(json.dumps(job_spec_document(**overrides)))


def _specless_context(tmp_path: Path, **overrides):
    return job_context(tmp_path, environ={"MLFLOW_TRACKING_PASSWORD": SECRET}, **overrides)


def _submitted(tmp_path: Path, spec=None, **context_overrides):
    """Submit one job against a scripted worker and return the durable record."""

    context = _specless_context(tmp_path, **context_overrides)
    record = JobSubmitter(context).submit(
        spec if spec is not None else _spec(), worker_id="pod-123"
    )
    return context, record


def _state_files(context) -> list[Path]:
    return sorted(context.job_store.directory.glob("job-*"))


def _state_text(context) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in _state_files(context))


# --- submission preconditions -----------------------------------------------------


def test_submission_requires_an_existing_worker(tmp_path: Path) -> None:
    provider = FakeProvider()
    provider.error = ProviderNotFoundError("pod-123 not found")
    context = _specless_context(tmp_path, provider=provider)

    with pytest.raises(JobPreconditionError, match="does not exist"):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert context.job_store.list_records() == []


def test_submission_requires_a_running_worker(tmp_path: Path) -> None:
    context = _specless_context(tmp_path, provider=FakeProvider(worker(state=WorkerState.STOPPED)))

    with pytest.raises(JobPreconditionError, match="requires a"):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert context.job_store.list_records() == []


def test_submission_requires_local_readiness_rather_than_implicit_bootstrap(tmp_path: Path) -> None:
    record = worker_record().model_copy(update={"readiness_state": WorkerReadinessState.NOT_READY})
    store = ready_worker_store(tmp_path, record)
    context = _specless_context(tmp_path, worker_store=store)

    with pytest.raises(JobPreconditionError, match="infra worker bootstrap"):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")


def test_submission_requires_an_untracked_worker_to_be_bootstrapped(tmp_path: Path) -> None:
    store = ready_worker_store(tmp_path)
    store.path.unlink()
    context = _specless_context(tmp_path, worker_store=store)

    with pytest.raises(JobPreconditionError, match="not tracked locally"):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")


def test_missing_declared_secret_fails_before_anything_is_recorded(tmp_path: Path) -> None:
    spec = _spec(runtime={"environment_secrets": ["MLFLOW_TRACKING_PASSWORD"]})
    context = job_context(tmp_path, environ={})

    with pytest.raises(JobPreconditionError, match="MLFLOW_TRACKING_PASSWORD"):
        JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert context.job_store.list_records() == []


def test_resolve_secrets_reads_only_present_values() -> None:
    spec = _spec(runtime={"environment_secrets": ["MLFLOW_TRACKING_PASSWORD"]})

    assert resolve_secrets(spec, {"MLFLOW_TRACKING_PASSWORD": SECRET}) == {
        "MLFLOW_TRACKING_PASSWORD": SECRET
    }

    with pytest.raises(JobPreconditionError):
        resolve_secrets(spec, {"MLFLOW_TRACKING_PASSWORD": ""})


def test_infra_environment_carries_provenance_not_credentials() -> None:
    values = infra_environment(
        worker(),
        worker_record(),
        job_id="job-0123456789abcdef",
        commit="a" * 40,
    )

    assert values["INFRA_PROVIDER"] == "runpod"
    assert values["INFRA_WORKER_ID"] == "pod-123"
    assert values["INFRA_GIT_COMMIT"] == "a" * 40
    assert values["INFRA_JOB_ID"] == "job-0123456789abcdef"
    assert values["INFRA_GPU"] == "NVIDIA RTX A5000"
    assert values["INFRA_GPU_COUNT"] == "1"
    assert SECRET not in json.dumps(values)


# --- submission happy path --------------------------------------------------------


def test_submission_installs_prepares_downloads_then_starts(tmp_path: Path) -> None:
    spec = _spec(
        runtime={
            "environment": {"PYTHONUNBUFFERED": "1"},
            "environment_secrets": ["MLFLOW_TRACKING_PASSWORD"],
            "timeout_seconds": 600,
        },
        inputs=[
            {"artifact": "embeddings/v1/a.tar", "destination": "nested/a.tar", "sha256": "b" * 64}
        ],
        outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"}],
    )
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 11
    context, record = _submitted(
        tmp_path, spec, executor=executor, transfer=transfer, storage=storage
    )

    assert record.state is JobState.RUNNING
    assert record.executed_commit == "a" * 40
    assert record.pid == 4321
    assert record.requested_commit == "a" * 40
    assert executor.calls == ["install", "prepare", "start"]
    assert executor.install_calls == ["pod-123"]
    assert executor.prepare_calls[0]["input_destinations"] == ("nested/a.tar",)
    assert transfer.downloads[0]["destination"].endswith("/inputs/nested/a.tar")
    assert transfer.downloads[0]["expected_size"] == 11
    assert executor.start_calls[0]["secrets"] == {"MLFLOW_TRACKING_PASSWORD": SECRET}
    assert executor.start_calls[0]["timeout_seconds"] == 600
    assert executor.start_calls[0]["environment"] == {"PYTHONUNBUFFERED": "1"}
    assert executor.start_calls[0]["argv"] == ["uv", "run", "python", "train.py", "--seed", "42"]
    assert executor.start_calls[0]["infra_environment"]["INFRA_GIT_COMMIT"] == "a" * 40
    assert record.inputs[0].materialized is True
    assert record.inputs[0].size_bytes == 11
    assert record.provenance.worker_bootstrap_version == "1"
    assert record.provenance.gpu_models == ("NVIDIA RTX A5000",)
    assert record.provenance.mlflow_owner == "wavCSE"
    assert len(_state_files(context)) == 1
    assert SECRET not in _state_text(context)


def test_submission_uses_explicit_digest_and_manifest_expectations(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[
            {
                "artifact": "embeddings/v1/a.tar",
                "destination": "a.tar",
                "sha256": "d" * 64,
                "size_bytes": 11,
            },
            {
                "artifact": "embeddings/v1/b.tar",
                "destination": "b.tar",
                "manifest": "embeddings/v1/b.manifest.json",
            },
        ]
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 11
    storage.objects["embeddings/v1/b.tar"] = 22
    storage.manifests["embeddings/v1/b.manifest.json"] = type(
        "Manifest",
        (),
        {"object_key": "embeddings/v1/b.tar", "size_bytes": 22, "sha256": "e" * 64},
    )()
    transfer = FakeTransfer()
    _, _ = _submitted(tmp_path, spec, storage=storage, transfer=transfer)

    assert transfer.downloads[0]["expected_sha256"] == "d" * 64
    assert transfer.downloads[1]["expected_size"] == 22
    assert transfer.downloads[1]["expected_sha256"] == "e" * 64


def test_optional_input_without_digest_is_verified_against_the_stored_size(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[{"artifact": "embeddings/v1/a.tar", "destination": "a.tar", "required": False}]
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 4096
    transfer = FakeTransfer()
    _submitted(tmp_path, spec, storage=storage, transfer=transfer)

    assert transfer.downloads[0]["expected_size"] == 4096
    assert transfer.downloads[0]["expected_sha256"] is None


def test_declared_size_that_contradicts_storage_prevents_the_job(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[
            {
                "artifact": "embeddings/v1/a.tar",
                "destination": "a.tar",
                "size_bytes": 10,
                "sha256": "b" * 64,
            },
        ]
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 11
    executor = FakeJobExecutor()
    context = _specless_context(tmp_path, storage=storage, executor=executor)

    with pytest.raises(JobPreconditionError, match="could not be materialized"):
        JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert "start" not in executor.calls
    record = context.job_store.list_records()[0]
    assert record.state is JobState.FAILED
    assert record.inputs[0].materialized is False


def test_manifest_that_describes_another_object_prevents_the_job(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[
            {
                "artifact": "embeddings/v1/a.tar",
                "destination": "a.tar",
                "manifest": "embeddings/v1/a.manifest.json",
            }
        ]
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 11
    storage.manifests["embeddings/v1/a.manifest.json"] = type(
        "Manifest",
        (),
        {"object_key": "embeddings/v1/other.tar", "size_bytes": 11, "sha256": "e" * 64},
    )()
    executor = FakeJobExecutor()
    context = _specless_context(tmp_path, storage=storage, executor=executor)

    with pytest.raises(JobPreconditionError):
        JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert "start" not in executor.calls
    assert context.job_store.list_records()[0].state is JobState.FAILED


def test_missing_artifact_prevents_the_job(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[
            {"artifact": "embeddings/v1/missing.tar", "destination": "a.tar", "sha256": "b" * 64}
        ]
    )
    executor = FakeJobExecutor()
    context = _specless_context(tmp_path, executor=executor, storage=FakeStorage())

    with pytest.raises(JobPreconditionError) as error:
        JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert "does not exist" in str(error.value)
    assert "start" not in executor.calls


def test_optional_input_failure_does_not_prevent_execution(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[
            {"artifact": "embeddings/v1/a.tar", "destination": "a.tar", "required": False},
            {"artifact": "embeddings/v1/b.tar", "destination": "b.tar", "sha256": "b" * 64},
        ]
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 1
    storage.objects["embeddings/v1/b.tar"] = 2
    transfer = FakeTransfer()
    original = transfer.download

    def flaky(worker_id: str, **kwargs):
        if "a.tar" in kwargs["destination"]:
            raise ArtifactTransferError("transfer refused")
        return original(worker_id, **kwargs)

    transfer.download = flaky  # type: ignore[method-assign]
    executor = FakeJobExecutor()
    context = _specless_context(tmp_path, storage=storage, transfer=transfer, executor=executor)

    record = JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert record.state is JobState.RUNNING
    assert record.inputs[0].materialized is False
    assert record.inputs[1].materialized is True
    assert "start" in executor.calls


def test_submission_without_storage_configuration_works_when_nothing_is_declared(
    tmp_path: Path,
) -> None:
    context = _specless_context(tmp_path, storage=None)
    assert context.storage is None
    with pytest.raises(ConfigurationError, match=r"storage\.bucket"):
        require_storage(context)

    record = JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    assert record.state is JobState.RUNNING


def test_prepare_failure_is_recorded_as_failed_without_starting(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.prepare_error = JobExecutionError("worker does not contain commit")
    context = _specless_context(tmp_path, executor=executor)

    with pytest.raises(JobExecutionError):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    record = context.job_store.list_records()[0]
    assert record.state is JobState.FAILED
    assert "does not contain commit" in (record.failure_reason or "")
    assert "start" not in executor.calls


def test_start_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.start_error = JobExecutionError("job workspace is missing")
    context = _specless_context(tmp_path, executor=executor)

    with pytest.raises(JobExecutionError):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    record = context.job_store.list_records()[0]
    assert record.state is JobState.FAILED
    assert record.executed_commit == "a" * 40
    assert record.finished_at is None


# --- status reconciliation --------------------------------------------------------


def test_status_reconciles_a_running_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="running", pid=4321, log_bytes=2048)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.warning is None
    assert result.record.state is JobState.RUNNING
    assert result.record.log_bytes == 2048


def test_status_marks_success_only_after_required_outputs_are_persisted(tmp_path: Path) -> None:
    spec = _spec(outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"}])
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted(tmp_path, spec, executor=executor, transfer=transfer)
    executor.status = RemoteJobStatus(status="finished", exit_code=0, log_bytes=64)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.SUCCEEDED
    assert transfer.uploads[0]["key"] == "jobs/run-1/metrics.json"
    assert transfer.uploads[0]["source"].endswith("/outputs/metrics.json")
    assert result.record.outputs[0].persisted is True
    assert result.record.outputs[0].sha256 == transfer.upload_digest
    assert result.record.outputs[0].verified_size_bytes == 7
    assert result.record.exit_code == 0
    assert context.job_store.read_log(record.job_id) == "job output\n"


def test_required_output_upload_failure_prevents_success(tmp_path: Path) -> None:
    spec = _spec(outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"}])
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.upload_error = ArtifactTransferError("upload source is not a regular file")
    context, record = _submitted(tmp_path, spec, executor=executor, transfer=transfer)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "required outputs were not persisted" in (result.record.failure_reason or "")
    assert result.record.outputs[0].persisted is False
    assert "not a regular file" in (result.record.outputs[0].failure_reason or "")


def test_optional_output_failure_does_not_block_success(tmp_path: Path) -> None:
    spec = _spec(
        outputs=[
            {
                "path": "outputs/optional.json",
                "artifact": "jobs/run-1/optional.json",
                "required": False,
            }
        ]
    )
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.upload_error = ArtifactTransferError("transfer refused")
    context, record = _submitted(tmp_path, spec, executor=executor, transfer=transfer)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.SUCCEEDED
    assert result.record.outputs[0].persisted is False


def test_scientific_failure_is_preserved_with_its_exit_code(tmp_path: Path) -> None:
    spec = _spec(outputs=[{"path": "outputs/debug.log", "artifact": "jobs/run-1/debug.log"}])
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    context, record = _submitted(tmp_path, spec, executor=executor, transfer=transfer)
    executor.status = RemoteJobStatus(status="finished", exit_code=7, stage="command")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.exit_code == 7
    assert "exited 7" in (result.record.state_reason or "")
    # Failure outputs are still attempted so debugging evidence is not thrown away.
    assert transfer.uploads


def test_timeout_is_reported_in_the_failure_reason(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(
        status="finished", exit_code=124, stage="command", timed_out=True
    )

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "timed out" in (result.record.failure_reason or "")


def test_cancelled_job_is_recorded_as_cancelled(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="finished", exit_code=-15, cancelled=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.CANCELLED
    assert result.record.exit_code == -15


def test_cancellation_that_arrives_after_success_still_reports_success(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="finished", exit_code=0, cancelled=True)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.SUCCEEDED
    assert "after the command had finished" in (result.record.state_reason or "")


def test_worker_absence_fails_the_job_without_claiming_running(tmp_path: Path) -> None:
    provider = FakeProvider()
    context, record = _submitted(tmp_path, provider=provider)
    provider.error = ProviderNotFoundError("pod-123 not found")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.worker_absent is True
    assert "no longer exists" in (result.record.failure_reason or "")


def test_worker_that_stopped_before_completion_fails_the_job(tmp_path: Path) -> None:
    provider = FakeProvider()
    context, record = _submitted(tmp_path, provider=provider)
    provider.subject = worker(state=WorkerState.STOPPED)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "left RUNNING" in (result.record.failure_reason or "")


def test_unreachable_provider_reports_uncertainty_without_changing_state(tmp_path: Path) -> None:
    provider = FakeProvider()
    context, record = _submitted(tmp_path, provider=provider)
    provider.error = ProviderUnavailableError("timeout")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert result.warning is not None
    assert "unavailable" in result.warning


def test_unreachable_worker_reports_uncertainty_without_changing_state(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.inspect_error = JobExecutionError("SSH timed out")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.RUNNING
    assert result.warning is not None
    assert "could not be inspected" in result.warning


def test_unknown_remote_state_fails_the_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="unknown")

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert "no recorded state" in (result.record.failure_reason or "")


def test_terminal_jobs_are_not_re_reconciled(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)
    finished = JobCoordinator(context).refresh(record.job_id).record
    provider = context.provider
    executor.calls.clear()
    provider.calls.clear()

    again = JobCoordinator(context).refresh(record.job_id)

    assert again.record == finished
    assert executor.calls == []
    assert provider.calls == []


def test_missing_local_record_is_reported(tmp_path: Path) -> None:
    context = _specless_context(tmp_path)

    with pytest.raises(Exception, match="infra job submit"):
        JobCoordinator(context).refresh("job-0000000000000000")


# --- wait and cancellation --------------------------------------------------------


def test_wait_stops_when_the_job_reaches_a_terminal_state(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    outcomes = [
        RemoteJobStatus(status="running"),
        RemoteJobStatus(status="finished", exit_code=0),
    ]
    executor.status = outcomes[0]
    calls = {"count": 0}

    def refresh_hook() -> None:
        calls["count"] += 1
        executor.status = outcomes[min(calls["count"], 1)]

    coordinator = JobCoordinator(context, sleep=lambda _: refresh_hook(), monotonic=lambda: 0.0)

    result = coordinator.wait(record.job_id, timeout_seconds=10)

    assert result.record.state is JobState.SUCCEEDED


def test_wait_reports_a_timeout_without_claiming_completion(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    clock = [0.0]

    coordinator = JobCoordinator(
        context,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        monotonic=lambda: clock[0],
    )

    result = coordinator.wait(record.job_id, timeout_seconds=1)

    assert result.record.state is JobState.RUNNING
    assert result.warning is not None
    assert "did not finish" in result.warning


def test_cancel_targets_the_job_process_and_records_the_state(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="cancelled", cancelled=True, executed_commit="a" * 40)

    cancelled = JobCoordinator(context).cancel(record.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancellation_requested_at == NOW
    assert executor.cancel_calls == [record.job_id]
    assert not hasattr(context.provider, "destroy_worker")


def test_cancel_does_not_overrule_a_successful_exit(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = JobCoordinator(context).cancel(record.job_id)

    assert result.state is JobState.SUCCEEDED
    assert result.cancellation_requested_at == NOW


def test_cancel_is_idempotent_for_a_terminal_job(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)
    finished = JobCoordinator(context).refresh(record.job_id).record
    executor.calls.clear()

    again = JobCoordinator(context).cancel(record.job_id)

    assert again.state is finished.state
    assert executor.calls == []


def test_cancel_of_an_already_finished_job_reconciles_instead(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.cancel_result = CancelResult(cancelled=False, already_finished=True)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = JobCoordinator(context).cancel(record.job_id)

    assert result.state is JobState.SUCCEEDED


def test_cancel_requires_a_running_worker(tmp_path: Path) -> None:
    provider = FakeProvider()
    context, record = _submitted(tmp_path, provider=provider)
    provider.subject = worker(state=WorkerState.STOPPED)

    with pytest.raises(JobCancellationError, match="cancellation targets the job process"):
        JobCoordinator(context).cancel(record.job_id)


def test_cancel_reports_worker_absence_without_claiming_cancellation(tmp_path: Path) -> None:
    provider = FakeProvider()
    context, record = _submitted(tmp_path, provider=provider)
    provider.error = ProviderNotFoundError("gone")

    result = JobCoordinator(context).cancel(record.job_id)

    assert result.state is JobState.FAILED
    assert result.worker_absent is True


def test_logs_read_from_the_worker_and_validate_the_bound(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)

    assert JobCoordinator(context).logs(record.job_id) == "job output\n"
    assert executor.log_calls == [context.jobs_config.log_tail_bytes]


def test_logs_report_a_missing_worker_actionably(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, executor=executor)
    executor.log_error = ProviderNotFoundError("gone")

    with pytest.raises(JobExecutionError, match="no longer exists"):
        JobCoordinator(context).logs(record.job_id)


# --- security ---------------------------------------------------------------------


def test_durable_records_never_contain_bearer_material_or_secret_values(tmp_path: Path) -> None:
    spec = _spec(
        inputs=[{"artifact": "embeddings/v1/a.tar", "destination": "a.tar", "sha256": "b" * 64}],
        outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"}],
        runtime={"environment_secrets": ["MLFLOW_TRACKING_PASSWORD"]},
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 11
    executor = FakeJobExecutor()
    context, record = _submitted(
        tmp_path, spec, executor=executor, storage=storage, transfer=FakeTransfer()
    )
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    JobCoordinator(context).refresh(record.job_id)

    text = _state_text(context)
    assert SECRET not in text
    assert "X-Amz-Signature" not in text
    assert "Expected-Signature" not in text
    assert "Authorization" not in text
    saved = json.loads(context.job_store.path_for(record.job_id).read_text(encoding="utf-8"))
    assert saved["spec"]["runtime"]["environment_secrets"] == ["MLFLOW_TRACKING_PASSWORD"]
    assert saved["provenance"]["mlflow_owner"] == "wavCSE"


def test_ssh_unavailable_during_submission_is_recorded_as_failed(tmp_path: Path) -> None:
    executor = FakeJobExecutor()
    executor.install_error = JobExecutionError(
        "Could not install the reviewed job runner on worker pod-123: SSH connect timed out"
    )
    context = _specless_context(tmp_path, executor=executor)

    with pytest.raises(JobExecutionError):
        JobSubmitter(context).submit(_spec(), worker_id="pod-123")

    record = context.job_store.list_records()[0]
    assert record.state is JobState.FAILED
    assert "SSH connect timed out" in (record.failure_reason or "")
    assert executor.calls == ["install"]


def test_required_input_checksum_mismatch_prevents_the_job(tmp_path: Path) -> None:
    from wavcse_infra.errors import ArtifactChecksumMismatchError

    spec = _spec(
        inputs=[{"artifact": "embeddings/v1/a.tar", "destination": "a.tar", "sha256": "b" * 64}]
    )
    storage = FakeStorage()
    storage.objects["embeddings/v1/a.tar"] = 11
    transfer = FakeTransfer()
    transfer.download_error = ArtifactChecksumMismatchError(
        "downloaded artifact SHA-256 is deadbeef, but " + "b" * 64 + " was expected"
    )
    executor = FakeJobExecutor()
    context = _specless_context(tmp_path, storage=storage, transfer=transfer, executor=executor)

    with pytest.raises(JobPreconditionError, match="could not be materialized"):
        JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert "start" not in executor.calls
    record = context.job_store.list_records()[0]
    assert record.state is JobState.FAILED
    assert "SHA-256" in (record.inputs[0].failure_reason or "")


def test_output_verification_failure_prevents_success(tmp_path: Path) -> None:
    spec = _spec(outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"}])
    executor = FakeJobExecutor()
    transfer = FakeTransfer()
    transfer.upload_error = StorageVerificationError(
        "s3://bucket/wavcse/jobs/run-1/metrics.json is 6 bytes, but 7 bytes were expected"
    )
    context, record = _submitted(tmp_path, spec, executor=executor, transfer=transfer)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.outputs[0].persisted is False
    assert "bytes were expected" in (result.record.outputs[0].failure_reason or "")


def test_finished_job_with_different_or_missing_executed_commit_never_succeeds(
    tmp_path: Path,
) -> None:
    for reported in ("b" * 40, None):
        executor = FakeJobExecutor()
        context, record = _submitted(tmp_path / (reported or "missing"), executor=executor)
        executor.report_missing_commit = True
        executor.status = RemoteJobStatus(status="finished", exit_code=0, executed_commit=reported)

        result = JobCoordinator(context).refresh(record.job_id)

        assert result.record.state is JobState.FAILED
        assert "provenance cannot be verified" in (result.record.failure_reason or "")
        assert result.record.executed_commit == "a" * 40


def test_missing_storage_after_command_exit_does_not_leave_job_running(tmp_path: Path) -> None:
    spec = _spec(outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run/metrics.json"}])
    executor = FakeJobExecutor()
    context, record = _submitted(tmp_path, spec, executor=executor, storage=FakeStorage())
    object.__setattr__(context, "storage", None)
    executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = JobCoordinator(context).refresh(record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.outputs[0].persisted is False
    assert "storage.bucket" in (result.record.outputs[0].failure_reason or "")
