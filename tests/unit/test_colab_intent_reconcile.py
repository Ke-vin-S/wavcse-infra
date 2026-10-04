"""Recovering an abandoned Colab allocation intent without weakening ambiguous-create safety.

The failure this reproduces: a create whose session never appeared leaves a
``create_pending`` Colab record. Absence from one listing is deliberately not proof that the
allocation never happened, and ``worker destroy``/``bootstrap`` refuse a pending record, so
the intent could never be retired and permanently blocked every later Colab allocation - the
2026-10-01 record ``wavcse-7f8888cdffb84331`` did exactly that.

Every test here is offline: the provider is a scripted object, the state is a temporary
document, and nothing allocates, starts, stops or destroys anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.config import ColabConfig
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ColabQuotaError,
    ProviderUnavailableError,
    StateError,
    UnresolvedCreateError,
)
from wavcse_infra.models import (
    Availability,
    CloudType,
    ExecutionTransport,
    GpuOffer,
    ProviderKind,
    Worker,
    WorkerCreationPlan,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.providers.colab import PINNED_COLAB_VERSION, ColabUsage
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.colab import INTENT_ABANDON_AFTER_HOURS, ColabLifecycle

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
STALE = "wavcse-7f8888cdffb84331"
FRESH = "wavcse-123456789abc"
runner = CliRunner()


class FakeColab:
    """Scripted Colab observations: one listing per call, plus an account snapshot."""

    def __init__(
        self,
        listings: list[list[Worker]],
        *,
        assignments: int = 0,
        error: Exception | None = None,
    ) -> None:
        self._listings = [list(item) for item in listings]
        self.assignments = assignments
        self.error = error
        self.calls: list[str] = []

    def list_workers(self) -> list[Worker]:
        self.calls.append("list_workers")
        if self.error is not None:
            raise self.error
        if len(self._listings) > 1:
            return self._listings.pop(0)
        return list(self._listings[0])

    def usage_snapshot(self) -> ColabUsage:
        self.calls.append("usage_snapshot")
        return ColabUsage(
            paid_balance_cu=Decimal(0),
            rate_cu_per_hour=Decimal(0),
            assignments=self.assignments,
        )


def colab_worker(name: str = STALE, state: WorkerState = WorkerState.RUNNING) -> Worker:
    return Worker(
        provider=ProviderKind.COLAB,
        execution_transport=ExecutionTransport.COLAB_EXEC,
        id=name,
        name=name,
        state=state,
        gpu_type="T4",
        gpu_count=1,
    )


class CreateRefusal(FakeColab):
    """A provider that refuses creation, with listings scripted per observation call.

    The create path filters each listing by the identity it just generated, so the script is
    expressed per call: ``"empty"`` is a listing without it and ``"self"`` a listing that
    contains exactly it. The identity is only knowable once the attempt has happened.
    """

    def __init__(self, script: list[str], *, assignments: int = 0) -> None:
        super().__init__([[]], assignments=assignments)
        self.script = list(script)
        self.attempted: str | None = None
        self.released: list[str] = []

    def validate_gpu(self, gpu: str) -> None:
        self.calls.append("validate_gpu")

    def version(self) -> str:
        return PINNED_COLAB_VERSION

    def create_worker(self, name: str, gpu: str) -> Worker:
        self.attempted = name
        raise ColabQuotaError("account quota/compute units exhausted")

    def destroy_worker(self, record: Any) -> None:
        self.released.append(record.infra_identity)

    def list_workers(self) -> list[Worker]:
        self.calls.append("list_workers")
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if step == "empty" or self.attempted is None:
            return []
        return [colab_worker(self.attempted)]


def intent_store(path: Path, *, age_hours: float, name: str = STALE) -> WorkerStateStore:
    """A store holding one Colab allocation intent recorded ``age_hours`` ago."""

    created = NOW - timedelta(hours=age_hours)
    WorkerStateStore(path, now=lambda: created).record_colab_intent(name, "T4")
    return WorkerStateStore(path, now=lambda: NOW)


def lifecycle(store: WorkerStateStore, client: FakeColab) -> ColabLifecycle:
    return ColabLifecycle(client, store, ColabConfig(enabled=True))  # type: ignore[arg-type]


def runpod_store(path: Path) -> WorkerStateStore:
    """A store holding one ordinary RunPod record, for the provider-isolation tests."""

    store = WorkerStateStore(path, now=lambda: NOW)
    plan = WorkerCreationPlan(
        spec=WorkerSpec(
            name="wavcse-training-abc123",
            gpu_type="NVIDIA RTX A5000",
            gpu_count=1,
            cloud_type=CloudType.COMMUNITY,
            image="runpod/pytorch:example",
            container_disk_gb=30,
            volume_gb=0,
        ),
        offer=GpuOffer(
            gpu_type_id="NVIDIA RTX A5000",
            display_name="RTX A5000",
            cloud_type=CloudType.COMMUNITY,
            gpu_count=1,
            maximum_gpu_count=2,
            availability=Availability.HIGH,
            price_per_gpu_hour=Decimal("0.16"),
            total_price_per_hour=Decimal("0.16"),
        ),
        max_hourly_price=Decimal("0.20"),
    )
    store.record_created(
        plan,
        Worker.model_validate(
            {
                "id": "pod-123",
                "name": "wavcse-training-abc123",
                "state": WorkerState.RUNNING,
                "native_status": "RUNNING",
                "gpu_type": "NVIDIA RTX A5000",
                "gpu_count": 1,
                "cloud_type": CloudType.COMMUNITY,
                "created_at": NOW,
            }
        ),
    )
    return store


def test_the_2026_10_01_failure_is_reconcilable(tmp_path: Path) -> None:
    """An old intent whose identity repeated listings omit becomes terminal and unblocks."""

    store = intent_store(tmp_path / "workers.json", age_hours=72)
    client = FakeColab([[], []], assignments=0)
    colab = lifecycle(store, client)

    assessment = colab.assess_intent(STALE)

    assert assessment.retirable is True
    assert assessment.identity == STALE
    assert assessment.observations == 2
    colab.retire_intent(assessment)

    record = store.get(STALE)
    assert record is not None
    assert record.provider_absent is True
    assert record.create_pending is False
    assert record.last_observed_state is WorkerState.DESTROYED
    # The record is retained as the audit trail of the identity this controller claimed.
    assert store.get(STALE) is not None
    # Only reads ever reached the provider.
    assert client.calls == ["list_workers", "list_workers", "usage_snapshot"]
    # A later allocation is no longer blocked by the retired intent.
    assert store.record_colab_intent("wavcse-" + "b" * 16, "T4").create_pending is True


def test_a_fresh_intent_is_never_retired_and_never_reaches_the_provider(tmp_path: Path) -> None:
    store = intent_store(tmp_path / "workers.json", age_hours=0)
    client = FakeColab([[], []], assignments=0)

    with pytest.raises(StateError, match="in-flight create"):
        lifecycle(store, client).assess_intent(STALE)

    assert client.calls == []
    record = store.get(STALE)
    assert record is not None and record.create_pending and not record.provider_absent


def test_an_intent_just_inside_the_bound_is_still_retained(tmp_path: Path) -> None:
    store = intent_store(
        tmp_path / "workers.json", age_hours=float(INTENT_ABANDON_AFTER_HOURS) - 0.5
    )
    with pytest.raises(StateError, match="in-flight create"):
        lifecycle(store, FakeColab([[], []])).assess_intent(STALE)


def test_a_failed_provider_listing_cannot_retire_an_intent(tmp_path: Path) -> None:
    store = intent_store(tmp_path / "workers.json", age_hours=72)
    client = FakeColab([[]], error=ProviderUnavailableError("provider refused the read"))

    with pytest.raises(ProviderUnavailableError):
        lifecycle(store, client).assess_intent(STALE)

    record = store.get(STALE)
    assert record is not None and record.create_pending and not record.provider_absent


def test_an_identity_the_provider_still_lists_is_never_retired(tmp_path: Path) -> None:
    store = intent_store(tmp_path / "workers.json", age_hours=72)

    with pytest.raises(StateError, match="present in the provider listing"):
        lifecycle(store, FakeColab([[colab_worker()], [colab_worker()]])).assess_intent(STALE)

    record = store.get(STALE)
    assert record is not None and record.create_pending and not record.provider_absent


def test_disagreeing_observations_fail_closed(tmp_path: Path) -> None:
    """An unrelated session appearing between reads is a disagreement, not absolution."""

    store = intent_store(tmp_path / "workers.json", age_hours=72)
    other = colab_worker("wavcse-" + "c" * 16)
    with pytest.raises(StateError, match="disagree"):
        lifecycle(store, FakeColab([[], [other]], assignments=1)).assess_intent(STALE)
    with pytest.raises(StateError, match="disagree"):
        lifecycle(store, FakeColab([[], []], assignments=1)).assess_intent(STALE)
    record = store.get(STALE)
    assert record is not None and record.create_pending and not record.provider_absent


def test_an_unknown_identity_is_refused(tmp_path: Path) -> None:
    store = intent_store(tmp_path / "workers.json", age_hours=72)
    with pytest.raises(StateError, match="No tracked worker record"):
        lifecycle(store, FakeColab([[], []])).assess_intent("wavcse-" + "d" * 16)


def test_a_runpod_record_is_never_reconciled_as_a_colab_intent(tmp_path: Path) -> None:
    store = runpod_store(tmp_path / "workers.json")
    with pytest.raises(StateError, match="not a Colab session"):
        lifecycle(store, FakeColab([[], []])).assess_intent("pod-123")
    assert store.mark_colab_intent_absent("pod-123") is None
    assert store.get("pod-123").provider_absent is False


def test_a_confirmed_allocation_is_not_an_intent(tmp_path: Path) -> None:
    """A record whose session exists is released with `worker destroy`, never retired here."""

    store = intent_store(tmp_path / "workers.json", age_hours=72)
    store.record_colab_created(colab_worker())

    with pytest.raises(StateError, match="confirmed allocation"):
        lifecycle(store, FakeColab([[], []])).assess_intent(STALE)

    assert store.mark_colab_intent_absent(STALE) is None
    record = store.get(STALE)
    assert record is not None and not record.provider_absent


def test_repeated_reconciliation_is_idempotent(tmp_path: Path) -> None:
    store = intent_store(tmp_path / "workers.json", age_hours=72)
    client = FakeColab([[], []])
    colab = lifecycle(store, client)
    colab.retire_intent(colab.assess_intent(STALE))
    first_state = store.get(STALE)

    second = colab.assess_intent(STALE)
    assert second.retirable is False
    assert second.detail == "the intent is already terminal and provider-absent"
    assert colab.retire_intent(second) is None
    # The second pass reads nothing from the provider and changes no local state.
    assert client.calls == ["list_workers", "list_workers", "usage_snapshot"]
    assert store.get(STALE) == first_state


def test_the_recovery_path_cannot_release_the_way_for_a_duplicate(tmp_path: Path) -> None:
    """A refused reconciliation leaves the intent unresolved, so allocation stays blocked."""

    store = intent_store(tmp_path / "workers.json", age_hours=0)
    with pytest.raises(StateError):
        lifecycle(store, FakeColab([[], []])).assess_intent(STALE)
    with pytest.raises(UnresolvedCreateError, match="unresolved allocation intent"):
        store.record_colab_intent("wavcse-" + "e" * 16, "T4")


def test_confirmed_ambiguous_create_recovery_still_retires_its_own_intent(tmp_path: Path) -> None:
    """The create path's own two-empty-listing rule is unchanged by the new transition."""

    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    client = CreateRefusal(["empty", "empty", "empty"])
    with pytest.raises(ColabQuotaError):
        lifecycle(store, client).create("T4")

    intent = next(
        record for record in store.list_records() if record.provider is ProviderKind.COLAB
    )
    assert intent.provider_absent is True
    assert intent.create_pending is True  # the create path's own transition is untouched


