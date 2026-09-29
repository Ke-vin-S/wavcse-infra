"""`infra job list`: deterministic ordering, filters, and a strictly read-only surface."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from job_fakes import NOW, job_context, job_spec_document
from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.jobs.models import JobProvenance, JobRecord, JobState, load_job_spec

runner = CliRunner()


def _record(
    job_id: str,
    state: JobState,
    *,
    created_offset: int = 0,
    worker_id: str = "pod-123",
    name: str | None = None,
) -> JobRecord:
    spec = load_job_spec(json.dumps(job_spec_document()))
    return JobRecord(
        job_id=job_id,
        name=name or spec.name,
        spec=spec,
        state=state,
        worker_id=worker_id,
        job_directory=f"/workspace/wavcse-jobs/{job_id}",
        log_path=f"/workspace/wavcse-jobs/{job_id}/logs/job.log",
        requested_commit=spec.source.commit,
        created_at=NOW + timedelta(seconds=created_offset),
        updated_at=NOW + timedelta(seconds=created_offset),
        provenance=JobProvenance(worker_id=worker_id, infra_version="0.1.0"),
    )


@pytest.fixture
def job_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a job store seeded with three records in a known order."""

    context = job_context(tmp_path)
    store = context.job_store
    store.create(_record("job-0000000000000001", JobState.SUCCEEDED, created_offset=0))
    store.create(
        _record(
            "job-0000000000000002",
            JobState.FAILED,
            created_offset=10,
            name="second",
            worker_id="pod-999",
        )
    )
    store.create(_record("job-0000000000000003", JobState.RUNNING, created_offset=20))
    monkeypatch.setattr(cli, "_job_store", lambda: store)
    return store


def test_empty_store_is_reported_rather_than_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = job_context(tmp_path).job_store
    monkeypatch.setattr(cli, "_job_store", lambda: store)

    result = runner.invoke(app, ["job", "list"])

    assert result.exit_code == 0
    assert "No recorded jobs." in result.stdout


def test_json_output_is_ordered_by_creation_then_id(job_store) -> None:
    result = runner.invoke(app, ["job", "list", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [entry["job_id"] for entry in payload] == [
        "job-0000000000000001",
        "job-0000000000000002",
        "job-0000000000000003",
    ]
    assert payload[0]["state"] == "SUCCEEDED"
    assert payload[0]["worker_id"] == "pod-123"


def test_state_filter_selects_only_that_state(job_store) -> None:
    result = runner.invoke(app, ["job", "list", "--state", "failed", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [entry["job_id"] for entry in payload] == ["job-0000000000000002"]


def test_worker_filter_selects_only_that_worker(job_store) -> None:
    result = runner.invoke(app, ["job", "list", "--worker", "pod-999", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [entry["job_id"] for entry in payload] == ["job-0000000000000002"]


def test_unknown_state_is_rejected(job_store) -> None:
    result = runner.invoke(app, ["job", "list", "--state", "EXPLODED"])

    assert result.exit_code != 0
    assert "EXPLODED" in result.output


def test_unmatched_filter_reports_an_empty_result(job_store) -> None:
    result = runner.invoke(app, ["job", "list", "--worker", "pod-absent"])

    assert result.exit_code == 0
    assert "No recorded jobs match the filter." in result.stdout


def test_human_output_lists_one_row_per_job(job_store) -> None:
    result = runner.invoke(app, ["job", "list"])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.startswith("job-")]
    assert len(lines) == 3
    assert "SUCCEEDED" in result.stdout


def test_list_is_read_only(job_store) -> None:
    before = {record.job_id: record.updated_at for record in job_store.list_records()}

    for _ in range(3):
        assert runner.invoke(app, ["job", "list", "--json"]).exit_code == 0

    after = {record.job_id: record.updated_at for record in job_store.list_records()}
    assert after == before
    assert sorted(path.name for path in job_store.directory.glob("*")) == [
        "job-0000000000000001.json",
        "job-0000000000000002.json",
        "job-0000000000000003.json",
    ]


def test_list_does_not_require_a_provider_client(
    job_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing reads local records only, so an unreachable provider cannot break it."""

    def explode(*args: object, **kwargs: object):
        raise AssertionError("job list must not contact the provider")

    monkeypatch.setattr(cli.RunPodClient, "from_settings", classmethod(explode))

    result = runner.invoke(app, ["job", "list", "--json"])

    assert result.exit_code == 0
    assert len(json.loads(result.stdout)) == 3
