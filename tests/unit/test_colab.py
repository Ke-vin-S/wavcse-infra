"""Pinned CLI boundary tests: no provider calls, allocations, or login."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from wavcse_infra.config import ColabConfig
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ColabAcceleratorUnavailableError,
    ColabAuthenticationRequiredError,
    ColabCliMissingError,
    ColabQuotaError,
    ProviderOperationAmbiguousError,
    ProviderResponseError,
    ProviderValidationError,
    UnsupportedProviderOperationError,
)
from wavcse_infra.models import ExecutionTransport, ProviderKind, WorkerState
from wavcse_infra.providers import colab
from wavcse_infra.state import WorkerRecord

NAME = "wavcse-123456789abc"
SESSION = f"[{NAME}] backend.example | Hardware: T4 | Shape: STANDARD | Variant: GPU"


def owned_record() -> WorkerRecord:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return WorkerRecord(
        provider=ProviderKind.COLAB,
        execution_transport=ExecutionTransport.COLAB_EXEC,
        provider_worker_id=NAME,
        infra_identity=NAME,
        requested_gpu_type="T4",
        requested_gpu_count=1,
        creation_timestamp=now,
        last_observed_at=now,
        last_observed_state=WorkerState.RUNNING,
    )


def client(monkeypatch: pytest.MonkeyPatch, *results: tuple[int, str, str]):
    monkeypatch.setattr(colab.shutil, "which", lambda command: "/usr/bin/colab")
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], code, stdout, stderr)
            for code, stdout, stderr in results
        ]
    )
    return colab.ColabClient(ColabConfig(enabled=True), runner=runner), runner


def test_version_and_sessions_normalize_without_exposing_cli_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner = client(
        monkeypatch,
        (0, "Version: 0.7.4\n", ""),
        (0, SESSION + "\n", ""),
    )
    worker = provider.list_workers()[0]
    assert worker.provider is ProviderKind.COLAB
    assert worker.execution_transport is ExecutionTransport.COLAB_EXEC
    assert worker.id == NAME and worker.gpu_type == "T4"
    assert "backend.example" not in worker.model_dump_json()
    assert runner.call_args_list[0].args[0] == ["colab", "--auth=adc", "version"]
    assert runner.call_args_list[1].args[0] == ["colab", "--auth=adc", "sessions"]
    for call in runner.call_args_list:
        assert call.kwargs["capture_output"] and not call.kwargs["check"]
        assert "shell" not in call.kwargs


def test_missing_cli_fails_before_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(colab.shutil, "which", lambda command: None)
    runner = Mock()
    with pytest.raises(ColabCliMissingError, match="uv tool install"):
        colab.ColabClient(ColabConfig(enabled=True), runner=runner).version()
    runner.assert_not_called()


def test_nonstandard_version_and_malformed_sessions_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = client(monkeypatch, (0, "Version: 0.8.0\n", ""))
    with pytest.raises(ProviderValidationError, match="unvalidated"):
        provider.version()
    provider, _ = client(monkeypatch, (0, "Version: 0.7.4\n", ""), (0, "bad line\n", ""))
    with pytest.raises(ProviderResponseError, match="could not be parsed"):
        provider.list_workers()


def test_duplicate_session_names_are_not_adopted(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = client(
        monkeypatch,
        (0, "Version: 0.7.4\n", ""),
        (0, SESSION + "\n" + SESSION + "\n", ""),
    )
    with pytest.raises(ProviderResponseError, match="duplicate"):
        provider.list_workers()


def test_status_handles_nested_busy_execution_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = client(
        monkeypatch,
        (0, "Version: 0.7.4\n", ""),
        (0, "Version: 0.7.4\n", ""),
        (0, SESSION + "\n", ""),
        (0, SESSION + " | Status: BUSY (exec(stdin))\n", ""),
    )
    worker = provider.get_worker(NAME)
    assert worker.native_status == "BUSY"
    assert worker.state.value == "RUNNING"


def test_session_status_never_persists_upstream_exec_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "X-Amz-Signature=must-not-be-recorded"
    provider, _ = client(
        monkeypatch,
        (0, "Version: 0.7.4\n", ""),
        (0, "Version: 0.7.4\n", ""),
        (0, SESSION + "\n", ""),
        (0, SESSION + f" | Status: BUSY (exec({secret}))\n", ""),
    )
    worker = provider.get_worker(NAME)
    assert worker.native_status == "BUSY"
    assert secret not in worker.model_dump_json()


def test_session_disappearance_is_provider_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from wavcse_infra.errors import ProviderNotFoundError

    provider, _ = client(
        monkeypatch,
        (0, "Version: 0.7.4\n", ""),
        (0, "Version: 0.7.4\n", ""),
        (0, "[colab] No active sessions found on server.\n", ""),
    )
    with pytest.raises(ProviderNotFoundError, match="absent"):
        provider.get_worker(NAME)


def test_adc_error_never_prints_bearer_material(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _ = client(
        monkeypatch,
        (1, "", "No valid default credentials; https://s3.test/x?X-Amz-Signature=secret"),
    )
    with pytest.raises(ColabAuthenticationRequiredError) as error:
        provider.version()
    assert "gcloud auth application-default login" in str(error.value)
    assert "secret" not in str(error.value)


def test_timeout_is_unknown_and_never_retries_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    provider, runner = client(monkeypatch, (0, "Version: 0.7.4\n", ""))
    runner.side_effect = [
        subprocess.CompletedProcess([], 0, "Version: 0.7.4\n", ""),
        subprocess.TimeoutExpired(["colab", "stop"], 1),
    ]
    with pytest.raises(ProviderOperationAmbiguousError, match="unknown"):
        provider.destroy_worker(owned_record())
    assert runner.call_count == 2


def test_create_rejects_unknown_gpu_before_cli_silently_substitutes_a100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner = client(monkeypatch)
    with pytest.raises(ProviderValidationError, match="unsupported"):
        provider.create_worker(NAME, "RTX_6000")
    runner.assert_not_called()


def test_create_normalizes_success_only_after_provider_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner = client(
        monkeypatch,
        (0, "Version: 0.7.4\n", ""),
        (0, "[colab] Session READY.\n", ""),
        (0, "Version: 0.7.4\n", ""),
        (0, "Version: 0.7.4\n", ""),
        (0, SESSION + "\n", ""),
        (0, SESSION + " | Status: IDLE\n", ""),
    )
    worker = provider.create_worker(NAME, "T4")
    assert worker.id == NAME
    assert worker.native_status == "IDLE"
    assert runner.call_args_list[1].args[0] == [
        "colab",
        "--auth=adc",
        "new",
        "-s",
        NAME,
        "--gpu",
        "T4",
    ]


def test_create_reconciles_exact_identity_after_lost_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner = client(monkeypatch)
    runner.side_effect = [
        subprocess.CompletedProcess([], 0, "Version: 0.7.4\n", ""),
        subprocess.TimeoutExpired(["colab", "new"], 30),
        subprocess.CompletedProcess([], 0, "Version: 0.7.4\n", ""),
        subprocess.CompletedProcess([], 0, SESSION + "\n", ""),
    ]
    worker = provider.create_worker(NAME, "T4")
    assert worker.id == NAME
    assert runner.call_count == 4
    assert runner.call_args_list[1].args[0] == [
        "colab",
        "--auth=adc",
        "new",
        "-s",
        NAME,
        "--gpu",
        "T4",
    ]


def test_lost_create_without_provider_evidence_stays_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner = client(monkeypatch)
    runner.side_effect = [
        subprocess.CompletedProcess([], 0, "Version: 0.7.4\n", ""),
        subprocess.TimeoutExpired(["colab", "new"], 30),
        subprocess.CompletedProcess([], 0, "Version: 0.7.4\n", ""),
        subprocess.CompletedProcess([], 0, "[colab] No active sessions found on server.\n", ""),
    ]
    with pytest.raises(AmbiguousCreateError, match="unknown outcome"):
        provider.create_worker(NAME, "T4")
    assert runner.call_count == 4


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("Backend rejected accelerator 'T4'", ColabAcceleratorUnavailableError),
        ("too many active sessions", ColabQuotaError),
    ],
)
def test_provider_capacity_is_not_experiment_failure(
    monkeypatch: pytest.MonkeyPatch, text: str, error: type[Exception]
) -> None:
    provider, _ = client(monkeypatch, (0, "Version: 0.7.4\n", ""), (1, "", text))
    with pytest.raises(error):
        provider.create_worker(NAME, "T4")


def test_destroy_uses_terminal_stop_only_after_exact_identity_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, runner = client(monkeypatch, (0, "Version: 0.7.4\n", ""), (0, "", ""))
    with pytest.raises(UnsupportedProviderOperationError):
        provider.destroy_worker(owned_record().model_copy(update={"infra_identity": "other"}))
    provider.destroy_worker(owned_record())
    assert runner.call_args_list[1].args[0] == ["colab", "--auth=adc", "stop", "-s", NAME]