def test_a_create_that_did_land_is_confirmed_and_released_as_before(tmp_path: Path) -> None:
    """Two listings that both show the identity confirm it, then release it."""

    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    client = CreateRefusal(["empty", "self", "self", "empty"])
    with pytest.raises(ColabQuotaError):
        lifecycle(store, client).create("T4")

    intent = next(
        record for record in store.list_records() if record.provider is ProviderKind.COLAB
    )
    assert client.released == [intent.infra_identity]
    assert intent.provider_absent is True and intent.create_pending is False


def test_disagreeing_create_observations_stay_ambiguous(tmp_path: Path) -> None:
    """A create that cannot prove absence or presence keeps its intent unresolved."""

    store = WorkerStateStore(tmp_path / "workers.json", now=lambda: NOW)
    client = CreateRefusal(["empty", "empty", "self"])
    with pytest.raises(AmbiguousCreateError):
        lifecycle(store, client).create("T4")

    intent = next(
        record for record in store.list_records() if record.provider is ProviderKind.COLAB
    )
    assert intent.create_pending is True and intent.provider_absent is False
    # And a second allocation is still refused while that intent is unresolved.
    with pytest.raises(UnresolvedCreateError):
        store.record_colab_intent("wavcse-" + "e" * 16, "T4")


def _colab_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[colab]\nenabled = true\n", encoding="utf-8")
    return config_file


