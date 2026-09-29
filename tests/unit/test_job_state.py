"""Durable per-job local state, atomic writes, and legal transitions."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from job_fakes import NOW, job_spec_document

from wavcse_infra.errors import JobSpecError, JobStateError
from wavcse_infra.jobs.models import JobProvenance, JobRecord, JobState, load_job_spec
from wavcse_infra.jobs.state import JobStateStore
from wavcse_infra.redaction import contains_bearer_material


def _record(job_id: str = "job-0123456789abcdef", state: JobState = JobState.PENDING) -> JobRecord:
    spec = load_job_spec(json.dumps(job_spec_document()))
    return JobRecord(
        job_id=job_id,
        name=spec.name,
        spec=spec,
        state=state,
        worker_id="pod-123",
        job_directory=f"/workspace/wavcse-jobs/{job_id}",
        log_path=f"/workspace/wavcse-jobs/{job_id}/logs/job.log",
        requested_commit=spec.source.commit,
        created_at=NOW,
        updated_at=NOW,
        provenance=JobProvenance(worker_id="pod-123", infra_version="0.1.0"),
    )


def test_create_get_and_list_round_trip_one_document_per_job(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    first = store.create(_record())
    second = store.create(_record("job-fedcba9876543210", JobState.RUNNING))

    assert store.get(first.job_id) == first
    assert store.get("job-0000000000000000") is None
    assert [record.job_id for record in store.list_records()] == [first.job_id, second.job_id]
    assert store.path_for(first.job_id).name == f"{first.job_id}.json"
    assert stat.S_IMODE(store.path_for(first.job_id).stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path_for(first.job_id).parent.stat().st_mode) == 0o700


def test_create_refuses_to_reuse_a_job_id(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    store.create(_record())

    with pytest.raises(JobStateError, match="never reused"):
        store.create(_record())


def test_require_reports_a_missing_record_actionably(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)

    with pytest.raises(JobStateError, match="infra job submit"):
        store.require("job-0123456789abcdef")


def test_corrupt_state_is_actionable_and_preserved(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    path = store.path_for("job-0123456789abcdef")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(JobStateError, match="not valid JSON"):
        store.get("job-0123456789abcdef")

    assert path.read_text(encoding="utf-8") == "not-json"


def test_invalid_record_document_is_rejected(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    path = store.path_for("job-0123456789abcdef")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"job_id": "job-0123456789abcdef"}), encoding="utf-8")

    with pytest.raises(JobStateError, match="invalid"):
        store.get("job-0123456789abcdef")


def test_legal_transitions_persist_state_and_reason(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    record = store.create(_record())

    preparing = store.transition(record, JobState.PREPARING, reason="starting")
    running = store.transition(preparing, JobState.RUNNING, reason="running", pid=4321)
    finished = store.transition(running, JobState.SUCCEEDED, reason="exit 0")

    assert finished.state is JobState.SUCCEEDED
    assert finished.state_reason == "exit 0"
    assert finished.pid == 4321
    assert store.get(record.job_id) == finished


def test_illegal_transitions_never_reach_disk(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    record = store.create(_record())

    with pytest.raises(JobSpecError, match="PENDING -> SUCCEEDED"):
        store.transition(record, JobState.SUCCEEDED, reason="impossible")

    assert store.get(record.job_id).state is JobState.PENDING


def test_terminal_states_are_frozen(tmp_path: Path) -> None:
    for index, target in enumerate((JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)):
        store = JobStateStore(tmp_path / f"jobs-{index}", now=lambda: NOW)
        job_id = f"job-{'0' * 15}{index + 1}"
        running = store.create(_record(job_id, JobState.RUNNING))
        terminal = store.transition(running, target, reason="done", failure_reason="kept")

        assert terminal.state is target
        assert terminal.failure_reason == "kept"
        assert store.get(job_id).state is target
        with pytest.raises(JobSpecError):
            store.transition(terminal, JobState.RUNNING, reason="reopen")


def test_save_refreshes_updated_at_and_preserves_creation_time(tmp_path: Path) -> None:
    clock = [NOW]
    store = JobStateStore(tmp_path / "jobs", now=lambda: clock[0])
    record = store.create(_record())

    clock[0] = NOW.replace(hour=13)
    updated = store.save(record.model_copy(update={"exit_code": 0}))

    assert updated.exit_code == 0
    assert updated.updated_at.hour == 13
    assert updated.created_at == record.created_at
    assert store.get(record.job_id).updated_at.hour == 13


def test_write_uses_atomic_replace_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    record = _record()
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def tracked_replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.exists()
        assert source_path.parent == destination_path.parent
        replacements.append((source_path, destination_path))
        original_replace(source, destination)

    monkeypatch.setattr("wavcse_infra.state.os.replace", tracked_replace)
    store.create(record)

    assert len(replacements) == 1
    assert replacements[0][1] == store.path_for(record.job_id)
    assert not list(store.directory.glob(".job-*.tmp"))


def test_local_log_copy_is_bounded_text_without_bearer_material(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    record = store.create(_record())

    path = store.write_log(record.job_id, "epoch 1 loss 0.5\n")

    assert path == store.log_path(record.job_id)
    assert store.read_log(record.job_id) == "epoch 1 loss 0.5\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read_log("job-0000000000000000") is None


def test_persisted_record_never_contains_bearer_material(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MLFLOW_TRACKING_PASSWORD", "presigned?X-Amz-Signature=leaked")
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    record = store.create(_record())

    text = store.path_for(record.job_id).read_text(encoding="utf-8")

    assert not contains_bearer_material(text)
    assert "leaked" not in text


def test_a_record_written_before_the_reconciliation_fields_existed_still_loads(
    tmp_path: Path,
) -> None:
    """Durable records are long-lived: a new field must never invalidate an old file."""

    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    record = store.create(_record("job-0123456789abcdef", JobState.PREPARING))
    path = store.path_for(record.job_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for field in ("preparation_phase", "interrupted_at", "reconciliation_required"):
        payload.pop(field)
    path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = store.require(record.job_id)

    assert reloaded.state is JobState.PREPARING
    assert reloaded.preparation_phase is None
    assert reloaded.interrupted_at is None
    assert reloaded.reconciliation_required is False
