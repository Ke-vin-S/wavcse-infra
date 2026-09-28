from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from wavcse_infra.errors import (
    CostGuardError,
    LifecycleTimeoutError,
    ProviderNotFoundError,
    ProviderUnavailableError,
    ResourceUnavailableError,
)
from wavcse_infra.models import (
    Availability,
    CloudType,
    GpuOffer,
    Worker,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.lifecycle import WorkerLifecycle


def _worker(state: WorkerState, *, cost: str = "0.16") -> Worker:
    native = {
        WorkerState.PROVISIONING: "PROVISIONING",
        WorkerState.STARTING: "STARTING",
        WorkerState.RUNNING: "RUNNING",
        WorkerState.STOPPED: "EXITED",
        WorkerState.ERROR: "ERROR",
    }.get(state, state.value)
    return Worker(
        id="pod-123",
        name="wavcse-training-abc123",
        state=state,
        native_status=native,
        gpu_type="NVIDIA RTX A5000",
        gpu_count=1,
        cloud_type=CloudType.COMMUNITY,
        hourly_cost=Decimal(cost),
    )


def _spec(**overrides) -> WorkerSpec:
    values = {
        "name": "wavcse-training-abc123",
        "gpu_type": "NVIDIA RTX A5000",
        "gpu_count": 1,
        "cloud_type": CloudType.COMMUNITY,
        "image": "runpod/pytorch:example",
    }
    values.update(overrides)
    return WorkerSpec.model_validate(values)


def _offer(**overrides) -> GpuOffer:
    values = {
        "gpu_type_id": "NVIDIA RTX A5000",
        "display_name": "RTX A5000",
        "cloud_type": CloudType.COMMUNITY,
        "gpu_count": 1,
        "maximum_gpu_count": 2,
        "availability": Availability.HIGH,
        "price_per_gpu_hour": Decimal("0.16"),
        "total_price_per_hour": Decimal("0.16"),
    }
    values.update(overrides)
    return GpuOffer.model_validate(values)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        self.value += delay


class FakeProvider:
    def __init__(
        self,
        *,
        offer: GpuOffer | None = None,
        get_results: Sequence[Worker | Exception] = (),
        create_result: Worker | None = None,
    ) -> None:
        self.offer = offer or _offer()
        self.get_results = list(get_results)
        self.create_result = create_result or _worker(WorkerState.PROVISIONING)
        self.create_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.destroy_calls = 0

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str] = (),
    ) -> GpuOffer:
        assert gpu_type == "NVIDIA RTX A5000"
        assert cloud_type is CloudType.COMMUNITY
        assert gpu_count == 1
        assert data_center_ids == ()
        return self.offer

    def create_worker(self, spec: WorkerSpec) -> Worker:
        self.create_calls += 1
        assert spec.name == "wavcse-training-abc123"
        return self.create_result

    def get_worker(self, worker_id: str) -> Worker:
        assert worker_id == "pod-123"
        if not self.get_results:
            return self.create_result
        result = self.get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def start_worker(self, worker_id: str) -> Worker:
        assert worker_id == "pod-123"
        self.start_calls += 1
        return _worker(WorkerState.STARTING)

    def stop_worker(self, worker_id: str) -> Worker:
        assert worker_id == "pod-123"
        self.stop_calls += 1
        return _worker(WorkerState.STOPPING)

    def destroy_worker(self, worker_id: str) -> None:
        assert worker_id == "pod-123"
        self.destroy_calls += 1


def _lifecycle(
    provider: FakeProvider,
    tmp_path: Path,
    clock: FakeClock | None = None,
) -> WorkerLifecycle:
    active_clock = clock or FakeClock()
    return WorkerLifecycle(
        provider,
        WorkerStateStore(tmp_path / "workers.json"),
        default_timeout_seconds=30,
        poll_interval_seconds=1,
        max_poll_interval_seconds=4,
        sleep=active_clock.sleep,
        monotonic=active_clock.monotonic,
    )


def test_plan_accepts_price_equal_to_guard(tmp_path: Path) -> None:
    lifecycle = _lifecycle(FakeProvider(), tmp_path)

    plan = lifecycle.plan_create(_spec(), max_hourly_price=Decimal("0.16"))

    assert plan.offer.total_price_per_hour == Decimal("0.16")
    assert plan.max_hourly_price == Decimal("0.16")


