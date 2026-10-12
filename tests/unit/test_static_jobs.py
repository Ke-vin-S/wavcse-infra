"""Recorded jobs on a declared static host: confined paths, declared environment, slots."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from job_fakes import NOW, job_spec_document
from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.config import Settings, StaticConfig, StaticWorkerConfig
from wavcse_infra.errors import ConfigurationError
from wavcse_infra.jobs.models import JobProvenance, JobRecord, JobState, load_job_spec
from wavcse_infra.providers.static import StaticClient
from wavcse_infra.state import WorkerStateStore

runner = CliRunner()

ROOT = "/home/operator/wavcse"


def _declaration(*, max_concurrent_jobs: int = 1) -> StaticWorkerConfig:
    return StaticWorkerConfig(
        name="university",
        host="hpc.example.edu",
        username="operator",
        gpu_type="NVIDIA A100-SXM4-80GB",
        root=ROOT,
        env={"CUDA_VISIBLE_DEVICES": "0"},
        max_concurrent_jobs=max_concurrent_jobs,
    )


def _config_file(tmp_path: Path, *, max_concurrent_jobs: int = 1) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[[static.workers]]
name = "university"
host = "hpc.example.edu"
username = "operator"
gpu_type = "NVIDIA A100-SXM4-80GB"
root = "{ROOT}"
env = {{ CUDA_VISIBLE_DEVICES = "0" }}
max_concurrent_jobs = {max_concurrent_jobs}
""".strip(),
        encoding="utf-8",
    )
    return config_file


def _record(job_id: str, state: JobState) -> JobRecord:
    spec = load_job_spec(json.dumps(job_spec_document()))
    return JobRecord(
        job_id=job_id,
        name=spec.name,
        spec=spec,
        state=state,
        worker_id="university",
        job_directory=f"{ROOT}/jobs/{job_id}",
        log_path=f"{ROOT}/jobs/{job_id}/logs/job.log",
        requested_commit=spec.source.commit,
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=1),
        provenance=JobProvenance(worker_id="university", infra_version="0.1.0"),
    )


class _JobStore:
    def __init__(self, records: list[JobRecord]) -> None:
        self._records = records

    def list_records(self) -> list[JobRecord]:
        return list(self._records)


def test_static_job_context_confines_every_path_and_uses_the_declared_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    declaration = _declaration()
    store.record_registered(
        StaticClient(StaticConfig(workers=(declaration,))).get_worker("university"),
        cache_root=f"{ROOT}/cache",
    )

    context = cli._static_job_context(
        Settings(static=StaticConfig(workers=(declaration,))), declaration
    )

    assert context.jobs_config.worker_root == f"{ROOT}/jobs"
    assert context.jobs_config.runner_path == f"{ROOT}/job_runner.py"
    assert context.executor.runner_path == f"{ROOT}/job_runner.py"
    assert context.executor.transport_environment() == {"CUDA_VISIBLE_DEVICES": "0"}
    # The rebuildable cache lives inside the confined root, and only the declaration's own
    # environment is injected ahead of the job specification's.
    assert context.cache is not None
    assert context.cache_root("university") == f"{ROOT}/cache"
    assert context.storage is None


def test_the_slot_guard_counts_only_jobs_that_are_still_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declaration = _declaration()
    finished = _record("job-0000000000000001", JobState.SUCCEEDED)
    running = _record("job-0000000000000002", JobState.RUNNING)

    monkeypatch.setattr(cli, "_job_store", lambda: _JobStore([finished]))
    cli._require_static_job_slot(declaration)

    monkeypatch.setattr(cli, "_job_store", lambda: _JobStore([finished, running]))
    with pytest.raises(ConfigurationError, match="allows 1") as failure:
        cli._require_static_job_slot(declaration)

    assert "university" in str(failure.value)
    assert running.job_id in str(failure.value)


def test_a_second_submission_is_refused_while_the_first_job_is_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    store.record_registered(
        StaticClient(StaticConfig(workers=(_declaration(),))).get_worker("university"),
        cache_root=f"{ROOT}/cache",
    )
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    monkeypatch.setattr(
        cli,
        "_job_store",
        lambda: _JobStore([_record("job-0000000000000002", JobState.RUNNING)]),
    )
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(job_spec_document()), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--config",
            str(_config_file(tmp_path)),
            "job",
            "submit",
            str(spec_file),
            "--worker",
            "university",
        ],
        env={},
    )

    assert result.exit_code == 2
    assert "already has 1 active recorded job(s)" in result.stderr
    assert "job-0000000000000002" in result.stderr
