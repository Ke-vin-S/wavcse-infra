"""Provider preference chooses leases before an experiment, not retries research failures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from job_fakes import job_spec_document, worker_record, write_spec
from job_fakes import worker as runpod_worker
from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.config import Settings
from wavcse_infra.errors import JobExecutionError, ProviderUnavailableError
from wavcse_infra.jobs.models import load_job_spec
from wavcse_infra.models import ProviderKind
from wavcse_infra.providers.colab import ColabUsage
from wavcse_infra.state import WorkerStateStore

SESSION = "wavcse-abcdef123456"


def _placement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, colab_fails: bool = False):
    from test_colab_execution import WORKER

    colab = WORKER.model_copy(update={"id": SESSION, "name": SESSION})
    store = WorkerStateStore(tmp_path / "workers.json")
    store.record_colab_intent(SESSION, "T4")
    store.record_colab_created(colab)
    store.record_colab_ready(
        SESSION,
        gpu_model="Tesla T4",
        rate=Decimal("1.8"),
        disk_bytes=10**9,
        baseline_rate=Decimal("0"),
        baseline_assignments=0,
    )
    runpod = worker_record()
    records = [*store.list_records(), runpod]

    def resolve(identifier: str):
        return next((r for r in records if r.provider_worker_id == identifier), None)

    def snapshot() -> ColabUsage:
        return ColabUsage(Decimal("90"), Decimal("1.8"), 1)

    monkeypatch.setattr(
        cli, "_state_store", lambda: SimpleNamespace(list_records=lambda: records, get=resolve)
    )
    monkeypatch.setattr(cli, "_job_store", lambda: SimpleNamespace(list_records=lambda: []))
    monkeypatch.setattr(
        cli, "_colab_client", lambda settings: SimpleNamespace(usage_snapshot=snapshot)
    )
    observed: list[str] = []

    @contextmanager
    def context(worker_id: str, settings: Settings) -> Iterator[SimpleNamespace]:
        del settings

        class Provider:
            def get_worker(self, ident: str):
                observed.append(ident)
                if ident == SESSION and colab_fails:
                    raise ProviderUnavailableError("Colab session transport unavailable")
                return colab if ident == SESSION else runpod_worker()

        yield SimpleNamespace(provider=Provider())

    monkeypatch.setattr(cli, "_job_context_for", context)
    return observed


def test_preference_and_explicit_override_use_existing_ready_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed = _placement(monkeypatch, tmp_path)
    settings = Settings(colab={"enabled": True})
    spec = load_job_spec(json.dumps(job_spec_document(runtime={"timeout_seconds": 600})))
    assert cli._select_job_worker(settings, spec, None) == SESSION
    assert cli._select_job_worker(settings, spec, ProviderKind.RUNPOD) == "pod-123"
    assert observed == [SESSION, "pod-123"]


def test_provider_transport_failure_falls_back_only_for_unspecified_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed = _placement(monkeypatch, tmp_path, colab_fails=True)
    settings = Settings(colab={"enabled": True})
    spec = load_job_spec(json.dumps(job_spec_document(runtime={"timeout_seconds": 600})))
    assert cli._select_job_worker(settings, spec, None) == "pod-123"
    with pytest.raises(ProviderUnavailableError):
        cli._select_job_worker(settings, spec, ProviderKind.COLAB)
    assert observed == [SESSION, "pod-123", SESSION]


def test_reuse_rechecks_current_rate_and_assignment_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from wavcse_infra.errors import CostGuardError

    _placement(monkeypatch, tmp_path)
    settings = Settings(colab={"enabled": True})
    spec = load_job_spec(json.dumps(job_spec_document(runtime={"timeout_seconds": 600})))
    monkeypatch.setattr(
        cli,
        "_colab_client",
        lambda settings: SimpleNamespace(
            usage_snapshot=lambda: ColabUsage(Decimal("90"), Decimal("4.20"), 1)
        ),
    )
    assert cli._select_job_worker(settings, spec, None) == "pod-123"
    record = next(r for r in cli._state_store().list_records() if r.provider is ProviderKind.COLAB)
    with pytest.raises(CostGuardError):
        cli._require_colab_job_budget(settings, spec, record)


def test_job_execution_failure_never_submits_again_to_fallback_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    selected: list[str] = []
    spec_path = write_spec(tmp_path / "job.json")
    monkeypatch.setattr(cli, "_select_job_worker", lambda settings, spec, provider: SESSION)
    store = SimpleNamespace(locked=nullcontext, get=lambda worker: None)
    monkeypatch.setattr(cli, "_state_store", lambda: store)

    @contextmanager
    def context(worker_id: str, settings: Settings) -> Iterator[SimpleNamespace]:
        del settings
        selected.append(worker_id)
        yield SimpleNamespace()

    monkeypatch.setattr(cli, "_job_context_for", context)

    class FailedSubmitter:
        def __init__(self, context):
            pass

        def submit(self, spec, *, worker_id: str):
            raise JobExecutionError(f"experiment command on {worker_id} failed")

    monkeypatch.setattr(cli, "JobSubmitter", FailedSubmitter)
    result = CliRunner().invoke(cli.app, ["job", "submit", str(spec_path)], env={})
    assert result.exit_code == 1
    assert selected == [SESSION]
