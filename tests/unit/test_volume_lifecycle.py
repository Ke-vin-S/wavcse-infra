"""Offline coverage for network volume lifecycle decisions and Pod placement constraints."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from wavcse_infra.errors import (
    AmbiguousCreateError,
    LifecycleTimeoutError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderUnavailableError,
    ProviderValidationError,
    StateError,
    UnresolvedCreateError,
    WorkerPlacementError,
)
from wavcse_infra.models import (
    Availability,
    CloudType,
    DataCenterInfo,
    GpuOffer,
    NetworkVolume,
    NetworkVolumeBilling,
    NetworkVolumeCreationPlan,
    NetworkVolumeSpec,
    VolumeType,
    Worker,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.state import VolumeStateStore, WorkerStateStore
from wavcse_infra.volumes.lifecycle import (
    VolumeLifecycle,
    constrain_worker_spec_to_volume,
)
from wavcse_infra.workers.lifecycle import WorkerLifecycle

POD_ID = "pod-123"
VOLUME_ID = "vol-abc123"
VOLUME_NAME = "wavcse-vol-cache-abc123"


def _data_center(**overrides: object) -> DataCenterInfo:
    values: dict[str, object] = {
        "id": "EU-RO-1",
        "name": "Romania 1",
        "region": "EUROPE",
        "network_volume_types": (VolumeType.STANDARD,),
    }
    values.update(overrides)
    return DataCenterInfo.model_validate(values)


def _spec(**overrides: object) -> NetworkVolumeSpec:
    values: dict[str, object] = {
        "name": VOLUME_NAME,
        "size_gb": 200,
        "datacenter": "EU-RO-1",
        "volume_type": VolumeType.STANDARD,
    }
    values.update(overrides)
    return NetworkVolumeSpec.model_validate(values)


def _volume(**overrides: object) -> NetworkVolume:
    values: dict[str, object] = {
        "id": VOLUME_ID,
        "name": VOLUME_NAME,
        "size_gb": 200,
        "datacenter": "EU-RO-1",
        "volume_type": VolumeType.STANDARD,
    }
    values.update(overrides)
    return NetworkVolume.model_validate(values)


def _plan(**overrides: object) -> NetworkVolumeCreationPlan:
    values: dict[str, object] = {"spec": _spec(), "data_center": _data_center()}
    values.update(overrides)
    return NetworkVolumeCreationPlan.model_validate(values)


class FakeVolumeProvider:
    """Provider double recording every volume and Pod mutation it is asked to perform."""

    def __init__(
        self,
        *,
        data_centers: Sequence[DataCenterInfo] | None = None,
        volumes: Sequence[NetworkVolume] | None = None,
        get_results: Sequence[object] | None = None,
        create_result: object | None = None,
        sticky_get_result: object | None = None,
    ) -> None:
        self.data_centers = list(data_centers if data_centers is not None else [_data_center()])
        self.volumes = list(volumes or [])
        self.get_results = list(get_results or [])
        self.create_result = create_result
        self.sticky_get_result = sticky_get_result
        self.create_calls = 0
        self.create_specs: list[NetworkVolumeSpec] = []
        self.volume_destroy_calls: list[str] = []
        self.worker_destroy_calls: list[str] = []

    def list_network_volumes(self) -> list[NetworkVolume]:
        return list(self.volumes)

    def get_network_volume(self, volume_id: str) -> NetworkVolume:
        if self.get_results:
            result = self.get_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result  # type: ignore[return-value]
        if self.sticky_get_result is not None:
            result = self.sticky_get_result
            if isinstance(result, Exception):
                raise result
            return result  # type: ignore[return-value]
        raise ProviderNotFoundError(f"RunPod network volume {volume_id} was not found (HTTP 404)")

    def create_network_volume(self, spec: NetworkVolumeSpec) -> NetworkVolume:
        self.create_calls += 1
        self.create_specs.append(spec)
        if isinstance(self.create_result, Exception):
            raise self.create_result
        assert self.create_result is not None
        return self.create_result  # type: ignore[return-value]

    def destroy_network_volume(self, volume_id: str) -> None:
        self.volume_destroy_calls.append(volume_id)

    def list_data_centers(self, *, include_gpu_availability: bool = False) -> list[DataCenterInfo]:
        del include_gpu_availability
        return list(self.data_centers)

    def list_network_volume_billing(
        self,
        *,
        volume_id: str | None = None,
        last_n: int | None = None,
    ) -> NetworkVolumeBilling:
        del volume_id, last_n
        return NetworkVolumeBilling(total_amount_usd=Decimal(0))

    # Present only so a test can prove a Pod destroy never reaches it.
    def destroy_worker(self, worker_id: str) -> None:
        self.worker_destroy_calls.append(worker_id)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.value += delay


def _lifecycle(
    provider: FakeVolumeProvider,
    tmp_path: Path,
    *,
    clock: FakeClock | None = None,
) -> VolumeLifecycle:
    ticking = clock or FakeClock()
    return VolumeLifecycle(
        provider,  # type: ignore[arg-type]
        VolumeStateStore(tmp_path / "volumes.json"),
        default_timeout_seconds=10.0,
        poll_interval_seconds=1.0,
        max_poll_interval_seconds=4.0,
        sleep=ticking.sleep,
        monotonic=ticking.monotonic,
    )


def _ambiguous_destroy(provider: FakeVolumeProvider):
    """A destroy that loses its response after being issued exactly once."""

    def destroy(volume_id: str) -> None:
        provider.volume_destroy_calls.append(volume_id)
        raise ProviderOperationAmbiguousError("lost the destroy response")

    return destroy


# --- planning -------------------------------------------------------------------------


def test_plan_create_refuses_an_unknown_data_center(tmp_path: Path) -> None:
    with pytest.raises(ProviderValidationError, match="does not report a data center"):
        _lifecycle(FakeVolumeProvider(), tmp_path).plan_create(_spec(datacenter="US-NOWHERE-9"))


def test_plan_create_refuses_a_data_center_without_network_volume_support(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(data_centers=[_data_center(network_volume_types=())])
    with pytest.raises(ProviderValidationError, match="does not support network volumes at all"):
        _lifecycle(provider, tmp_path).plan_create(_spec())


def test_plan_create_refuses_a_tier_the_data_center_does_not_offer(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(data_centers=[_data_center()])
    with pytest.raises(ProviderValidationError, match="cannot host network volume tier"):
        _lifecycle(provider, tmp_path).plan_create(_spec(volume_type=VolumeType.HIGH_PERFORMANCE))


def test_plan_create_quotes_the_published_list_price_and_marks_it_as_such(tmp_path: Path) -> None:
    plan = _lifecycle(FakeVolumeProvider(), tmp_path).plan_create(_spec())

    assert plan.published_list_price_usd_per_gb_month == Decimal("0.07")
    assert plan.estimated_monthly_cost_usd == Decimal("14.00")


def test_plan_create_has_no_price_for_a_premium_tier(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(
        data_centers=[
            _data_center(network_volume_types=(VolumeType.HIGH_PERFORMANCE, VolumeType.STANDARD))
        ]
    )
    plan = _lifecycle(provider, tmp_path).plan_create(
        _spec(volume_type=VolumeType.HIGH_PERFORMANCE)
    )

    assert plan.published_list_price_usd_per_gb_month is None
    assert plan.estimated_monthly_cost_usd is None


def test_plan_create_infers_the_default_tier_of_a_single_tier_data_center(tmp_path: Path) -> None:
    plan = _lifecycle(FakeVolumeProvider(), tmp_path).plan_create(_spec(volume_type=None))

    assert plan.published_list_price_usd_per_gb_month == Decimal("0.07")


def test_plan_create_cannot_price_a_default_tier_it_cannot_know(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(
        data_centers=[
            _data_center(network_volume_types=(VolumeType.HIGH_PERFORMANCE, VolumeType.STANDARD))
        ]
    )
    plan = _lifecycle(provider, tmp_path).plan_create(_spec(volume_type=None))

    assert plan.published_list_price_usd_per_gb_month is None


# --- creation -------------------------------------------------------------------------


def test_create_records_the_intent_before_the_paid_request(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(create_result=_volume())

    created = _lifecycle(provider, tmp_path).create(_plan())

    assert provider.create_calls == 1
    assert provider.create_specs[0].name == VOLUME_NAME
    assert created.volume.id == VOLUME_ID
    assert created.record.provider_volume_id == VOLUME_ID


def test_an_ambiguous_create_leaves_the_intent_reconcilable(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(create_result=AmbiguousCreateError("may have created a volume"))
    lifecycle = _lifecycle(provider, tmp_path)

    with pytest.raises(AmbiguousCreateError):
        lifecycle.create(_plan())

    records = VolumeStateStore(tmp_path / "volumes.json").list_records()
    assert [record.infra_identity for record in records] == [VOLUME_NAME]
    assert records[0].provider_volume_id is None
    assert records[0].requested_data_center == "EU-RO-1"


def test_create_refuses_a_second_request_under_the_same_identity(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(create_result=_volume())
    lifecycle = _lifecycle(provider, tmp_path)
    lifecycle.create(_plan())

    with pytest.raises(StateError, match="already exists for infra identity"):
        lifecycle.create(_plan())

    assert provider.create_calls == 1, "a duplicate identity must never reach the provider"


# --- destruction ----------------------------------------------------------------------


def test_destroy_is_exact_id_and_marks_the_local_record(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(
        create_result=_volume(),
        get_results=[_volume(), ProviderNotFoundError("gone")],
    )
    lifecycle = _lifecycle(provider, tmp_path)
    lifecycle.create(_plan())

    result = lifecycle.destroy(VOLUME_ID)

    assert result.already_absent is False
    assert provider.volume_destroy_calls == [VOLUME_ID]
    records = VolumeStateStore(tmp_path / "volumes.json").list_records()
    assert records[0].lifecycle_state.value == "DESTROYED"


def test_destroy_of_an_absent_volume_is_a_no_op(tmp_path: Path) -> None:
    provider = FakeVolumeProvider()

    result = _lifecycle(provider, tmp_path).destroy("vol-gone")

    assert result.already_absent is True
    assert provider.volume_destroy_calls == []


def test_destroy_reconciles_an_ambiguous_result_by_polling(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(
        get_results=[
            _volume(),
            ProviderUnavailableError("transient read failure"),
            ProviderNotFoundError("gone"),
        ]
    )
    provider.destroy_network_volume = _ambiguous_destroy(provider)  # type: ignore[method-assign]

    result = _lifecycle(provider, tmp_path).destroy(VOLUME_ID)

    assert result.already_absent is False
    assert provider.volume_destroy_calls == [VOLUME_ID], "the destroy must not be repeated"


def test_destroy_gives_up_boundedly_when_the_volume_never_goes_away(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(sticky_get_result=_volume())
    provider.destroy_network_volume = _ambiguous_destroy(provider)  # type: ignore[method-assign]
    clock = FakeClock()

    with pytest.raises(LifecycleTimeoutError, match="still exists after"):
        _lifecycle(provider, tmp_path, clock=clock).destroy(VOLUME_ID)

    assert clock.delays, "the wait must be bounded polling, not a busy loop"


def test_destroying_a_volume_never_destroys_a_pod(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(get_results=[_volume(), ProviderNotFoundError("gone")])

    _lifecycle(provider, tmp_path).destroy(VOLUME_ID)

    assert provider.worker_destroy_calls == []


# --- Pod placement constraints --------------------------------------------------------


def _worker_spec(**overrides: object) -> WorkerSpec:
    values: dict[str, object] = {
        "name": "wavcse-cache-abc123",
        "gpu_type": "NVIDIA L4",
        "gpu_count": 1,
        "cloud_type": CloudType.SECURE,
        "image": "runpod/pytorch:example",
        "network_volume_id": VOLUME_ID,
        "volume_mount_path": "/workspace/cache",
        "start_ssh": True,
    }
    values.update(overrides)
    return WorkerSpec.model_validate(values)


def test_constraining_a_spec_sets_the_volume_data_center() -> None:
    constrained = constrain_worker_spec_to_volume(_worker_spec(), _volume())

    assert constrained.data_center_ids == ("EU-RO-1",)


def test_constraining_rejects_a_different_requested_data_center() -> None:
    spec = _worker_spec(data_center_ids=("US-KS-2",))
    with pytest.raises(ProviderValidationError, match="can only be placed in"):
        constrain_worker_spec_to_volume(spec, _volume())


def test_constraining_accepts_the_volume_data_center_when_named_explicitly() -> None:
    spec = _worker_spec(data_center_ids=("EU-RO-1",))
    assert constrain_worker_spec_to_volume(spec, _volume()) is spec


def test_network_volumes_are_refused_on_community_cloud() -> None:
    spec = _worker_spec(cloud_type=CloudType.COMMUNITY)
    with pytest.raises(ProviderValidationError, match="Secure Cloud Pods"):
        constrain_worker_spec_to_volume(spec, _volume())


def test_a_different_volume_id_is_refused() -> None:
    spec = _worker_spec(network_volume_id="vol-other")
    with pytest.raises(ProviderValidationError, match="refusing to attach a different volume"):
        constrain_worker_spec_to_volume(spec, _volume())


def test_a_spec_without_a_volume_cannot_be_constrained() -> None:
    spec = _worker_spec(network_volume_id=None, volume_mount_path="/workspace")
    with pytest.raises(ProviderValidationError, match="does not name it as its network volume"):
        constrain_worker_spec_to_volume(spec, _volume())


def test_the_estimate_is_withheld_above_the_published_tier(tmp_path: Path) -> None:
    """A size the published rate does not cover has no estimate rather than a wrong one."""

    lifecycle = _lifecycle(FakeVolumeProvider(), tmp_path)

    small = lifecycle.plan_create(_spec(size_gb=200))
    assert small.published_list_price_max_size_gb == 1024
    assert small.estimated_monthly_cost_usd == Decimal("14.00")

    boundary = lifecycle.plan_create(_spec(size_gb=1024))
    assert boundary.estimated_monthly_cost_usd == Decimal("71.68")

    oversize = lifecycle.plan_create(_spec(size_gb=2048))
    assert oversize.published_list_price_usd_per_gb_month == Decimal("0.07")
    assert oversize.estimated_monthly_cost_usd is None


# --- created Pod placement verification ------------------------------------------------


class PodProvider:
    """Provider double whose create answer can place the Pod anywhere asked."""

    def __init__(
        self,
        created: Worker,
        *,
        refreshed: Worker | None = None,
        refresh_error: Exception | None = None,
        get_results: Sequence[object] | None = None,
    ) -> None:
        self.created = created
        self.refreshed = refreshed
        self.refresh_error = refresh_error
        self.get_results = list(get_results or [])
        self.get_calls: list[str] = []
        self.destroy_calls: list[str] = []
        self.volume_destroy_calls: list[str] = []

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str] = (),
        require_public_ip: bool = False,
    ) -> GpuOffer:
        del gpu_type, cloud_type, gpu_count, data_center_ids, require_public_ip
        return GpuOffer(
            gpu_type_id="NVIDIA L4",
            display_name="L4",
            cloud_type=CloudType.SECURE,
            gpu_count=1,
            availability=Availability.HIGH,
            price_per_gpu_hour=Decimal("0.49"),
            total_price_per_hour=Decimal("0.49"),
            public_ip_capable=True,
        )

    def create_worker(self, spec: WorkerSpec) -> Worker:
        del spec
        return self.created

    def get_worker(self, worker_id: str) -> Worker:
        self.get_calls.append(worker_id)
        if self.get_results:
            result = self.get_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result  # type: ignore[return-value]
        if self.refresh_error is not None:
            raise self.refresh_error
        if self.refreshed is None:
            raise ProviderNotFoundError(f"RunPod worker {worker_id} was not found (HTTP 404)")
        return self.refreshed

    def start_worker(self, worker_id: str) -> Worker:
        raise AssertionError("start must not be reached")

    def stop_worker(self, worker_id: str) -> Worker:
        raise AssertionError("stop must not be reached")

    def destroy_worker(self, worker_id: str) -> None:
        self.destroy_calls.append(worker_id)

    def destroy_network_volume(self, volume_id: str) -> None:
        self.volume_destroy_calls.append(volume_id)


def _placed_worker(**overrides: object) -> Worker:
    values: dict[str, object] = {
        "id": POD_ID,
        "state": WorkerState.RUNNING,
        "cloud_type": CloudType.SECURE,
        "datacenter": "EU-RO-1",
        "network_volume_id": VOLUME_ID,
        "volume_mount_path": "/workspace/cache",
        "mounts_reported": True,
    }
    values.update(overrides)
    return Worker.model_validate(values)


def _worker_lifecycle(provider: object, tmp_path: Path) -> WorkerLifecycle:
    return WorkerLifecycle(
        provider,  # type: ignore[arg-type]
        WorkerStateStore(tmp_path / "workers.json"),
        default_timeout_seconds=10.0,
        poll_interval_seconds=1.0,
        max_poll_interval_seconds=4.0,
        sleep=lambda delay: None,
        monotonic=lambda: 0.0,
    )


def test_a_pod_placed_in_the_wrong_data_center_is_reported(tmp_path: Path) -> None:
    provider = PodProvider(_placed_worker(datacenter="US-KS-2"))
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_worker_spec(data_center_ids=("EU-RO-1",)), max_hourly_price=None)

    with pytest.raises(WorkerPlacementError, match="cannot use that volume"):
        lifecycle.create(plan)


def test_a_pod_placed_correctly_is_returned(tmp_path: Path) -> None:
    provider = PodProvider(_placed_worker())
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_worker_spec(data_center_ids=("EU-RO-1",)), max_hourly_price=None)

    worker = lifecycle.create(plan)

    assert worker.datacenter == "EU-RO-1"
    assert provider.get_calls == [], "a fully reported create answer needs no refresh"


def test_a_pod_reporting_a_different_mount_is_reported(tmp_path: Path) -> None:
    provider = PodProvider(_placed_worker(network_volume_id="vol-other"))
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_worker_spec(data_center_ids=("EU-RO-1",)), max_hourly_price=None)

    with pytest.raises(WorkerPlacementError, match="does not have the intended working storage"):
        lifecycle.create(plan)


def test_a_pod_that_reported_no_mount_is_reported(tmp_path: Path) -> None:
    """`mounts` is required in the v2 Pod response, so its absence is real evidence."""

    provider = PodProvider(
        _placed_worker(datacenter="EU-RO-1", network_volume_id=None, mounts_reported=False),
        refreshed=_placed_worker(network_volume_id=None, mounts_reported=True),
    )
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_worker_spec(data_center_ids=("EU-RO-1",)), max_hourly_price=None)

    with pytest.raises(WorkerPlacementError, match="no network volume"):
        lifecycle.create(plan)


def test_an_answer_that_never_reported_mounts_is_not_a_contradiction(tmp_path: Path) -> None:
    """Silence is not evidence, so a response with no mounts document is not a failure."""

    provider = PodProvider(
        _placed_worker(datacenter="EU-RO-1", network_volume_id=None, mounts_reported=False),
        refreshed=_placed_worker(
            datacenter="EU-RO-1",
            network_volume_id=None,
            mounts_reported=False,
        ),
    )
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_worker_spec(data_center_ids=("EU-RO-1",)), max_hourly_price=None)

    assert lifecycle.create(plan).id == POD_ID


def test_a_sparse_create_answer_is_refreshed_once_before_concluding(tmp_path: Path) -> None:
    provider = PodProvider(
        _placed_worker(datacenter=None, network_volume_id=None, mounts_reported=False),
        refreshed=_placed_worker(),
    )
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_worker_spec(data_center_ids=("EU-RO-1",)), max_hourly_price=None)

    worker = lifecycle.create(plan)

    assert worker.network_volume_id == VOLUME_ID
    assert provider.get_calls == [POD_ID]


def test_placement_is_not_checked_for_a_pod_without_a_volume(tmp_path: Path) -> None:
    spec = _worker_spec(network_volume_id=None, volume_mount_path="/workspace")
    provider = PodProvider(_placed_worker(datacenter="US-KS-2", network_volume_id=None))
    lifecycle = _worker_lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(spec, max_hourly_price=None)

    assert lifecycle.create(plan).id == POD_ID
    assert provider.get_calls == []


def test_destroying_a_pod_never_destroys_a_volume(tmp_path: Path) -> None:
    provider = PodProvider(
        _placed_worker(),
        get_results=[_placed_worker(), ProviderNotFoundError("gone")],
    )
    lifecycle = _worker_lifecycle(provider, tmp_path)

    result = lifecycle.destroy(POD_ID)

    assert result.already_absent is False
    assert provider.destroy_calls == [POD_ID]
    assert provider.volume_destroy_calls == []


# --- unresolved paid creates ----------------------------------------------------------


def test_a_new_create_is_refused_while_an_earlier_one_is_unresolved(tmp_path: Path) -> None:
    """The one way this command could duplicate a paid volume is a retry after silence."""

    provider = FakeVolumeProvider(create_result=AmbiguousCreateError("may have created a volume"))
    lifecycle = _lifecycle(provider, tmp_path)
    with pytest.raises(AmbiguousCreateError):
        lifecycle.create(_plan())

    with pytest.raises(UnresolvedCreateError, match="while an earlier create is unresolved"):
        lifecycle.plan_create(_spec(name="wavcse-vol-cache-second"))

    assert provider.create_calls == 1, "the refusal must happen before any new paid request"


def test_reconciling_an_intent_against_the_provider_clears_the_refusal(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(create_result=AmbiguousCreateError("lost the response"))
    lifecycle = _lifecycle(provider, tmp_path)
    with pytest.raises(AmbiguousCreateError):
        lifecycle.create(_plan())

    provider.volumes = [_volume()]
    lifecycle.refresh()

    assert lifecycle.plan_create(_spec(name="wavcse-vol-cache-second")).spec.size_gb == 200


def test_forgetting_an_intent_clears_the_refusal_without_touching_the_provider(
    tmp_path: Path,
) -> None:
    provider = FakeVolumeProvider(create_result=AmbiguousCreateError("lost the response"))
    lifecycle = _lifecycle(provider, tmp_path)
    with pytest.raises(AmbiguousCreateError):
        lifecycle.create(_plan())

    forgotten = VolumeStateStore(tmp_path / "volumes.json").forget(VOLUME_NAME)

    assert forgotten is not None
    assert forgotten.provider_volume_id is None
    assert provider.volume_destroy_calls == []
    assert provider.create_calls == 1
    lifecycle.plan_create(_spec(name="wavcse-vol-cache-second"))


def test_forgetting_an_unknown_identity_reports_nothing_to_forget(tmp_path: Path) -> None:
    store = VolumeStateStore(tmp_path / "volumes.json")

    assert store.forget("wavcse-vol-cache-absent") is None
    assert store.list_records() == []


def test_a_resolved_volume_does_not_block_a_later_create(tmp_path: Path) -> None:
    provider = FakeVolumeProvider(create_result=_volume())
    lifecycle = _lifecycle(provider, tmp_path)
    lifecycle.create(_plan())

    assert lifecycle.plan_create(_spec(name="wavcse-vol-cache-second")).spec.size_gb == 200


def test_a_definite_provider_refusal_leaves_no_unresolved_intent(tmp_path: Path) -> None:
    """A refusal is an answer: the intent must not outlive a request that created nothing."""

    provider = FakeVolumeProvider(
        create_result=ProviderValidationError(
            "RunPod rejected the request to create network volume (HTTP 422)"
        )
    )
    lifecycle = _lifecycle(provider, tmp_path)

    with pytest.raises(ProviderValidationError):
        lifecycle.create(_plan())

    assert VolumeStateStore(tmp_path / "volumes.json").list_records() == []
    assert lifecycle.plan_create(_spec(name="wavcse-vol-cache-second")).spec.size_gb == 200


def test_an_ambiguous_create_keeps_its_intent_while_a_refusal_does_not(tmp_path: Path) -> None:
    ambiguous = FakeVolumeProvider(create_result=AmbiguousCreateError("lost the response"))
    lifecycle = _lifecycle(ambiguous, tmp_path)
    with pytest.raises(AmbiguousCreateError):
        lifecycle.create(_plan())
    assert len(VolumeStateStore(tmp_path / "volumes.json").unresolved_intents()) == 1

    refused = FakeVolumeProvider(create_result=ProviderUnavailableError("no response"))
    other = _lifecycle(refused, tmp_path / "other")
    with pytest.raises(ProviderUnavailableError):
        other.create(_plan())
    assert len(VolumeStateStore(tmp_path / "other" / "volumes.json").unresolved_intents()) == 1


def test_destroying_an_unadopted_volume_clears_its_intent(tmp_path: Path) -> None:
    """The recovery flow the tool recommends must not strand local state."""

    store = VolumeStateStore(tmp_path / "volumes.json")
    store.record_create_intent(_spec())
    provider = FakeVolumeProvider(get_results=[_volume(), ProviderNotFoundError("gone")])

    result = _lifecycle(provider, tmp_path).destroy(VOLUME_ID)

    assert result.already_absent is False
    record = store.get_by_identity(VOLUME_NAME)
    assert record is not None
    assert record.provider_volume_id == VOLUME_ID
    assert record.lifecycle_state.value == "DESTROYED"
    assert store.unresolved_intents() == []
