"""Offline coverage for the supplemental network volume state document."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wavcse_infra.errors import StateError, UnresolvedCreateError
from wavcse_infra.models import NetworkVolume, NetworkVolumeSpec, VolumeType
from wavcse_infra.state import (
    DEFAULT_VOLUME_STATE_PATH,
    VolumeLifecycleState,
    VolumeStateStore,
    WorkerStateStore,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _spec(**overrides: object) -> NetworkVolumeSpec:
    values: dict[str, object] = {
        "name": "wavcse-vol-cache-abc123",
        "size_gb": 200,
        "datacenter": "EU-RO-1",
        "volume_type": VolumeType.STANDARD,
    }
    values.update(overrides)
    return NetworkVolumeSpec.model_validate(values)


def _volume(**overrides: object) -> NetworkVolume:
    values: dict[str, object] = {
        "id": "vol-abc123",
        "name": "wavcse-vol-cache-abc123",
        "size_gb": 200,
        "datacenter": "EU-RO-1",
        "volume_type": VolumeType.STANDARD,
    }
    values.update(overrides)
    return NetworkVolume.model_validate(values)


def _store(tmp_path: Path) -> VolumeStateStore:
    return VolumeStateStore(tmp_path / "volumes.json", now=lambda: NOW)


def test_default_state_path_is_a_separate_document() -> None:
    assert DEFAULT_VOLUME_STATE_PATH.name == "volumes.json"
    assert DEFAULT_VOLUME_STATE_PATH.name != "workers.json"


def test_a_create_intent_is_recorded_before_the_paid_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.record_create_intent(_spec())

    assert record.lifecycle_state is VolumeLifecycleState.PENDING_CREATE
    assert record.provider_volume_id is None
    assert record.requested_size_gb == 200
    assert record.requested_data_center == "EU-RO-1"
    assert store.get_by_identity("wavcse-vol-cache-abc123") is not None
    assert [stored.infra_identity for stored in store.list_records()] == ["wavcse-vol-cache-abc123"]


def test_a_duplicate_identity_is_refused_rather_than_recorded_twice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_create_intent(_spec())
    with pytest.raises(StateError, match="already exists for infra identity"):
        store.record_create_intent(_spec())


def test_a_second_intent_is_refused_inside_the_locked_write(tmp_path: Path) -> None:
    """The guard lives in the same atomic transition that writes the intent.

    Evaluating it only before the confirmation prompt would let two overlapping
    `infra volume create` invocations each pass the check and each open a billable volume.
    """

    store = _store(tmp_path)
    store.record_create_intent(_spec())

    with pytest.raises(UnresolvedCreateError, match="while an earlier create is unresolved"):
        store.record_create_intent(_spec(name="wavcse-vol-cache-def456"))

    assert [record.infra_identity for record in store.list_records()] == ["wavcse-vol-cache-abc123"]


def test_created_volume_is_attached_to_its_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = store.record_create_intent(_spec())
    record = store.record_created(intent, _volume())

    assert record.lifecycle_state is VolumeLifecycleState.AVAILABLE
    assert record.provider_volume_id == "vol-abc123"
    assert record.observed_size_gb == 200
    assert record.observed_data_center == "EU-RO-1"
    assert record.observed_volume_type is VolumeType.STANDARD
    assert store.get("vol-abc123") is not None


def test_reconcile_adopts_a_pending_intent_the_provider_confirms(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_create_intent(_spec())
    records = store.reconcile([_volume()])

    assert records[0].lifecycle_state is VolumeLifecycleState.AVAILABLE
    assert records[0].provider_volume_id == "vol-abc123"


def test_reconcile_marks_a_vanished_volume_absent_without_forgetting_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())

    records = store.reconcile([])

    assert records[0].provider_absent is True
    assert records[0].provider_volume_id == "vol-abc123"
    assert store.get("vol-abc123") is not None


def test_reconcile_leaves_an_ambiguous_identity_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_create_intent(_spec())

    records = store.reconcile([_volume(id="vol-one"), _volume(id="vol-two")])

    assert records[0].lifecycle_state is VolumeLifecycleState.PENDING_CREATE
    assert records[0].provider_volume_id is None


def test_reconcile_leaves_an_unconfirmed_intent_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record_create_intent(_spec())
    records = store.reconcile([])

    assert records[0].lifecycle_state is VolumeLifecycleState.PENDING_CREATE
    assert records[0].provider_absent is False


def test_observe_promotes_a_confirmed_pending_intent(tmp_path: Path) -> None:
    """A provider read is confirmation, so the intent stops being reported as pending."""

    store = _store(tmp_path)
    store.record_create_intent(_spec())

    updated = store.observe(_volume())

    assert updated is not None
    assert updated.lifecycle_state is VolumeLifecycleState.AVAILABLE
    assert updated.provider_volume_id == "vol-abc123"


def test_observe_does_not_revive_a_destroyed_record(tmp_path: Path) -> None:
    """A deletion the provider has not caught up with is not silently undone."""

    store = _store(tmp_path)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())
    store.mark_destroyed("vol-abc123")

    updated = store.observe(_volume())

    assert updated is not None
    assert updated.lifecycle_state is VolumeLifecycleState.DESTROYED


def test_reconcile_matches_a_record_by_provider_id_when_the_name_differs(tmp_path: Path) -> None:
    """A renamed or anonymous provider listing must not read as 'absent'."""

    store = _store(tmp_path)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())

    records = store.reconcile([_volume(name="renamed-out-of-band")])

    assert records[0].provider_absent is False
    assert records[0].lifecycle_state is VolumeLifecycleState.AVAILABLE
    assert records[0].provider_volume_id == "vol-abc123"


def test_observe_matches_by_identity_and_not_by_position(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())

    updated = store.observe(_volume(size_gb=300))

    assert updated is not None
    assert updated.observed_size_gb == 300
    assert store.observe(_volume(id="vol-other", name="someone-elses-volume")) is None


def test_mark_destroyed_records_the_explicit_deletion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())

    record = store.mark_destroyed("vol-abc123")

    assert record is not None
    assert record.lifecycle_state is VolumeLifecycleState.DESTROYED
    assert record.provider_absent is True
    assert store.mark_destroyed("vol-unknown") is None


def test_volume_state_is_written_atomically_and_replaced_in_place(tmp_path: Path) -> None:
    path = tmp_path / "volumes.json"
    store = VolumeStateStore(path, now=lambda: NOW)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert set(payload["volumes"]) == {"wavcse-vol-cache-abc123"}
    assert list(tmp_path.glob(".*tmp")) == []


def test_the_volume_document_never_shares_a_path_with_worker_state(tmp_path: Path) -> None:
    volumes = VolumeStateStore(tmp_path / "volumes.json", now=lambda: NOW)
    volumes.record_create_intent(_spec())
    workers = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)

    assert workers.list_records() == []
    assert volumes.list_records() != []


def test_volume_state_is_non_secret(tmp_path: Path) -> None:
    path = tmp_path / "volumes.json"
    store = VolumeStateStore(path, now=lambda: NOW)
    intent = store.record_create_intent(_spec())
    store.record_created(intent, _volume())

    text = path.read_text(encoding="utf-8")
    for forbidden in ("X-Amz-Signature", "Authorization", "Bearer", "runpod-token"):
        assert forbidden not in text


def test_an_unreadable_document_is_reported_rather_than_silently_ignored(tmp_path: Path) -> None:
    path = tmp_path / "volumes.json"
    path.write_text("{not json", encoding="utf-8")
    store = VolumeStateStore(path, now=lambda: NOW)

    with pytest.raises(StateError, match="Could not read network volume state"):
        store.list_records()


def test_lock_path_is_derived_from_the_document(tmp_path: Path) -> None:
    store = VolumeStateStore(tmp_path / "volumes.json", now=lambda: NOW)
    assert store.lock_path() == tmp_path / "volumes.json.lock"
