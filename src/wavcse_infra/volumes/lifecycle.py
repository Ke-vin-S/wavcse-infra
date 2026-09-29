"""Provider-neutral network volume lifecycle decisions and Pod placement constraints.

A network volume is rebuildable working storage, so this module's whole job is to keep three
things true:

* a volume is only ever created with an explicit, catalog-verified data center and size;
* a Pod that mounts one is constrained - before any paid request - to that exact data
  center, and the provider's answer is verified afterwards;
* a volume and a Pod have independent lifetimes, so neither destroy implies the other.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from wavcse_infra.errors import (
    LifecycleTimeoutError,
    ProviderError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderUnavailableError,
    ProviderValidationError,
    StateError,
    UnresolvedCreateError,
)
from wavcse_infra.models import (
    CloudType,
    DataCenterInfo,
    NetworkVolume,
    NetworkVolumeBilling,
    NetworkVolumeCreationPlan,
    NetworkVolumeSpec,
    VolumeType,
    WorkerSpec,
)
from wavcse_infra.providers.runpod import (
    PUBLISHED_NETWORK_VOLUME_STORAGE_MAX_TIER_GB,
    PUBLISHED_NETWORK_VOLUME_STORAGE_USD_PER_GB_MONTH,
)
from wavcse_infra.state import VolumeRecord, VolumeStateStore


class VolumeProvider(Protocol):
    """Narrow normalized provider surface used by volume lifecycle decisions."""

    def list_network_volumes(self) -> list[NetworkVolume]: ...

    def get_network_volume(self, volume_id: str) -> NetworkVolume: ...

    def create_network_volume(self, spec: NetworkVolumeSpec) -> NetworkVolume: ...

    def destroy_network_volume(self, volume_id: str) -> None: ...

    def list_data_centers(
        self,
        *,
        include_gpu_availability: bool = False,
    ) -> list[DataCenterInfo]: ...

    def list_network_volume_billing(
        self,
        *,
        volume_id: str | None = None,
        last_n: int | None = None,
    ) -> NetworkVolumeBilling: ...


@dataclass(frozen=True)
class VolumeDestroyResult:
    """Outcome of an exact-ID network volume destroy request."""

    volume_id: str
    already_absent: bool


@dataclass(frozen=True)
class VolumeCreated:
    """A created volume together with the local record that now tracks it."""

    record: VolumeRecord
    volume: NetworkVolume


def constrain_worker_spec_to_volume(spec: WorkerSpec, volume: NetworkVolume) -> WorkerSpec:
    """Return `spec` constrained to the data center of the volume it mounts.

    A network volume exists in exactly one data center, and RunPod can only place a Pod that
    mounts it in that same data center. This runs before the offer lookup and before any paid
    request, so an incompatible or contradictory request fails while it is still free to do
    so. An operator-supplied `--data-center` that names anything else is rejected rather than
    quietly overridden, because silently ignoring a placement constraint is exactly how a Pod
    ends up created in the wrong place.

    Network volumes attach to Secure Cloud Pods only; Community Cloud is rejected here rather
    than at the provider.
    """

    if spec.network_volume_id is None:
        raise ProviderValidationError(
            f"Network volume {volume.id} cannot be attached because this request does not "
            "name it as its network volume"
        )
    if spec.network_volume_id != volume.id:
        raise ProviderValidationError(
            f"Network volume {volume.id} was resolved, but the Pod request names "
            f"{spec.network_volume_id}; refusing to attach a different volume"
        )
    if spec.cloud_type is not CloudType.SECURE:
        raise ProviderValidationError(
            f"RunPod attaches network volumes only to Secure Cloud Pods, but "
            f"{spec.cloud_type.value} was requested; use --cloud secure for a Pod that mounts "
            f"network volume {volume.id}"
        )
    if spec.data_center_ids and set(spec.data_center_ids) != {volume.datacenter}:
        raise ProviderValidationError(
            f"Network volume {volume.id} is in data center {volume.datacenter}, but this "
            f"request allows {', '.join(spec.data_center_ids)}; a Pod that mounts the volume "
            f"can only be placed in {volume.datacenter}"
        )
    if spec.data_center_ids == (volume.datacenter,):
        return spec
    return spec.model_copy(update={"data_center_ids": (volume.datacenter,)})


class VolumeLifecycle:
    """Plan, create, observe, and destroy network volumes with explicit guards."""

    def __init__(
        self,
        provider: VolumeProvider,
        state_store: VolumeStateStore,
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

    def plan_create(self, spec: NetworkVolumeSpec) -> NetworkVolumeCreationPlan:
        """Resolve the catalog facts that must be shown before a billable create.

        This is also the placement guard for the volume itself: a data center that the
        provider's own catalog does not report as network-volume capable is rejected here,
        and so is a storage tier that data center does not offer.

        It additionally refuses to plan a second paid create while an earlier one is still
        unresolved. The provider names are not unique and the create has no idempotency key,
        so a retry after a lost response is the one way this command could create a duplicate
        billable volume. Reconciling the intent is the way forward; creating again is not.
        """

        unresolved = self._state.unresolved_intents()
        if unresolved:
            summary = "; ".join(
                f"{record.infra_identity!r} ({record.requested_size_gb} GB in "
                f"{record.requested_data_center}, requested "
                f"{record.creation_timestamp.isoformat()})"
                for record in unresolved
            )
            raise UnresolvedCreateError(
                "Refusing to plan another network volume while an earlier create is "
                f"unresolved: {summary}. Run `infra volume list` to reconcile it against the "
                "provider, or `infra volume forget <infra-identity>` if the provider really "
                "has no such volume, before creating another"
            )
        data_centers = self._provider.list_data_centers()
        data_center = next(
            (entry for entry in data_centers if entry.id == spec.datacenter),
            None,
        )
        if data_center is None:
            raise ProviderValidationError(
                f"RunPod does not report a data center with ID {spec.datacenter!r}; run "
                "`infra volume datacenters` to list the exact IDs that can host a volume"
            )
        if not data_center.supports_network_volume(spec.volume_type):
            supported = ", ".join(
                volume_type.value for volume_type in data_center.network_volume_types
            )
            detail = (
                f"it supports {supported}"
                if supported
                else "it does not support network volumes at all"
            )
            requested = spec.volume_type.value if spec.volume_type is not None else "the default"
            raise ProviderValidationError(
                f"Data center {data_center.id} cannot host network volume tier {requested}: "
                f"{detail}"
            )
        effective_tier = _effective_tier(data_center, spec.volume_type)
        published_rate = (
            PUBLISHED_NETWORK_VOLUME_STORAGE_USD_PER_GB_MONTH.get(effective_tier)
            if effective_tier is not None
            else None
        )
        return NetworkVolumeCreationPlan(
            spec=spec,
            data_center=data_center,
            published_list_price_usd_per_gb_month=published_rate,
            published_list_price_max_size_gb=(
                PUBLISHED_NETWORK_VOLUME_STORAGE_MAX_TIER_GB if published_rate is not None else None
            ),
        )

    def create(self, plan: NetworkVolumeCreationPlan) -> VolumeCreated:
        """Record the intent, create once, then attach the provider's answer to it.

        The intent is durable before the paid request is issued. That is what makes an
        ambiguous create recoverable: the exact infra identity and the requested placement
        survive a lost response, so a later read can match the provider's own listing.
        """

        # Refuse a billable create before the POST if its intent could not be persisted, and
        # refuse a duplicate identity outright.
        intent = self._state.record_create_intent(plan.spec)
        try:
            volume = self._provider.create_network_volume(plan.spec)
        except (ProviderOperationAmbiguousError, ProviderUnavailableError):
            # The outcome is unknown, so the intent is the only evidence that a billable volume
            # may exist. It must survive for a later reconciliation.
            raise
        except ProviderError:
            # The provider answered and refused, so nothing was created. Keeping the intent
            # would block every later create and warn about a resource that provably does not
            # exist, which is the opposite of what an unresolved intent means.
            with suppress(StateError):
                self._state.forget(intent.infra_identity)
            raise
        try:
            record = self._state.record_created(intent, volume)
        except StateError as exc:
            raise StateError(
                f"RunPod network volume {volume.id} was created, but local state could not be "
                f"persisted: {exc}"
            ) from exc
        return VolumeCreated(record=record, volume=volume)

    def refresh(self) -> list[NetworkVolume]:
        """Return the provider's authoritative volume list and reconcile local records."""

        volumes = self._provider.list_network_volumes()
        with suppress(StateError):
            self._state.reconcile(volumes)
        return volumes

    def show(self, volume_id: str) -> NetworkVolume:
        """Return one exact volume and refresh its tracked record when one exists."""

        volume = self._provider.get_network_volume(volume_id)
        with suppress(StateError):
            self._state.observe(volume)
        return volume

    def billing(
        self,
        *,
        volume_id: str | None = None,
        last_n: int | None = None,
    ) -> NetworkVolumeBilling:
        """Return provider-reported storage charges for one or all tracked volumes."""

        return self._provider.list_network_volume_billing(volume_id=volume_id, last_n=last_n)

    def data_centers(self) -> list[DataCenterInfo]:
        """Return the data centers that can host a network volume."""

        return [
            data_center
            for data_center in self._provider.list_data_centers()
            if data_center.network_volume_types
        ]

    def destroy(
        self,
        volume_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> VolumeDestroyResult:
        """Permanently delete one exact-ID network volume and wait for its absence.

        This never touches a Pod, and it never touches canonical storage: it addresses one
        provider volume by immutable ID and nothing else.
        """

        try:
            current = self._provider.get_network_volume(volume_id)
        except ProviderNotFoundError:
            self._mark_destroyed(volume_id)
            return VolumeDestroyResult(volume_id=volume_id, already_absent=True)
        # Link an unadopted record - for example a pending intent whose create response was
        # lost - to the volume it names before that volume is deleted, so the record this
        # destroy is about to update is the one that actually describes it.
        with suppress(StateError):
            self._state.observe(current)
        try:
            self._provider.destroy_network_volume(volume_id)
        except ProviderNotFoundError:
            self._mark_destroyed(volume_id)
            return VolumeDestroyResult(volume_id=volume_id, already_absent=True)
        except ProviderOperationAmbiguousError:
            pass

        self._wait_until_absent(volume_id, timeout_seconds=timeout_seconds)
        self._mark_destroyed(volume_id)
        return VolumeDestroyResult(volume_id=volume_id, already_absent=False)

    def tracked_record(self, volume_id: str) -> VolumeRecord | None:
        """Return the local record for one exact provider volume ID, if any."""

        with suppress(StateError):
            return self._state.get(volume_id)
        return None

    def _wait_until_absent(
        self,
        volume_id: str,
        *,
        timeout_seconds: float | None,
    ) -> None:
        timeout = self._default_timeout if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("lifecycle timeout must be greater than zero")
        deadline = self._monotonic() + timeout
        delay = self._poll_interval
        while True:
            try:
                self._provider.get_network_volume(volume_id)
            except ProviderNotFoundError:
                return
            except ProviderUnavailableError:
                pass
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LifecycleTimeoutError(
                    f"RunPod network volume {volume_id} still exists after {timeout:g} seconds "
                    "while waiting for destroy"
                )
            self._sleep(min(delay, remaining))
            delay = min(delay * 2, self._max_poll_interval)

    def _mark_destroyed(self, volume_id: str) -> None:
        # A local-state problem must not redirect or repeat a provider deletion.
        with suppress(StateError):
            self._state.mark_destroyed(volume_id)


def _effective_tier(
    data_center: DataCenterInfo,
    requested: VolumeType | None,
) -> VolumeType | None:
    """Return the tier a create would actually use, when the provider's choice is knowable.

    An explicit tier is that tier. Without one the provider uses the data center's default
    (primary) tier, which is only knowable when that data center offers exactly one tier.
    """

    if requested is not None:
        return requested
    if len(data_center.network_volume_types) == 1:
        return data_center.network_volume_types[0]
    return None


def volume_placement_failure_message(
    spec: WorkerSpec,
    volume: NetworkVolume,
    *,
    reason: str,
) -> str:
    """Name the volume constraint when a volume-constrained Pod request cannot be placed.

    A capacity failure for a Pod that must mount a volume is not a generic capacity failure:
    the requested GPU may be available elsewhere, and only the volume's own data center can
    host this Pod. Saying so is what makes the failure actionable.
    """

    return (
        f"{reason}. This Pod must mount network volume {volume.id}, which exists only in data "
        f"center {volume.datacenter}. No Pod was created. Choose a GPU with confirmed capacity "
        f"in {volume.datacenter}, or use a network volume in a data center that has the "
        "capacity you need"
    )
