import json
import os
import stat
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from wavcse_infra import state
from wavcse_infra.errors import StateError
from wavcse_infra.models import (
    Availability,
    CloudType,
    ExecutionTransport,
    GpuOffer,
    HealthCheckStatus,
    ProviderKind,
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


def test_a_mount_path_is_only_recorded_when_the_provider_reported_one(tmp_path: Path) -> None:
    """A create answer that omits `mounts` must not fabricate a confirmed mount path.

    The recorded path decides whether a later job treats a directory as cache-backed, so a
    requested path persisted as if the provider had reported it would make an ordinary
    container-disk directory look like the network volume.
    """

    plan = _plan()
    spec = plan.spec.model_copy(
        update={
            "cloud_type": CloudType.SECURE,
            "network_volume_id": "vol-abc123",
            "volume_mount_path": "/workspace/cache",
        }
    )
    plan = plan.model_copy(update={"spec": spec})
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)

    silent = store.record_created(plan, _worker(cloud_type=CloudType.SECURE))
    assert silent.network_volume_id == "vol-abc123"
    assert silent.network_volume_mount_path is None

    reported = store.record_created(
        plan,
        _worker(cloud_type=CloudType.SECURE, volume_mount_path="/workspace/cache"),
    )
    assert reported.network_volume_mount_path == "/workspace/cache"


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


def test_colab_intent_and_provider_scoped_reconciliation_preserve_runpod(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    store.record_created(_plan(), _worker(state=WorkerState.RUNNING))
    intent = store.record_colab_intent("wavcse-123456789abc", "T4")
    assert intent.execution_transport is ExecutionTransport.COLAB_EXEC
    assert intent.requested_cloud_type is None
    assert intent.image is None
    assert intent.create_pending
    assert store.provider_for("wavcse-123456789abc") is ProviderKind.COLAB

    # A RunPod-only listing cannot declare Colab absent; a missing Colab listing
    # cannot clear an unresolved, potentially billable allocation intent.
    store.reconcile([_worker(state=WorkerState.RUNNING)])
    store.reconcile([], provider=ProviderKind.COLAB)
    assert store.get("pod-123").last_observed_state is WorkerState.RUNNING
    assert store.get("wavcse-123456789abc").create_pending

    colab = Worker(
        provider=ProviderKind.COLAB,
        execution_transport=ExecutionTransport.COLAB_EXEC,
        id="wavcse-123456789abc",
        name="wavcse-123456789abc",
        state=WorkerState.RUNNING,
        gpu_type="T4",
        gpu_count=1,
    )
    store.reconcile([colab], provider=ProviderKind.COLAB)
    adopted = store.get("wavcse-123456789abc")
    assert not adopted.create_pending
    assert adopted.actual_gpu_type == "T4"
    observed = store.record_colab_created(colab)
    assert observed.actual_gpu_type == "T4"
    assert not observed.create_pending
    store.reconcile([colab], provider=ProviderKind.COLAB)
    assert store.get("pod-123").last_observed_state is WorkerState.RUNNING
    store.reconcile([], provider=ProviderKind.COLAB)
    assert store.get("wavcse-123456789abc").provider_absent


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

    failed_report = report.model_copy(
        update={
            "readiness_state": WorkerReadinessState.FAILED,
            "checks": (
                WorkerHealthCheck(
                    name="volume",
                    status=HealthCheckStatus.FAIL,
                    detail="required persistent volume is not mounted",
                ),
            ),
        }
    )
    store.record_health(failed_report)
    failed = store.get("pod-123")
    assert failed is not None
    assert failed.readiness_state is WorkerReadinessState.FAILED
    assert failed.health_status == "FAILED"

    store.observe(_worker(state=WorkerState.STOPPED, native_status="EXITED"))
    stopped = store.get("pod-123")
    assert stopped is not None
    assert stopped.readiness_state is WorkerReadinessState.NOT_READY


# --- readiness is a ladder, not a last-writer-wins flag ----------------------------


def _ready_worker_pair():
    """Return one running worker plus the direct endpoint it answers on."""

    connection = WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="direct",
        host="203.0.113.9",
        port=30222,
        username="root",
    )
    worker = _worker(state=WorkerState.RUNNING, native_status="RUNNING", ssh_direct=connection)
    return worker, connection


def _health_report(connection: WorkerConnectionInfo, readiness: WorkerReadinessState):
    return WorkerHealthReport(
        provider_worker_id="pod-123",
        provider_state=WorkerState.RUNNING,
        readiness_state=readiness,
        connection=connection,
        bootstrap_version_expected="1",
        bootstrap_version_observed="1",
        checks=(
            WorkerHealthCheck(name="shell", status=HealthCheckStatus.PASS, detail="shell works"),
        ),
    )