def _cli_fake(client: Any) -> Any:
    class FakeClient:
        def __init__(self, config: Any) -> None:
            assert getattr(config, "enabled", False)

        def list_workers(self) -> list[Worker]:
            return client.list_workers()

        def usage_snapshot(self) -> ColabUsage:
            return client.usage_snapshot()

    return FakeClient


def test_cli_retires_an_abandoned_intent_without_touching_runpod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "workers.json"
    store = intent_store(path, age_hours=72)
    client = FakeColab([[], []], assignments=0)
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    monkeypatch.setattr(cli, "ColabClient", _cli_fake(client))
    monkeypatch.setattr(
        cli.RunPodClient,
        "from_settings",
        lambda settings: (_ for _ in ()).throw(AssertionError("RunPod must not be resolved")),
    )
    config_file = _colab_config(tmp_path)

    result = runner.invoke(
        cli.app,
        ["--config", str(config_file), "worker", "reconcile", STALE, "--yes"],
        env={},
    )

    assert result.exit_code == 0, result.output
    assert "terminal and provider-absent" in result.stdout
    assert "only the local" in result.stdout
    record = store.get(STALE)
    assert record is not None and record.provider_absent and not record.create_pending


def test_cli_confirmation_is_required_before_local_state_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "workers.json"
    store = intent_store(path, age_hours=72)
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    monkeypatch.setattr(cli, "ColabClient", _cli_fake(FakeColab([[], []])))
    config_file = _colab_config(tmp_path)

    result = runner.invoke(
        cli.app, ["--config", str(config_file), "worker", "reconcile", STALE], env={}
    )

    assert result.exit_code != 0
    record = store.get(STALE)
    assert record is not None and record.create_pending and not record.provider_absent


def test_cli_refuses_an_unowned_or_runpod_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = runpod_store(tmp_path / "workers.json")
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    monkeypatch.setattr(
        cli,
        "ColabClient",
        lambda config: (_ for _ in ()).throw(AssertionError("must not reach the provider")),
    )
    config_file = _colab_config(tmp_path)

    unknown = runner.invoke(
        cli.app, ["--config", str(config_file), "worker", "reconcile", "wavcse-" + "f" * 16]
    )
    assert unknown.exit_code == 0
    assert "No tracked worker record" in unknown.stdout

    runpod = runner.invoke(
        cli.app, ["--config", str(config_file), "worker", "reconcile", "pod-123"]
    )
    assert runpod.exit_code == 2
    assert "only an unresolved" in runpod.stderr
    assert store.get("pod-123").provider_absent is False
