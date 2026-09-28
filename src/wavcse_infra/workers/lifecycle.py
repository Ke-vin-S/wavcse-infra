"""Provider-neutral worker lifecycle decisions and bounded polling."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from wavcse_infra.errors import (
    CostGuardError,
    LifecycleError,
    LifecycleTimeoutError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderUnavailableError,
    ProviderValidationError,
    ResourceUnavailableError,
    StateError,
)
from wavcse_infra.models import (
    Availability,
    CloudType,
    GpuOffer,
    Worker,
    WorkerCreationPlan,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.state import WorkerStateStore


class WorkerProvider(Protocol):
    """Narrow normalized provider surface used by lifecycle decisions."""

    def get_worker(self, worker_id: str) -> Worker: ...

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str] = (),
    ) -> GpuOffer: ...

    def create_worker(self, spec: WorkerSpec) -> Worker: ...

    def start_worker(self, worker_id: str) -> Worker: ...

    def stop_worker(self, worker_id: str) -> Worker: ...

    def destroy_worker(self, worker_id: str) -> None: ...


@dataclass(frozen=True)
class DestroyResult:
    """Outcome of an exact-ID destroy request."""

    worker_id: str
    already_absent: bool


class WorkerLifecycle:
    """Plan mutations, enforce guards, persist state, and poll boundedly."""

    def __init__(
        self,
        provider: WorkerProvider,
        state_store: WorkerStateStore,
        *,
        default_timeout_seconds: float,
        poll_interval_seconds: float,
        max_poll_interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._state = state_store
        self._default_timeout = default_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._max_poll_interval = max_poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def plan_create(
        self,
        spec: WorkerSpec,
        *,
        max_hourly_price: Decimal | None,
    ) -> WorkerCreationPlan:
        """Resolve one exact current offer and enforce availability/cost constraints."""

        # Refuse a paid create before the POST if its provider ID could not be persisted.
        self._state.list_records()
        if spec.interruptible:
            raise ProviderValidationError(
                "RunPod REST API v2 does not expose interruptible Pod creation; "
                "omit --interruptible to create an on-demand Pod"
            )
        offer = self._provider.get_gpu_offer(
            spec.gpu_type,
            spec.cloud_type,
            spec.gpu_count,
            data_center_ids=spec.data_center_ids,
        )
        if offer.maximum_gpu_count is not None and offer.maximum_gpu_count < spec.gpu_count:
            raise ResourceUnavailableError(
                f"RunPod {spec.cloud_type.value} cloud supports at most "
                f"{offer.maximum_gpu_count} x {spec.gpu_type} on one Pod; "
                f"{spec.gpu_count} requested"
            )
        if offer.availability in {Availability.NONE, Availability.UNKNOWN}:
            location = f" in {', '.join(spec.data_center_ids)}" if spec.data_center_ids else ""
            raise ResourceUnavailableError(
                f"RunPod reports no confirmed {spec.cloud_type.value} availability for "
                f"{spec.gpu_count} x {spec.gpu_type}{location}"
            )
        if max_hourly_price is not None:
            known_price = offer.total_price_per_hour
            if known_price is None:
                raise CostGuardError(
                    f"RunPod did not provide a reliable price for {spec.gpu_type}; "
                    f"cannot enforce --max-price {max_hourly_price}"
                )
            if known_price > max_hourly_price:
                raise CostGuardError(
                    f"Refusing to create Pod: provider price ${known_price}/hour exceeds "
                    f"--max-price ${max_hourly_price}/hour"
                )
        return WorkerCreationPlan(
            spec=spec,
            offer=offer,
            max_hourly_price=max_hourly_price,
        )

    def create(
        self,
        plan: WorkerCreationPlan,
        *,
        timeout_seconds: float | None = None,
    ) -> Worker:
        """Create once, persist the provider ID, then wait for RUNNING."""

        worker = self._provider.create_worker(plan.spec)
        try:
            self._state.record_created(plan, worker)
        except StateError as exc:
            raise StateError(
                f"RunPod Pod {worker.id} was created, but local state could not be persisted: {exc}"
            ) from exc
        if worker.state is WorkerState.RUNNING:
            return worker
        return self._wait_for_state(
            worker.id,
            target_states={WorkerState.RUNNING},
            terminal_states={WorkerState.ERROR, WorkerState.DESTROYED},
            operation="become RUNNING after creation",
            timeout_seconds=timeout_seconds,
            initial_worker=worker,
        )

    def start(
        self,
        worker_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Worker:
        """Start an exact-ID worker and wait until RUNNING."""

        current = self._provider.get_worker(worker_id)
        self._observe_state(current)
        if current.state is WorkerState.RUNNING:
            return current
        try:
            initial = self._provider.start_worker(worker_id)
        except ProviderOperationAmbiguousError:
            initial = current
        return self._wait_for_state(
            worker_id,
            target_states={WorkerState.RUNNING},
            terminal_states={WorkerState.ERROR, WorkerState.DESTROYED},
            operation="become RUNNING after start",
            timeout_seconds=timeout_seconds,
            initial_worker=initial,
        )

    def stop(
        self,
        worker_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Worker:
        """Stop an exact-ID worker and wait until STOPPED."""

        current = self._provider.get_worker(worker_id)
        self._observe_state(current)
        if current.state is WorkerState.STOPPED:
            return current
        try:
            initial = self._provider.stop_worker(worker_id)
        except ProviderOperationAmbiguousError:
            initial = current
        return self._wait_for_state(
            worker_id,
            target_states={WorkerState.STOPPED},
            terminal_states={WorkerState.ERROR, WorkerState.DESTROYED},
            operation="become STOPPED after stop",
            timeout_seconds=timeout_seconds,
            initial_worker=initial,
        )

    def destroy(
        self,
        worker_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DestroyResult:
        """Terminate one exact-ID worker and wait for provider absence."""

        try:
            current = self._provider.get_worker(worker_id)
        except ProviderNotFoundError:
            self._mark_destroyed(worker_id)
            return DestroyResult(worker_id=worker_id, already_absent=True)
        self._observe_state(current)
        try:
            self._provider.destroy_worker(worker_id)
        except ProviderNotFoundError:
            self._mark_destroyed(worker_id)
            return DestroyResult(worker_id=worker_id, already_absent=True)
        except ProviderOperationAmbiguousError:
            pass

        self._wait_until_absent(worker_id, timeout_seconds=timeout_seconds)
        self._mark_destroyed(worker_id)
        return DestroyResult(worker_id=worker_id, already_absent=False)

    def _wait_for_state(
        self,
        worker_id: str,
        *,
        target_states: set[WorkerState],
        terminal_states: set[WorkerState],
        operation: str,
        timeout_seconds: float | None,
        initial_worker: Worker,
    ) -> Worker:
        timeout = self._timeout(timeout_seconds)
        deadline = self._monotonic() + timeout
        delay = self._poll_interval
        last_worker = initial_worker
        self._observe_state(last_worker)
        while True:
            if last_worker.state in target_states:
                return last_worker
            if last_worker.state in terminal_states:
                raise LifecycleError(
                    f"RunPod worker {worker_id} reached terminal state "
                    f"{last_worker.state.value} while waiting to {operation}"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LifecycleTimeoutError(
                    f"RunPod worker {worker_id} did not {operation} within {timeout:g} seconds; "
                    f"last known provider state: {last_worker.state.value} "
                    f"({last_worker.native_status or 'unknown'})"
                )
            self._sleep(min(delay, remaining))
            delay = min(delay * 2, self._max_poll_interval)
            try:
                last_worker = self._provider.get_worker(worker_id)
                self._observe_state(last_worker)
            except ProviderUnavailableError:
                continue
            except ProviderNotFoundError as exc:
                raise LifecycleTimeoutError(
                    f"RunPod worker {worker_id} disappeared while waiting to {operation}"
                ) from exc

    def _wait_until_absent(
        self,
        worker_id: str,
        *,
        timeout_seconds: float | None,
    ) -> None:
        timeout = self._timeout(timeout_seconds)
        deadline = self._monotonic() + timeout
        delay = self._poll_interval
        last_worker: Worker | None = None
        while True:
            try:
                worker = self._provider.get_worker(worker_id)
                last_worker = worker
                self._observe_state(worker)
                if worker.state is WorkerState.DESTROYED:
                    return
            except ProviderNotFoundError:
                return
            except ProviderUnavailableError:
                pass
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                state = last_worker.state.value if last_worker is not None else "unknown"
                raise LifecycleTimeoutError(
                    f"RunPod worker {worker_id} still exists after {timeout:g} seconds while "
                    f"waiting for destroy; last known provider state: {state}"
                )
            self._sleep(min(delay, remaining))
            delay = min(delay * 2, self._max_poll_interval)

    def _timeout(self, requested: float | None) -> float:
        timeout = self._default_timeout if requested is None else requested
        if timeout <= 0:
            raise ValueError("lifecycle timeout must be greater than zero")
        return timeout

    def _observe_state(self, worker: Worker) -> None:
        # Provider state is authoritative for existing workers.
        with suppress(StateError):
            self._state.observe(worker)

    def _mark_destroyed(self, worker_id: str) -> None:
        # A local-state problem must not redirect or repeat a provider deletion.
        with suppress(StateError):
            self._state.mark_destroyed(worker_id)
