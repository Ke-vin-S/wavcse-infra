"""Controller-side executor: protocol, argv/stdin discipline, and error handling."""

from __future__ import annotations

import hashlib
import json

import pytest
from job_fakes import (
    CONNECTION,
    FakeWaiter,
    RecordingExecutor,
    cancel_output,
    inspect_output,
    prepare_output,
    runner_output,
    start_output,
)

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.errors import JobExecutionError, SshCommandError
from wavcse_infra.jobs.execution import (
    JobExecutor,
    load_worker_job_runner_source,
    worker_job_runner_digest,
)

COMMIT = "a" * 40
JOB_ID = "job-0123456789abcdef"
JOB_DIRECTORY = f"/workspace/wavcse-jobs/{JOB_ID}"


def _executor(
    responses: dict[str, tuple[int, str, str]],
    *,
    jobs_config: JobsConfig | None = None,
) -> tuple[JobExecutor, RecordingExecutor, FakeWaiter]:
    remote = RecordingExecutor(responses=responses)
    waiter = FakeWaiter()
    return (
        JobExecutor(waiter, remote, SshConfig(), jobs_config or JobsConfig()),
        remote,
        waiter,
    )


def test_installs_the_reviewed_runner_verified_by_digest_on_stdin_only() -> None:
    source = load_worker_job_runner_source()
    digest = worker_job_runner_digest(source)
    executor, remote, _ = _executor(
        {"-c": (0, "wavcse_job_schema\t1\nwavcse_job_install\tinstalled\n", "")}
    )

    assert executor.install_runner("pod-123") == "installed"

    argv, input_text, _ = remote.calls[0]
    assert argv[0] == "python3"
    assert argv[1] == "-c"
    assert argv[3] == executor.runner_path
    assert argv[4] == digest
    # The reviewed module travels on stdin; only its path and digest are arguments.
    assert input_text == source


def test_install_reports_unchanged_and_validates_the_digest_of_the_payload() -> None:
    source = load_worker_job_runner_source()
    executor, remote, _ = _executor(
        {"-c": (0, "wavcse_job_schema\t1\nwavcse_job_install\tunchanged\n", "")}
    )

    assert executor.install_runner("pod-123") == "unchanged"
    argv, input_text, _ = remote.calls[0]
    assert argv[4] == hashlib.sha256(str(input_text).encode("utf-8")).hexdigest()
    assert argv[4] == worker_job_runner_digest(source)


def test_install_failure_is_actionable() -> None:
    executor, _, _ = _executor({"-c": (1, "", "worker runner payload digest mismatch\n")})

    with pytest.raises(JobExecutionError, match="Could not install the reviewed job runner"):
        executor.install_runner("pod-123")


def test_install_without_a_confirmation_row_is_rejected() -> None:
    executor, _, _ = _executor(
        {"-c": (0, "wavcse_job_schema\t1\nwavcse_job_install\tunknown\n", "")}
    )

    with pytest.raises(JobExecutionError, match="did not confirm"):
        executor.install_runner("pod-123")


def test_prepare_sends_the_descriptor_on_stdin_and_verifies_the_commit() -> None:
    executor, remote, _ = _executor({"prepare": (0, prepare_output(COMMIT, JOB_DIRECTORY), "")})

    result = executor.prepare(
        "pod-123",
        job_id=JOB_ID,
        job_directory=JOB_DIRECTORY,
        repository="https://github.com/Synergy-io/wavCSE.git",
        commit=COMMIT,
        name="dg-0004",
        input_destinations=("nested/input.tar",),
    )

    assert result.executed_commit == COMMIT
    assert result.source_directory == f"{JOB_DIRECTORY}/source"
    argv, input_text, _ = remote.calls[0]
    assert argv == ("python3", executor.runner_path, "prepare")
    descriptor = json.loads(str(input_text))
    assert descriptor["job_id"] == JOB_ID
    assert descriptor["job_directory"] == JOB_DIRECTORY
    assert descriptor["source"]["commit"] == COMMIT
    assert descriptor["expected_bootstrap_version"] == "1"
    assert descriptor["input_destinations"] == ["nested/input.tar"]
    assert JOB_DIRECTORY not in " ".join(argv)


