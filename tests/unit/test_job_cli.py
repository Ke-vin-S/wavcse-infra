"""`infra job` command surface: wiring, exit codes, and no implicit lifecycle calls."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from job_fakes import (
    FakeProvider,
    FakeStorage,
    FakeTransfer,
    job_context,
    job_spec_document,
    write_spec,
)
from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.errors import ArtifactTransferInProgressError
from wavcse_infra.jobs.execution import RemoteJobStatus
from wavcse_infra.jobs.models import JobState

runner = CliRunner()
JOB_ID = "job-0123456789abcdef"


def _strip_ansi(output: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", output)


@pytest.fixture
def cli_job_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a fake job context and return (context, spec_path)."""

    context = job_context(tmp_path, environ={"MLFLOW_TRACKING_PASSWORD": "secret-value"})
    monkeypatch.setattr(cli, "_job_context", lambda client, settings: context)
    monkeypatch.setattr(cli, "_job_store", lambda: context.job_store)
    monkeypatch.setattr(cli, "_optional_storage", lambda settings: FakeStorage())
    spec_path = write_spec(tmp_path / "job.json")
    return context, spec_path


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None


@pytest.fixture(autouse=True)
def fake_runpod_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli.RunPodClient, "from_settings", classmethod(lambda cls, settings: _FakeClient())
    )


def _submit(cli_job_context) -> str:
    context, spec_path = cli_job_context
    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )
    assert result.exit_code == 0, result.output
    return next(iter(context.job_store.list_records())).job_id


def test_job_help_lists_the_phase_six_commands() -> None:
    result = runner.invoke(app, ["job", "--help"])

    assert result.exit_code == 0
    for command in ("submit", "status", "logs", "cancel"):
        assert command in result.output

    submit_help = runner.invoke(app, ["job", "submit", "--help"])
    assert submit_help.exit_code == 0
    plain_help = _strip_ansi(submit_help.output)
    assert "--worker" in plain_help
    assert "--wait" in plain_help


def test_submit_requires_an_explicit_worker(cli_job_context) -> None:
    _, spec_path = cli_job_context

    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path)],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code != 0
    assert "--worker" in _strip_ansi(result.output)


