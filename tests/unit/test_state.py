import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from wavcse_infra import state
from wavcse_infra.errors import StateError
from wavcse_infra.models import (
    Availability,
    CloudType,
    GpuOffer,
    HealthCheckStatus,
    Worker,
    WorkerConnectionInfo,
    WorkerCreationPlan,
    WorkerGpuInfo,
    WorkerHealthCheck,
    WorkerHealthReport,
    WorkerReadinessState,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.state import WorkerStateStore

NOW = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def _plan() -> WorkerCreationPlan:
    spec = WorkerSpec(
        name="wavcse-training-abc123",
        gpu_type="NVIDIA RTX A5000",
        gpu_count=1,
        cloud_type=CloudType.COMMUNITY,
        image="runpod/pytorch:example",
        container_disk_gb=30,
        volume_gb=20,
    )
    offer = GpuOffer(
        gpu_type_id=spec.gpu_type,
        display_name="RTX A5000",
        cloud_type=spec.cloud_type,
        gpu_count=spec.gpu_count,
        maximum_gpu_count=2,
        availability=Availability.HIGH,
        price_per_gpu_hour=Decimal("0.16"),
        total_price_per_hour=Decimal("0.16"),
    )
    return WorkerCreationPlan(spec=spec, offer=offer, max_hourly_price=Decimal("0.20"))


def _worker(**overrides) -> Worker:
    values = {
        "id": "pod-123",
        "name": "wavcse-training-abc123",
        "state": WorkerState.PROVISIONING,
        "native_status": "PROVISIONING",
        "gpu_type": "NVIDIA RTX A5000",
        "gpu_count": 1,
        "cloud_type": CloudType.COMMUNITY,
        "hourly_cost": Decimal("0.15"),
        "image": "runpod/pytorch:example",
        "container_disk_gb": 30,
        "volume_gb": 20,
        "created_at": NOW,
    }
    values.update(overrides)
    return Worker.model_validate(values)


def test_created_worker_state_persists_requested_and_actual_metadata(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "workers.json"
    store = WorkerStateStore(state_path, now=lambda: NOW)

    record = store.record_created(_plan(), _worker())

    assert record.provider_worker_id == "pod-123"
    assert record.infra_identity == "wavcse-training-abc123"
    assert record.requested_gpu_type == "NVIDIA RTX A5000"
    assert record.actual_gpu_type == "NVIDIA RTX A5000"
    assert record.requested_cloud_type is CloudType.COMMUNITY
    assert record.known_hourly_price == Decimal("0.15")
    assert store.get("pod-123") == record
    payload = state_path.read_text(encoding="utf-8")
    assert "RUNPOD_API_KEY" not in payload
    assert "Authorization" not in payload
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700


def test_state_write_uses_atomic_replace_in_same_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state" / "workers.json"
    replacements: list[tuple[Path, Path]] = []
    original_replace = os.replace

    def tracked_replace(source, destination) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.exists()
        assert source_path.parent == destination_path.parent
        replacements.append((source_path, destination_path))
        original_replace(source, destination)

    monkeypatch.setattr(state.os, "replace", tracked_replace)
    store = WorkerStateStore(state_path, now=lambda: NOW)

    store.record_created(_plan(), _worker())

    assert len(replacements) == 1
    assert replacements[0][1] == state_path
    assert not list(state_path.parent.glob(".workers-*.tmp"))


def test_reconcile_updates_known_workers_and_marks_absent_workers_destroyed(
    tmp_path: Path,
) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    store.record_created(_plan(), _worker())

    store.reconcile([])

    absent = store.get("pod-123")
    assert absent is not None
    assert absent.provider_absent
    assert absent.last_observed_state is WorkerState.DESTROYED

    running = _worker(state=WorkerState.RUNNING, native_status="RUNNING")
    store.reconcile([running])
    observed = store.get("pod-123")
    assert observed is not None
    assert not observed.provider_absent
    assert observed.last_observed_state is WorkerState.RUNNING


def test_stopped_zero_compute_cost_preserves_known_running_price(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    store.record_created(_plan(), _worker(hourly_cost=Decimal("0.15")))

    store.observe(
        _worker(
            state=WorkerState.STOPPED,
            native_status="EXITED",
            hourly_cost=Decimal("0"),
        )
    )

    record = store.get("pod-123")
    assert record is not None
    assert record.known_hourly_price == Decimal("0.15")


def test_corrupt_state_is_actionable_and_never_silently_overwritten(tmp_path: Path) -> None:
    state_path = tmp_path / "workers.json"
    state_path.write_text("not-json", encoding="utf-8")
    store = WorkerStateStore(state_path)

    with pytest.raises(StateError, match="Could not read worker state"):
        store.list_records()

    assert state_path.read_text(encoding="utf-8") == "not-json"


def test_state_document_is_versioned_json(tmp_path: Path) -> None:
    state_path = tmp_path / "workers.json"
    WorkerStateStore(state_path, now=lambda: NOW).record_created(_plan(), _worker())

    payload = json.loads(state_path.read_text(encoding="utf-8"))

    assert payload["version"] == 1
    assert list(payload["workers"]) == ["pod-123"]


def test_readiness_transitions_persist_health_and_reset_when_stopped(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    connection = WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="direct",
        host="203.0.113.9",
        port=30222,
        username="root",
    )
    running = _worker(
        state=WorkerState.RUNNING,
        native_status="RUNNING",
        ssh_direct=connection,
    )
    store.record_created(_plan(), running)

    store.mark_ssh_ready(running, connection)
    store.mark_bootstrapped("pod-123", "1")
    store.mark_gpu_healthy("pod-123")
    report = WorkerHealthReport(
        provider_worker_id="pod-123",
        provider_state=WorkerState.RUNNING,
        readiness_state=WorkerReadinessState.READY,
        connection=connection,
        bootstrap_version_expected="1",
        bootstrap_version_observed="1",
        disk_path="/workspace",
        disk_available_bytes=50 * 1024**3,
        git_version="git version 2.43.0",
        python_version="Python 3.12.3",
        uv_version="uv 0.10.9",
        gpu=WorkerGpuInfo(
            count=1,
            models=("NVIDIA RTX A4000",),
            memory_mib=(16376,),
            driver_version="550.54.15",
            cuda_version="12.8",
        ),
        checks=(
            WorkerHealthCheck(name="gpu", status=HealthCheckStatus.PASS, detail="one NVIDIA GPU"),
        ),
    )
    store.record_health(report)

    ready = store.get("pod-123")
    assert ready is not None
    assert ready.readiness_state is WorkerReadinessState.READY
    assert ready.last_ssh_ready_at == NOW
    assert ready.bootstrap_version == "1"
    assert ready.health_status == "READY"
    assert ready.observed_gpu_models == ("NVIDIA RTX A4000",)
    assert ready.disk_available_bytes == 50 * 1024**3
    serialized = (tmp_path / "workers.json").read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in serialized
    assert "Authorization" not in serialized

    store.observe(_worker(state=WorkerState.STOPPED, native_status="EXITED"))
    stopped = store.get("pod-123")
    assert stopped is not None
    assert stopped.readiness_state is WorkerReadinessState.NOT_READY