def test_prepare_rejects_a_worker_that_checked_out_a_different_commit() -> None:
    executor, _, _ = _executor({"prepare": (0, prepare_output("b" * 40, JOB_DIRECTORY), "")})

    with pytest.raises(JobExecutionError, match="refusing to run a different revision"):
        executor.prepare(
            "pod-123",
            job_id=JOB_ID,
            job_directory=JOB_DIRECTORY,
            repository="https://github.com/Synergy-io/wavCSE.git",
            commit=COMMIT,
        )


def test_remote_failure_is_wrapped_with_the_job_phase_and_worker() -> None:
    executor, _, _ = _executor({"prepare": (1, "", "fatal: unable to access repository\n")})

    with pytest.raises(JobExecutionError, match="prepare failed on RunPod worker pod-123"):
        executor.prepare(
            "pod-123",
            job_id=JOB_ID,
            job_directory=JOB_DIRECTORY,
            repository="https://github.com/Synergy-io/wavCSE.git",
            commit=COMMIT,
        )


def test_start_keeps_secret_values_out_of_argv_and_parses_the_acknowledgement() -> None:
    executor, remote, _ = _executor({"start": (0, start_output(pid=999, commit=COMMIT), "")})

    result = executor.start(
        "pod-123",
        job_id=JOB_ID,
        job_directory=JOB_DIRECTORY,
        repository="https://github.com/Synergy-io/wavCSE.git",
        commit=COMMIT,
        argv=["uv", "run", "python", "train.py"],
        setup_argv=["uv", "sync", "--locked"],
        working_directory="improvements/base",
        environment={"PYTHONUNBUFFERED": "1"},
        secrets={"MLFLOW_TRACKING_PASSWORD": "super-secret-value"},
        timeout_seconds=3600,
        infra_environment={"INFRA_JOB_ID": JOB_ID, "INFRA_GIT_COMMIT": COMMIT},
    )

    assert result.pid == 999
    assert result.executed_commit == COMMIT
    argv, input_text, timeout = remote.calls[0]
    assert argv == ("python3", executor.runner_path, "start")
    assert "super-secret-value" not in " ".join(argv)
    assert timeout == SshConfig().bootstrap_timeout_seconds
    descriptor = json.loads(str(input_text))
    assert descriptor["secrets"] == {"MLFLOW_TRACKING_PASSWORD": "super-secret-value"}
    assert descriptor["command"]["argv"][0] == "uv"
    assert descriptor["setup_argv"] == ["uv", "sync", "--locked"]
    assert descriptor["infra"]["INFRA_GIT_COMMIT"] == COMMIT


def test_start_merges_transport_environment_and_declared_values_win() -> None:
    executor, remote, _ = _executor({"start": (0, start_output(pid=999, commit=COMMIT), "")})
    executor.transport_environment = lambda: {  # type: ignore[method-assign]
        "LD_LIBRARY_PATH": "/usr/lib64-nvidia",
        "SHARED": "transport",
    }

    executor.start(
        "pod-123",
        job_id=JOB_ID,
        job_directory=JOB_DIRECTORY,
        repository="https://github.com/Synergy-io/wavcse.git",
        commit=COMMIT,
        argv=["python3", "-c", "import torch"],
        setup_argv=None,
        working_directory=None,
        environment={"SHARED": "declared", "OTHER": "1"},
        secrets={},
        timeout_seconds=600,
        infra_environment={},
    )

    descriptor = json.loads(str(remote.calls[0][1]))
    assert descriptor["environment"]["LD_LIBRARY_PATH"] == "/usr/lib64-nvidia"
    assert descriptor["environment"]["SHARED"] == "declared"
    assert descriptor["environment"]["OTHER"] == "1"


def test_default_transport_environment_is_empty() -> None:
    executor, _, _ = _executor({})
    assert executor.transport_environment() == {}