def test_plan_rejects_price_above_guard_before_create(tmp_path: Path) -> None:
    provider = FakeProvider()
    lifecycle = _lifecycle(provider, tmp_path)

    with pytest.raises(CostGuardError, match="exceeds --max-price"):
        lifecycle.plan_create(_spec(), max_hourly_price=Decimal("0.15"))

    assert provider.create_calls == 0


def test_plan_refuses_guard_when_provider_price_is_unknown(tmp_path: Path) -> None:
    provider = FakeProvider(offer=_offer(price_per_gpu_hour=None, total_price_per_hour=None))

    with pytest.raises(CostGuardError, match="cannot enforce --max-price"):
        _lifecycle(provider, tmp_path).plan_create(
            _spec(),
            max_hourly_price=Decimal("0.20"),
        )


def test_plan_rejects_unavailable_explicit_gpu(tmp_path: Path) -> None:
    provider = FakeProvider(offer=_offer(availability=Availability.NONE))

    with pytest.raises(ResourceUnavailableError, match="no confirmed"):
        _lifecycle(provider, tmp_path).plan_create(_spec(), max_hourly_price=None)


def test_create_persists_provider_id_and_polls_to_running(tmp_path: Path) -> None:
    provider = FakeProvider(get_results=[_worker(WorkerState.RUNNING)])
    lifecycle = _lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_spec(), max_hourly_price=Decimal("0.20"))

    worker = lifecycle.create(plan)

    assert worker.state is WorkerState.RUNNING
    assert provider.create_calls == 1
    record = WorkerStateStore(tmp_path / "workers.json").get("pod-123")
    assert record is not None
    assert record.last_observed_state is WorkerState.RUNNING


def test_polling_tolerates_transient_get_failure(tmp_path: Path) -> None:
    provider = FakeProvider(
        get_results=[
            ProviderUnavailableError("temporary failure"),
            _worker(WorkerState.RUNNING),
        ]
    )
    lifecycle = _lifecycle(provider, tmp_path)
    plan = lifecycle.plan_create(_spec(), max_hourly_price=None)

    worker = lifecycle.create(plan)

    assert worker.state is WorkerState.RUNNING


def test_polling_timeout_reports_last_known_state(tmp_path: Path) -> None:
    clock = FakeClock()
    provider = FakeProvider(create_result=_worker(WorkerState.PROVISIONING))
    lifecycle = _lifecycle(provider, tmp_path, clock)
    plan = lifecycle.plan_create(_spec(), max_hourly_price=None)

    with pytest.raises(
        LifecycleTimeoutError,
        match="last known provider state: PROVISIONING",
    ):
        lifecycle.create(plan, timeout_seconds=3)

    assert clock.delays == [1, 2]


def test_start_and_stop_use_current_state_and_poll_successfully(tmp_path: Path) -> None:
    provider = FakeProvider(
        get_results=[
            _worker(WorkerState.STOPPED, cost="0"),
            _worker(WorkerState.RUNNING),
            _worker(WorkerState.RUNNING),
            _worker(WorkerState.STOPPED, cost="0"),
        ]
    )
    lifecycle = _lifecycle(provider, tmp_path)

    started = lifecycle.start("pod-123")
    stopped = lifecycle.stop("pod-123")

    assert started.state is WorkerState.RUNNING
    assert stopped.state is WorkerState.STOPPED
    assert provider.start_calls == 1
    assert provider.stop_calls == 1


def test_destroy_polls_until_exact_id_is_absent(tmp_path: Path) -> None:
    provider = FakeProvider(
        get_results=[
            _worker(WorkerState.RUNNING),
            ProviderNotFoundError("absent"),
        ]
    )
    lifecycle = _lifecycle(provider, tmp_path)

    result = lifecycle.destroy("pod-123")

    assert not result.already_absent
    assert provider.destroy_calls == 1


def test_destroy_already_absent_is_idempotent_and_does_not_delete_other_resource(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(get_results=[ProviderNotFoundError("absent")])
    lifecycle = _lifecycle(provider, tmp_path)

    result = lifecycle.destroy("pod-123")

    assert result.already_absent
    assert provider.destroy_calls == 0
