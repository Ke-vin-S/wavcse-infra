"""CU and upload transport invariants without allocating a live Colab runtime."""

from __future__ import annotations

import io
import json
import stat
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from wavcse_infra.config import ColabConfig
from wavcse_infra.errors import CostGuardError, ProviderResponseError, UnresolvedCreateError
from wavcse_infra.jobs.colab_transport import ColabConnection, ColabExecutor, validate_envelope
from wavcse_infra.models import ExecutionTransport, ProviderKind, Worker, WorkerState
from wavcse_infra.providers.colab import ColabUsage, parse_usage
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.colab import ColabLifecycle, normalize_gpu

SESSION = "wavcse-123456789abc"
USAGE = ColabUsage(Decimal("100"), Decimal("0"), 0)
WORKER = Worker(
    provider=ProviderKind.COLAB,
    execution_transport=ExecutionTransport.COLAB_EXEC,
    id=SESSION,
    name=SESSION,
    state=WorkerState.RUNNING,
    gpu_type="T4",
    gpu_count=1,
)


def test_cli_usage_is_native_decimal_and_malformed_accounting_is_refused() -> None:
    assert parse_usage(
        "Current balance: 12.34 compute units\nUsage rate: 1.80/hr\nActive assignments: 1"
    ) == ColabUsage(Decimal("12.34"), Decimal("1.80"), 1)
    with pytest.raises(ProviderResponseError):
        parse_usage("Current balance: N/A compute units\nUsage rate: 0/hr\nActive assignments: 1")


def test_cli_upload_records_paths_and_restricts_new_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    from unittest.mock import Mock

    from wavcse_infra.providers import colab

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(colab.shutil, "which", lambda _: "/bin/true")
    source = tmp_path / "envelope.json"
    source.write_text('{"dummy":"temporary"}')
    runner = Mock()

    def invoke(argv, **kwargs):
        del kwargs
        root = tmp_path / ".config" / "colab-cli"
        state_file = root / "sessions.json"
        history_file = root / "history" / f"{SESSION}.jsonl"
        state_file.write_text('{"runtime":"token"}')
        history_file.write_text('{"operation":"upload"}')
        os.chmod(state_file, 0o644)
        os.chmod(history_file, 0o644)
        return subprocess.CompletedProcess(argv, 0, "uploaded", "")

    runner.side_effect = invoke
    client = colab.ColabClient(ColabConfig(), runner=runner)
    client.upload_file(SESSION, source, f"/content/wavcse-envelope-{SESSION}.json")
    args = runner.call_args.args[0]
    assert args == [
        "colab",
        "--auth=adc",
        "upload",
        "-s",
        SESSION,
        str(source),
        f"/content/wavcse-envelope-{SESSION}.json",
    ]
    root = tmp_path / ".config" / "colab-cli"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "history").stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "sessions.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "history" / f"{SESSION}.jsonl").stat().st_mode) == 0o600


def test_one_active_or_unresolved_colab_intent_blocks_second_allocation(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    store.record_colab_intent(SESSION, "T4")
    with pytest.raises(UnresolvedCreateError):
        store.record_colab_intent("wavcse-abcdef123456", "T4")
    store.record_colab_created(WORKER)
    with pytest.raises(UnresolvedCreateError):
        store.record_colab_intent("wavcse-abcdef123456", "T4")
    store.mark_destroyed(SESSION)
    store.record_colab_intent("wavcse-abcdef123456", "T4")


def test_acknowledged_create_waits_for_exact_provider_identity_without_second_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    from wavcse_infra.errors import ProviderNotFoundError
    from wavcse_infra.providers import colab

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(colab.shutil, "which", lambda _: "/bin/true")
    monkeypatch.setattr(colab.time, "sleep", lambda _: None)
    runner = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, "Version: 0.7.4\n", ""),
            subprocess.CompletedProcess([], 0, "created\n", ""),
        ]
    )
    provider = colab.ColabClient(ColabConfig(lifecycle_timeout_seconds=2), runner=runner)
    seen = []

    def observe(name: str):
        seen.append(name)
        if len(seen) == 1:
            raise ProviderNotFoundError("new session not yet listed")
        return WORKER

    monkeypatch.setattr(provider, "get_worker", observe)
    assert provider.create_worker(SESSION, "T4") == WORKER
    assert seen == [SESSION, SESSION]
    assert [call.args[0][2] for call in runner.call_args_list] == ["version", "new"]


def test_post_allocation_rate_rejection_releases_only_confirmed_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    client = Mock()
    client.usage_snapshot.side_effect = [USAGE, ColabUsage(Decimal("99.99"), Decimal("4.50"), 1)]
    client.list_workers.side_effect = [[], []]
    client.create_worker.return_value = WORKER
    client.destroy_worker.side_effect = lambda record: setattr(client, "destroyed", record)
    monkeypatch.setattr(
        "wavcse_infra.workers.colab.uuid4", lambda: type("U", (), {"hex": "123456789abc"})()
    )
    lifecycle = ColabLifecycle(
        client, store, ColabConfig(max_incremental_rate_cu_per_hour=Decimal("3"))
    )
    with pytest.raises(CostGuardError, match="COST_POLICY_REJECTION"):
        lifecycle.create("T4")
    assert client.destroyed.provider_worker_id == SESSION
    assert client.destroyed.create_pending is False
    assert store.get(SESSION).provider_absent is True
    assert not client.exec_code.called