def test_submit_reports_the_job_and_never_touches_worker_lifecycle(
    cli_job_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, spec_path = cli_job_context
    lifecycle_calls: list[str] = []

    def forbidden(*args, **kwargs):
        lifecycle_calls.append("called")
        raise AssertionError("job submission must not mutate worker lifecycle")

    monkeypatch.setattr(cli, "_lifecycle", forbidden)

    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert lifecycle_calls == []
    assert "Job ID: " in result.output
    assert "State: RUNNING" in result.output
    assert "Requested commit: " + "a" * 40 in result.output
    assert "Executed commit: " + "a" * 40 in result.output
    assert "secret-value" not in result.output
    assert len(list(context.job_store.list_records())) == 1


def test_submit_json_output_is_machine_readable(cli_job_context) -> None:
    _, spec_path = cli_job_context

    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123", "--json"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    payload = json.loads(result.output)
    assert payload["state"] == "RUNNING"
    assert payload["requested_commit"] == "a" * 40
    assert payload["provenance"]["mlflow_owner"] == "wavCSE"
    assert "secret-value" not in result.output


def test_submit_rejects_an_invalid_specification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = job_context(tmp_path)
    monkeypatch.setattr(cli, "_job_context", lambda client, settings: context)
    monkeypatch.setattr(cli, "_optional_storage", lambda settings: FakeStorage())
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**job_spec_document(), "schema_version": 99}), encoding="utf-8")

    result = runner.invoke(
        app,
        ["job", "submit", str(bad), "--worker", "pod-123"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 2
    assert "Configuration error" in result.output
    assert context.job_store.list_records() == []


def test_submit_reports_precondition_failures_with_a_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = job_context(tmp_path)
    provider = FakeProvider()
    from wavcse_infra.errors import ProviderNotFoundError

    provider.error = ProviderNotFoundError("pod-123 not found")
    context = job_context(tmp_path, provider=provider)
    monkeypatch.setattr(cli, "_job_context", lambda client, settings: context)
    monkeypatch.setattr(cli, "_optional_storage", lambda settings: FakeStorage())
    spec_path = write_spec(tmp_path / "job.json")

    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 1
    assert "does not exist" in result.output


def test_status_reconciles_and_exits_nonzero_for_a_failed_job(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    executor = context.executor
    executor.status = RemoteJobStatus(status="finished", exit_code=5, stage="command")

    result = runner.invoke(
        app,
        ["job", "status", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 1
    assert "State: FAILED" in result.output
    assert "Exit code: 5" in result.output


def test_status_of_a_successful_job_exits_zero_and_shows_outputs(cli_job_context) -> None:
    context, spec_path = cli_job_context
    document = job_spec_document(
        outputs=[{"path": "outputs/metrics.json", "artifact": "jobs/run-1/metrics.json"}]
    )
    spec_path.write_text(json.dumps(document), encoding="utf-8")
    job_id = _submit(cli_job_context)
    context.executor.status = RemoteJobStatus(status="finished", exit_code=0)

    result = runner.invoke(
        app,
        ["job", "status", job_id, "--json"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == "SUCCEEDED"
    assert payload["outputs"][0]["artifact"] == "jobs/run-1/metrics.json"
    assert payload["outputs"][0]["persisted"] is True


def test_status_warns_without_claiming_a_state_change(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    from wavcse_infra.errors import JobExecutionError

    context.executor.inspect_error = JobExecutionError("SSH timed out")

    result = runner.invoke(
        app,
        ["job", "status", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert "Warning:" in result.output
    assert "State: RUNNING" in result.output


def test_status_rejects_a_malformed_job_id(cli_job_context) -> None:
    result = runner.invoke(
        app,
        ["job", "status", "../../etc/passwd"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 2
    assert "malformed" in result.output


def test_logs_streams_remote_output_without_framing(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    context.executor.log_text = "epoch 1 loss 0.5\nepoch 2 loss 0.4\n"

    result = runner.invoke(
        app,
        ["job", "logs", job_id, "--tail-bytes", "4096"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert result.output == "epoch 1 loss 0.5\nepoch 2 loss 0.4\n"
    assert context.executor.log_calls == [4096]


def test_logs_falls_back_to_the_local_copy_and_fails(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    context.job_store.write_log(job_id, "captured locally\n")
    from wavcse_infra.errors import JobExecutionError

    context.executor.log_error = JobExecutionError("worker is gone")

    result = runner.invoke(
        app,
        ["job", "logs", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 1
    assert "captured locally" in result.output
    assert "local copy" in result.output


def test_logs_local_flag_reads_the_captured_copy(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    context.job_store.write_log(job_id, "offline copy\n")

    result = runner.invoke(
        app,
        ["job", "logs", job_id, "--local"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert result.output == "offline copy\n"


def test_cancel_reports_the_state_and_leaves_the_worker_alone(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    context.executor.status = RemoteJobStatus(
        status="cancelled", cancelled=True, executed_commit="a" * 40
    )

    result = runner.invoke(
        app,
        ["job", "cancel", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert "cancelled on RunPod worker pod-123" in result.output
    assert "was not stopped or destroyed" in result.output
    assert context.executor.cancel_calls == [job_id]
    assert context.job_store.get(job_id).state is JobState.CANCELLED


def test_cancel_of_a_terminal_job_is_idempotent(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    context.executor.status = RemoteJobStatus(status="finished", exit_code=0)
    runner.invoke(
        app,
        ["job", "status", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    result = runner.invoke(
        app,
        ["job", "cancel", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert "already SUCCEEDED" in result.output
    assert context.executor.cancel_calls == []


def test_cancel_json_is_one_parseable_document(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    context.executor.status = RemoteJobStatus(
        status="cancelled", cancelled=True, executed_commit="a" * 40
    )

    result = runner.invoke(
        app,
        ["job", "cancel", job_id, "--json"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["state"] == "CANCELLED"


def test_submit_wait_blocks_until_terminal(
    cli_job_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, spec_path = cli_job_context
    from wavcse_infra.jobs import status as status_module

    monkeypatch.setattr(
        status_module.JobCoordinator,
        "wait",
        lambda self, job_id, *, timeout_seconds: status_module.RefreshResult(
            self._context.job_store.require(job_id).model_copy(
                update={"state": JobState.SUCCEEDED, "state_reason": "exit 0"}
            )
        ),
    )

    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123", "--wait"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    assert result.exit_code == 0
    assert "State: SUCCEEDED" in result.output
    assert context.executor.start_calls != []


def test_fake_transfer_and_storage_are_untouched_by_status_only_flows(cli_job_context) -> None:
    context, _ = cli_job_context
    job_id = _submit(cli_job_context)
    assert isinstance(context.transfer, FakeTransfer)

    context.executor.status = RemoteJobStatus(status="finished", exit_code=0)
    runner.invoke(
        app,
        ["job", "status", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    # The valid specification declares no outputs, so nothing may be uploaded.
    assert context.transfer.uploads == []


def _interrupted_context(tmp_path: Path, transfer: FakeTransfer):
    storage = FakeStorage()
    storage.objects["embeddings/v1/probe.tar"] = 4096
    context = job_context(
        tmp_path,
        transfer=transfer,
        storage=storage,
        environ={"MLFLOW_TRACKING_PASSWORD": "secret-value"},
    )
    return context


def _interrupting_spec(tmp_path: Path) -> Path:
    document = job_spec_document(
        inputs=[
            {
                "artifact": "embeddings/v1/probe.tar",
                "destination": "probe.tar",
                "sha256": "e" * 64,
            }
        ]
    )
    return write_spec(tmp_path / "job.json", document)


def test_submit_reports_an_interrupted_preparation_without_claiming_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = FakeTransfer()
    transfer.download_interrupts = 1
    context = _interrupted_context(tmp_path, transfer)
    monkeypatch.setattr(cli, "_job_context", lambda client, settings: context)
    monkeypatch.setattr(cli, "_job_store", lambda: context.job_store)
    monkeypatch.setattr(cli, "_optional_storage", lambda settings: context.storage)
    spec_path = _interrupting_spec(tmp_path)

    result = runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    output = _strip_ansi(result.output)
    assert result.exit_code == 1
    assert "State: PREPARING" in output
    assert "Reconciliation required: yes" in output
    assert "Preparation phase: materializing_inputs" in output
    assert "neither failed nor lost" in output
    stored = context.job_store.list_records()[0]
    assert stored.state is JobState.PREPARING
    assert stored.failure_reason is None


def test_status_reports_preparation_evidence_and_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transfer = FakeTransfer()
    transfer.download_error = ArtifactTransferInProgressError(
        "another transfer for the destination is already in progress on this worker"
    )
    context = _interrupted_context(tmp_path, transfer)
    monkeypatch.setattr(cli, "_job_context", lambda client, settings: context)
    monkeypatch.setattr(cli, "_job_store", lambda: context.job_store)
    monkeypatch.setattr(cli, "_optional_storage", lambda settings: context.storage)
    spec_path = _interrupting_spec(tmp_path)
    runner.invoke(
        app,
        ["job", "submit", str(spec_path), "--worker", "pod-123"],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )
    job_id = next(iter(context.job_store.list_records())).job_id
    context.executor.status = RemoteJobStatus(
        status="unknown",
        job_directory_exists=True,
        prepared=True,
        started=False,
    )

    result = runner.invoke(
        app,
        ["job", "status", job_id],
        obj=cli.CliContext(config_path=None, cli_overrides={}, verbose=False),
    )

    output = _strip_ansi(result.output)
    assert result.exit_code == 0, output
    assert "State: PREPARING" in output
    assert "Reconciliation required: yes" in output
    assert "Preparation phase: materializing_inputs" in output
    assert "reconciliation required" in output