def test_inspect_parses_worker_evidence_and_booleans() -> None:
    executor, _remote, _ = _executor(
        {
            "inspect": (
                0,
                inspect_output(
                    "finished", exit_code=3, timed_out=True, cancelled=True, log_bytes=99
                ),
                "",
            )
        }
    )

    status = executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)

    assert status.status == "finished"
    assert status.exit_code == 3
    assert status.timed_out is True
    assert status.cancelled is True
    assert status.log_bytes == 99
    assert status.finished_at is not None


def test_inspect_requires_every_protocol_field() -> None:
    executor, _, _ = _executor({"inspect": (0, runner_output(status="running"), "")})

    with pytest.raises(JobExecutionError, match="incomplete job inspect result"):
        executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)


def test_protocol_without_schema_version_is_rejected() -> None:
    executor, _, _ = _executor({"inspect": (0, "status\trunning\n", "")})

    with pytest.raises(JobExecutionError, match="without schema version 1"):
        executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)


def test_malformed_protocol_rows_are_rejected() -> None:
    executor, _, _ = _executor({"inspect": (0, "wavcse_job_schema\t1\nnot-a-row\n", "")})

    with pytest.raises(JobExecutionError, match="malformed job output"):
        executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)


def test_cancel_parses_the_idempotent_result() -> None:
    executor, _, _ = _executor(
        {"cancel": (0, cancel_output(cancelled=True, already_finished=False), "")}
    )

    result = executor.cancel("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)

    assert result.cancelled is True
    assert result.already_finished is False
    assert result.pid == 4321


def test_logs_returns_raw_output_and_forwards_the_tail_bound() -> None:
    executor, remote, _ = _executor({"logs": (0, "line one\nline two\n", "")})

    text = executor.logs("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY, tail_bytes=4096)

    assert text == "line one\nline two\n"
    argv, input_text, _ = remote.calls[0]
    assert argv == ("python3", executor.runner_path, "logs")
    assert json.loads(str(input_text))["tail_bytes"] == 4096


def test_logs_failure_is_actionable() -> None:
    executor, _, _ = _executor({"logs": (1, "", "wavcse_job_error\tlog is missing\n")})

    with pytest.raises(JobExecutionError, match="Could not read logs for job"):
        executor.logs("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY, tail_bytes=10)


def test_waiters_are_used_before_every_remote_phase() -> None:
    executor, _, waiter = _executor(
        {
            "prepare": (0, prepare_output(COMMIT, JOB_DIRECTORY), ""),
            "inspect": (0, inspect_output(), ""),
            "cancel": (0, cancel_output(), ""),
        }
    )

    executor.prepare(
        "pod-123",
        job_id=JOB_ID,
        job_directory=JOB_DIRECTORY,
        repository="https://github.com/Synergy-io/wavCSE.git",
        commit=COMMIT,
    )
    executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)
    executor.cancel("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)

    assert waiter.calls == ["pod-123", "pod-123", "pod-123"]
    assert CONNECTION.kind == "direct"


def test_ssh_command_errors_never_leak_bearer_material() -> None:
    remote = RecordingExecutor()
    waiter = FakeWaiter()
    executor = JobExecutor(waiter, remote, SshConfig(), JobsConfig())
    remote.responses["inspect"] = (1, "", "denied for https://host/x?X-Amz-Signature=secret\n")

    with pytest.raises(JobExecutionError) as error:
        executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)

    assert "X-Amz-Signature=secret" not in str(error.value)


def test_schema_only_output_is_accepted_for_phases_without_required_fields() -> None:
    remote = RecordingExecutor(responses={"inspect": (0, "wavcse_job_schema\t1\n", "")})
    executor = JobExecutor(FakeWaiter(), remote, SshConfig(), JobsConfig())

    with pytest.raises(JobExecutionError, match="missing"):
        executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)


def test_remote_command_failure_type_is_preserved_for_callers() -> None:
    remote = RecordingExecutor()
    remote.responses["inspect"] = (255, "", "Connection refused\n")
    executor = JobExecutor(FakeWaiter(), remote, SshConfig(), JobsConfig())

    with pytest.raises(JobExecutionError) as error:
        executor.inspect("pod-123", job_id=JOB_ID, job_directory=JOB_DIRECTORY)

    assert isinstance(error.value.__cause__, SshCommandError)