def test_definitive_quota_refusal_retires_confirmed_absent_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wavcse_infra.errors import ColabQuotaError

    store = WorkerStateStore(tmp_path / "workers.json")
    client = Mock()
    client.usage_snapshot.return_value = USAGE
    client.list_workers.side_effect = [[], [], []]
    client.create_worker.side_effect = ColabQuotaError("no available CU")
    monkeypatch.setattr(
        "wavcse_infra.workers.colab.uuid4", lambda: type("U", (), {"hex": "123456789abc"})()
    )
    with pytest.raises(ColabQuotaError):
        ColabLifecycle(client, store, ColabConfig()).create("T4")
    assert store.get(SESSION).provider_absent
    assert not client.destroy_worker.called


def test_accepted_cu_rate_bootstraps_physical_gpu_and_marks_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    client = Mock()
    client.usage_snapshot.side_effect = [
        USAGE,
        ColabUsage(Decimal("99.98"), Decimal("1.80"), 1),
    ]
    client.list_workers.return_value = []
    client.create_worker.return_value = WORKER
    client.get_worker.return_value = WORKER
    client.exec_code.side_effect = [
        'wavcse_colab_health\t{"gpu": "Tesla T4", "disk": 2000000000}\n',
        "wavcse_job_schema\t1\nwavcse_job_install\tinstalled\nwavcse_colab_exit\t0\n",
    ]
    monkeypatch.setattr(
        "wavcse_infra.workers.colab.uuid4", lambda: type("U", (), {"hex": "123456789abc"})()
    )
    worker, before, after = ColabLifecycle(client, store, ColabConfig()).create("T4")
    record = store.get(SESSION)
    assert worker.id == SESSION
    assert after.rate_cu_per_hour - before.rate_cu_per_hour == Decimal("1.80")
    assert record.observed_rate_cu_per_hour == Decimal("1.80")
    assert record.observed_gpu_models == ("Tesla T4",)
    assert record.readiness_state.value == "READY"
    assert not client.destroy_worker.called


def test_failed_colab_recheck_clears_stale_ready_state(tmp_path: Path) -> None:
    from wavcse_infra.errors import WorkerBootstrapError

    store = WorkerStateStore(tmp_path / "workers.json")
    store.record_colab_intent(SESSION, "T4")
    store.record_colab_created(WORKER)
    store.record_colab_ready(
        SESSION, gpu_model="Tesla T4", rate=Decimal("1.8"), disk_bytes=2_000_000_000
    )
    client = Mock()
    client.get_worker.return_value = WORKER
    client.exec_code.return_value = "wavcse_colab_health_failed\n"
    with pytest.raises(WorkerBootstrapError):
        ColabLifecycle(client, store, ColabConfig()).bootstrap(SESSION)
    assert store.get(SESSION).readiness_state.value == "FAILED"


def test_envelope_is_private_and_exec_source_never_contains_capability(tmp_path: Path) -> None:
    session = SESSION
    capability = "https://example.test/object?X-Amz-Signature=secret123"
    uploaded: list[tuple[Path, dict[str, object], int]] = []

    class FakeClient:
        def upload_file(self, name: str, local: Path, remote: str) -> None:
            assert name == session
            remote_file = tmp_path / Path(remote).name
            remote_file.write_bytes(local.read_bytes())
            uploaded.append(
                (remote_file, json.loads(local.read_text()), stat.S_IMODE(local.stat().st_mode))
            )

        def exec_code(self, name: str, code: str, *, timeout: float) -> str:
            assert name == session and capability not in code
            assert "wavcse-envelope" in code
            output = io.StringIO()
            with redirect_stdout(output):
                exec(code.replace("/content/wavcse-envelope-", str(tmp_path / "wavcse-envelope-")))
            return output.getvalue()

        def remove_file(self, name: str, remote: str) -> None:
            pytest.fail("successful exec must consume the envelope remotely")

    result = ColabExecutor(FakeClient()).run_checked(  # type: ignore[arg-type]
        ColabConnection(session),
        ("python3", "-c", "import sys; print('received', len(sys.stdin.read()))"),
        input_text=capability,
        timeout_seconds=30,
    )
    assert result.exit_code == 0
    assert f"received {len(capability)}" in result.stdout
    assert uploaded[0][2] == 0o600
    assert uploaded[0][1]["session"] == session
    assert not uploaded[0][0].exists()


def test_envelope_rejects_expiry_and_wrong_session() -> None:
    job = "job-1234567890abcdef"
    envelope: dict[str, object] = {
        "schema_version": 1,
        "session": SESSION,
        "job_id": job,
        "expires_at": datetime.now(UTC).timestamp() - 1,
        "stdin": "",
        "argv": ["python3", "runner.py"],
    }
    with pytest.raises(ProviderResponseError, match="expired"):
        validate_envelope(envelope, SESSION, job)
    envelope["expires_at"] = datetime.now(UTC).timestamp() + 60
    with pytest.raises(ProviderResponseError, match="session"):
        validate_envelope(envelope, "wavcse-abcdef123456", job)


def test_physical_gpu_normalization_never_guesses_cuda_model() -> None:
    assert normalize_gpu("Tesla T4") == "T4"
    assert normalize_gpu("NVIDIA L4") == "L4"
    from wavcse_infra.errors import ColabAcceleratorUnavailableError

    with pytest.raises(ColabAcceleratorUnavailableError):
        normalize_gpu("unknown accelerator")