def test_successful_ssh_probe_keeps_established_readiness(tmp_path: Path) -> None:
    """A read-only SSH observation must never downgrade a stronger proven capability."""

    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    running, connection = _ready_worker_pair()
    store.record_created(_plan(), running)
    store.mark_ssh_ready(running, connection)
    store.mark_bootstrapped("pod-123", "1")
    store.mark_gpu_healthy("pod-123")
    store.record_health(_health_report(connection, WorkerReadinessState.READY))

    for _ in range(3):
        record = store.mark_ssh_ready(running, connection)

    assert record is not None
    assert record.readiness_state is WorkerReadinessState.READY
    assert record.last_ssh_ready_at == NOW


def test_successful_ssh_probe_keeps_bootstrapped_readiness(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    running, connection = _ready_worker_pair()
    store.record_created(_plan(), running)
    store.mark_bootstrapped("pod-123", "1")

    record = store.mark_ssh_ready(running, connection)

    assert record is not None
    assert record.readiness_state is WorkerReadinessState.BOOTSTRAPPED


def test_successful_ssh_probe_promotes_a_weaker_readiness(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    running, connection = _ready_worker_pair()
    store.record_created(_plan(), running)

    fresh = store.mark_ssh_ready(running, connection)
    failed_then_probed = store.mark_ssh_ready(running, connection)

    assert fresh is not None and fresh.readiness_state is WorkerReadinessState.SSH_READY
    assert failed_then_probed is not None
    assert failed_then_probed.readiness_state is WorkerReadinessState.SSH_READY


def test_ssh_probe_replaces_a_failed_health_marker(tmp_path: Path) -> None:
    """FAILED means the last inspection found a problem, not that SSH stopped working."""

    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    running, connection = _ready_worker_pair()
    store.record_created(_plan(), running)
    store.record_health(_health_report(connection, WorkerReadinessState.FAILED))

    record = store.mark_ssh_ready(running, connection)

    assert record is not None
    assert record.readiness_state is WorkerReadinessState.SSH_READY
    assert record.health_status == "FAILED"


def test_gpu_checkpoint_never_downgrades_established_readiness(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    running, connection = _ready_worker_pair()
    store.record_created(_plan(), running)
    store.record_health(_health_report(connection, WorkerReadinessState.READY))

    record = store.mark_gpu_healthy("pod-123")

    assert record is not None
    assert record.readiness_state is WorkerReadinessState.READY


def test_genuine_invalidation_still_resets_readiness(tmp_path: Path) -> None:
    """A stopped, destroyed, or unhealthy worker must lose its READY evidence."""

    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    running, connection = _ready_worker_pair()
    store.record_created(_plan(), running)
    store.record_health(_health_report(connection, WorkerReadinessState.READY))

    store.observe(_worker(state=WorkerState.STOPPED, native_status="EXITED"))
    assert store.get("pod-123").readiness_state is WorkerReadinessState.NOT_READY

    store.record_health(_health_report(connection, WorkerReadinessState.READY))
    store.mark_destroyed("pod-123")
    assert store.get("pod-123").readiness_state is WorkerReadinessState.NOT_READY

    store.record_health(_health_report(connection, WorkerReadinessState.READY))
    store.record_health(_health_report(connection, WorkerReadinessState.FAILED))
    assert store.get("pod-123").readiness_state is WorkerReadinessState.FAILED


def test_a_concurrent_ssh_probe_cannot_regress_a_health_result(tmp_path: Path) -> None:
    """Two controller processes must not lose the stronger readiness either wrote.

    Without the document lock the probe's read-decide-write window lets the health result
    land first and then be overwritten by the probe's stale observation, leaving a READY
    worker recorded as merely SSH_READY.
    """

    path = tmp_path / "workers.json"
    running, connection = _ready_worker_pair()
    probe_store = WorkerStateStore(path, now=lambda: NOW)
    probe_store.record_created(_plan(), running)
    health_store = WorkerStateStore(path, now=lambda: NOW)
    probe_ready = threading.Event()
    health_attempted = threading.Event()

    original_load = probe_store._load
    original_write = probe_store._write

    def noticed_load():  # type: ignore[no-untyped-def]
        # Announced once the probe has read the document inside its lock, which is exactly
        # the window the health writer must not be able to enter.
        document = original_load()
        probe_ready.set()
        return document

    def delayed_write(document):  # type: ignore[no-untyped-def]
        assert probe_ready.is_set()
        # The health decision is issued now, while the probe still holds the lock.
        health_attempted.set()
        original_write(document)

    probe_store._load = noticed_load  # type: ignore[method-assign]
    probe_store._write = delayed_write  # type: ignore[method-assign]

    probe_results: list[object] = []

    def probe() -> None:
        probe_results.append(probe_store.mark_ssh_ready(running, connection))

    thread = threading.Thread(target=probe)
    thread.start()
    try:
        assert probe_ready.wait(timeout=30)
        health_store.record_health(_health_report(connection, WorkerReadinessState.READY))
    finally:
        thread.join(timeout=60)

    assert not thread.is_alive(), "the probe was blocked by the health writer"
    final = health_store.get("pod-123")
    assert final is not None
    assert final.readiness_state is WorkerReadinessState.READY
    # Neither writer's evidence was lost.
    assert final.health_status == "READY"
    assert final.last_ssh_ready_at == NOW
