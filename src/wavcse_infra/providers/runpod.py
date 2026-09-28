"""RunPod REST v2/GraphQL client and provider response normalization."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from wavcse_infra.config import RunPodConfig, Settings
from wavcse_infra.credentials import SsmClient, resolve_runpod_api_key
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderConflictError,
    ProviderError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderPermissionError,
    ProviderResponseError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from wavcse_infra.models import (
    Availability,
    CloudType,
    GpuDataCenterAvailability,
    GpuOffer,
    Worker,
    WorkerConnectionInfo,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.redaction import redact

_RETRYABLE_STATUS_CODES = frozenset({429})
_AMBIGUOUS_MUTATION_STATUS_CODES = frozenset({408, 429})
_MAX_RETRY_DELAY_SECONDS = 30.0
_LIST_PAGE_SIZE = 1000

_PUBLIC_IP_OFFERS_QUERY = """
query PublicIpOffers($gpu: GpuTypeFilter, $price: GpuLowestPriceInput!) {
  gpuTypes(input: $gpu) {
    id
    displayName
    memoryInGb
    lowestPrice(input: $price) {
      uninterruptablePrice
      stockStatus
      supportPublicIp
      maxGpuCount
      availableGpuCounts
    }
  }
}
"""

_CREATE_PUBLIC_IP_POD_MUTATION = """
mutation CreatePublicIpPod($input: PodFindAndDeployOnDemandInput!) {
  podFindAndDeployOnDemand(input: $input) {
    id
  }
}
"""


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _Pagination(_WireModel):
    next_cursor: str | None = Field(alias="nextCursor")
    has_next_page: bool = Field(alias="hasNextPage")


class _SshEndpoint(_WireModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1)


class _Ssh(_WireModel):
    proxy: _SshEndpoint | None = None
    direct: _SshEndpoint | None = None


class _PodGpu(_WireModel):
    id: str | None = None
    count: int | None = Field(default=None, ge=0)


class _PersistentMount(_WireModel):
    size: int = Field(ge=0)
    path: str


class _NetworkMount(_WireModel):
    volume_id: str = Field(alias="volumeId")
    path: str


class _Mounts(_WireModel):
    persistent: _PersistentMount | None = None
    network: list[_NetworkMount] = Field(default_factory=list)


class _Pod(_WireModel):
    id: str = Field(min_length=1)
    name: str | None = None
    status: str | None = None
    gpu: _PodGpu | None = None
    cloud: str | None = None
    cost: Decimal | None = Field(default=None, ge=0)
    data_center_id: str | None = Field(default=None, alias="dataCenterId")
    image: str | None = None
    template: str | None = None
    disk: int | None = Field(default=None, ge=0)
    ports: list[str] = Field(default_factory=list)
    mounts: _Mounts | None = None
    ssh: _Ssh | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")


class _PodList(_WireModel):
    pods: list[_Pod]
    pagination: _Pagination


class _GpuPrice(_WireModel):
    secure: Decimal | None = Field(default=None, ge=0)
    community: Decimal | None = Field(default=None, ge=0)


class _GpuMaximumCount(_WireModel):
    secure: int | None = Field(default=None, ge=0)
    community: int | None = Field(default=None, ge=0)


class _GpuDataCenter(_WireModel):
    id: str = Field(min_length=1)
    name: str | None = None
    availability: str | None = None


class _GpuType(_WireModel):
    id: str = Field(min_length=1)
    name: str | None = None
    memory: int | None = Field(default=None, ge=0)
    secure: bool = False
    community: bool = False
    price: _GpuPrice
    max_count: _GpuMaximumCount = Field(alias="maxCount")
    availability: str | None = None
    data_centers: list[_GpuDataCenter] = Field(default_factory=list, alias="dataCenters")


class _GpuList(_WireModel):
    gpus: list[_GpuType]


class _GraphqlError(_WireModel):
    message: str = Field(min_length=1)


class _GraphqlLowestPrice(_WireModel):
    uninterruptable_price: Decimal | None = Field(default=None, alias="uninterruptablePrice", ge=0)
    stock_status: str | None = Field(default=None, alias="stockStatus")
    support_public_ip: bool | None = Field(default=None, alias="supportPublicIp")
    max_gpu_count: int | None = Field(default=None, alias="maxGpuCount", ge=0)
    available_gpu_counts: list[int] | None = Field(default=None, alias="availableGpuCounts")


class _GraphqlGpuType(_WireModel):
    id: str = Field(min_length=1)
    display_name: str | None = Field(default=None, alias="displayName")
    memory_gb: int | None = Field(default=None, alias="memoryInGb", ge=0)
    lowest_price: _GraphqlLowestPrice | None = Field(default=None, alias="lowestPrice")


class _GraphqlOfferData(_WireModel):
    gpu_types: list[_GraphqlGpuType] = Field(alias="gpuTypes")


class _GraphqlOfferResponse(_WireModel):
    data: _GraphqlOfferData | None = None
    errors: list[_GraphqlError] = Field(default_factory=list)


class _GraphqlCreatedPod(_WireModel):
    id: str = Field(min_length=1)


class _GraphqlCreateData(_WireModel):
    pod: _GraphqlCreatedPod | None = Field(default=None, alias="podFindAndDeployOnDemand")


class _GraphqlCreateResponse(_WireModel):
    data: _GraphqlCreateData | None = None
    errors: list[_GraphqlError] = Field(default_factory=list)


class RunPodClient:
    """RunPod v2 client with safe read retries and conservative mutations."""

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        ssm_client: SsmClient | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Self:
        """Resolve one runtime credential and construct a client that reuses it."""

        credential = resolve_runpod_api_key(settings, ssm_client=ssm_client)
        return cls(
            settings.runpod,
            api_key=credential.api_key,
            transport=transport,
            sleep=sleep,
        )

    def __init__(
        self,
        config: RunPodConfig,
        *,
        api_key: SecretStr | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved_api_key = api_key if api_key is not None else config.api_key
        if resolved_api_key is None or not resolved_api_key.get_secret_value():
            raise ConfigurationError("RUNPOD_API_KEY is required for RunPod commands")
        _require_v2_base_url(str(config.api_url))

        self._config = config
        self._sleep = sleep
        base_url = f"{str(config.api_url).rstrip('/')}/"
        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {resolved_api_key.get_secret_value()}",
                "Accept": "application/json",
                "User-Agent": "wavcse-infra/0.1",
            },
            timeout=httpx.Timeout(config.request_timeout_seconds),
            transport=transport,
        )
        self._graphql_url = str(config.graphql_url)
        self._graphql_client = httpx.Client(
            headers={
                "Authorization": f"Bearer {resolved_api_key.get_secret_value()}",
                "Accept": "application/json",
                "User-Agent": "wavcse-infra/0.1",
            },
            timeout=httpx.Timeout(config.request_timeout_seconds),
            transport=transport,
        )

    def __enter__(self) -> RunPodClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        self._graphql_client.close()

    def list_workers(self) -> list[Worker]:
        """Return every standalone Pod visible to the configured account."""

        workers: list[Worker] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str | int] = {"limit": _LIST_PAGE_SIZE}
            if cursor is not None:
                params["cursor"] = cursor
            payload = self._get_json("pods", operation="list Pods", params=params)
            try:
                page = _PodList.model_validate(payload)
                workers.extend(_normalize_pod(pod) for pod in page.pods)
            except ValidationError as exc:
                raise ProviderResponseError(
                    f"RunPod list Pods returned an unexpected response: {_validation_summary(exc)}"
                ) from exc

            if not page.pagination.has_next_page:
                return workers
            cursor = page.pagination.next_cursor
            if cursor is None or cursor in seen_cursors:
                raise ProviderResponseError("RunPod list Pods returned invalid pagination metadata")
            seen_cursors.add(cursor)

    def get_worker(self, worker_id: str) -> Worker:
        """Return one Pod by exact immutable provider ID."""

        normalized_id = _worker_id(worker_id)
        payload = self._get_json(
            f"pods/{quote(normalized_id, safe='')}",
            operation=f"show Pod {normalized_id}",
            worker_id=normalized_id,
        )
        return _parse_pod(payload, operation=f"show Pod {normalized_id}")

    def list_gpu_offers(
        self,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str] = (),
        require_public_ip: bool = False,
    ) -> list[GpuOffer]:
        """Return current Pod prices and availability for a cloud/count selection."""

        if require_public_ip:
            return self._list_public_ip_offers(
                cloud_type,
                gpu_count,
                data_center_ids=data_center_ids,
            )

        params = _catalog_params(cloud_type, gpu_count)
        payload = self._get_json(
            "catalog/gpus",
            operation="list GPU types",
            params=params,
        )
        try:
            catalog = _GpuList.model_validate(payload)
            return [
                _normalize_gpu_offer(gpu, cloud_type, gpu_count, data_center_ids)
                for gpu in catalog.gpus
            ]
        except ValidationError as exc:
            raise ProviderResponseError(
                f"RunPod list GPU types returned an unexpected response: {_validation_summary(exc)}"
            ) from exc

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str] = (),
        require_public_ip: bool = False,
    ) -> GpuOffer:
        """Return one exact GPU type's current price and availability."""

        normalized_gpu_type = gpu_type.strip()
        if not normalized_gpu_type:
            raise ProviderValidationError("RunPod GPU type ID must not be empty")
        if require_public_ip:
            offers = self._list_public_ip_offers(
                cloud_type,
                gpu_count,
                data_center_ids=data_center_ids,
                gpu_type=normalized_gpu_type,
            )
            if not offers:
                raise ProviderNotFoundError(f"RunPod GPU type {normalized_gpu_type} was not found")
            return offers[0]
        payload = self._get_json(
            f"catalog/gpus/{quote(normalized_gpu_type, safe='')}",
            operation=f"show GPU type {normalized_gpu_type}",
            params=_catalog_params(cloud_type, gpu_count),
        )
        try:
            gpu = _GpuType.model_validate(payload)
            return _normalize_gpu_offer(gpu, cloud_type, gpu_count, data_center_ids)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"RunPod show GPU type {normalized_gpu_type} returned an unexpected response: "
                f"{_validation_summary(exc)}"
            ) from exc

    def create_worker(self, spec: WorkerSpec) -> Worker:
        """Create one Pod without ever blindly retrying the paid POST."""

        if spec.interruptible:
            raise ProviderValidationError(
                "RunPod REST API v2 does not expose interruptible Pod creation; "
                "omit --interruptible to create an on-demand Pod"
            )
        if spec.require_direct_ssh:
            return self._create_public_ip_worker(spec)
        try:
            response = self._client.post("pods", json=_create_payload(spec))
        except httpx.TransportError as exc:
            return self._reconcile_ambiguous_create(spec, cause=exc)

        if response.status_code >= 500 or response.status_code in _AMBIGUOUS_MUTATION_STATUS_CODES:
            return self._reconcile_ambiguous_create(
                spec,
                cause=ProviderOperationAmbiguousError(
                    f"RunPod create Pod returned HTTP {response.status_code}"
                ),
            )
        _raise_for_provider_status(response, "create Pod")
        try:
            return _parse_pod(response.json(), operation="create Pod")
        except (ValueError, ProviderResponseError) as exc:
            return self._reconcile_ambiguous_create(spec, cause=exc)

    def _list_public_ip_offers(
        self,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str],
        gpu_type: str | None = None,
    ) -> list[GpuOffer]:
        if gpu_count < 1:
            raise ProviderValidationError("RunPod GPU count must be at least 1")
        variables: dict[str, Any] = {
            "gpu": {"id": gpu_type} if gpu_type is not None else {},
            "price": {
                "gpuCount": gpu_count,
                "secureCloud": cloud_type is CloudType.SECURE,
                "supportPublicIp": True,
            },
        }
        if data_center_ids:
            variables["price"]["dataCenterId"] = ",".join(data_center_ids)
        payload = self._graphql_json(
            _PUBLIC_IP_OFFERS_QUERY,
            variables,
            operation="list public-IP-capable GPU offers",
        )
        try:
            response = _GraphqlOfferResponse.model_validate(payload)
        except ValidationError as exc:
            raise ProviderResponseError(
                "RunPod public-IP GPU discovery returned an unexpected response: "
                f"{_validation_summary(exc)}"
            ) from exc
        if response.errors:
            raise ProviderValidationError(
                f"RunPod rejected public-IP GPU discovery: {_graphql_error_detail(response.errors)}"
            )
        if response.data is None:
            raise ProviderResponseError("RunPod public-IP GPU discovery returned no data")
        offers = [
            _normalize_public_ip_offer(gpu, cloud_type, gpu_count)
            for gpu in response.data.gpu_types
        ]
        if gpu_type is not None:
            return offers
        return [
            offer
            for offer in offers
            if offer.public_ip_capable is True
            and offer.availability not in {Availability.NONE, Availability.UNKNOWN}
        ]

    def _create_public_ip_worker(self, spec: WorkerSpec) -> Worker:
        variables = {"input": _graphql_create_input(spec)}
        try:
            response = self._graphql_client.post(
                self._graphql_url,
                json={"query": _CREATE_PUBLIC_IP_POD_MUTATION, "variables": variables},
            )
        except httpx.TransportError as exc:
            return self._reconcile_ambiguous_create(spec, cause=exc)
        if response.status_code >= 500 or response.status_code in _AMBIGUOUS_MUTATION_STATUS_CODES:
            return self._reconcile_ambiguous_create(
                spec,
                cause=ProviderOperationAmbiguousError(
                    f"RunPod public-IP create returned HTTP {response.status_code}"
                ),
            )
        _raise_for_provider_status(response, "create public-IP Pod")
        try:
            parsed = _GraphqlCreateResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            return self._reconcile_ambiguous_create(spec, cause=exc)
        if parsed.data is not None and parsed.data.pod is not None:
            return Worker(
                id=parsed.data.pod.id,
                name=spec.name,
                state=WorkerState.PROVISIONING,
                native_status="CREATED",
                gpu_type=spec.gpu_type,
                gpu_count=spec.gpu_count,
                cloud_type=spec.cloud_type,
                exposed_ports=("22/tcp",),
                image=spec.image,
                template_id=spec.template_id,
                container_disk_gb=spec.container_disk_gb,
                volume_gb=spec.volume_gb,
                volume_mount_path=spec.volume_mount_path,
                network_volume_id=spec.network_volume_id,
            )
        if parsed.errors:
            raise ProviderValidationError(
                "RunPod rejected public-IP Pod creation before allocating a Pod: "
                f"{_graphql_error_detail(parsed.errors)}"
            )
        return self._reconcile_ambiguous_create(
            spec,
            cause=ProviderResponseError("RunPod public-IP create returned no Pod ID"),
        )

    def _graphql_json(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        operation: str,
    ) -> Any:
        attempts = self._config.max_read_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = self._graphql_client.post(
                    self._graphql_url, json={"query": query, "variables": variables}
                )
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise ProviderUnavailableError(
                        f"RunPod {operation} failed after {attempts} attempt(s): {redact(exc)}"
                    ) from exc
                self._backoff(attempt)
                continue
            if _is_retryable(response.status_code):
                if attempt == attempts:
                    raise ProviderUnavailableError(
                        f"RunPod {operation} failed after {attempts} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt)
                continue
            _raise_for_provider_status(response, operation)
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderResponseError(
                    f"RunPod {operation} returned invalid JSON (HTTP {response.status_code})"
                ) from exc
        raise AssertionError("bounded RunPod GraphQL retry loop exited unexpectedly")

    def start_worker(self, worker_id: str) -> Worker:
        """Request a start transition exactly once."""

        return self._worker_action(worker_id, "start")

    def stop_worker(self, worker_id: str) -> Worker:
        """Request a stop transition exactly once."""

        return self._worker_action(worker_id, "stop")

    def destroy_worker(self, worker_id: str) -> None:
        """Terminate one Pod by exact provider ID without mutation retries."""

        normalized_id = _worker_id(worker_id)
        operation = f"destroy Pod {normalized_id}"
        try:
            response = self._client.delete(f"pods/{quote(normalized_id, safe='')}")
        except httpx.TransportError as exc:
            raise ProviderOperationAmbiguousError(
                f"RunPod {operation} lost its response; provider state must be reconciled"
            ) from exc
        if response.status_code >= 500 or response.status_code in _AMBIGUOUS_MUTATION_STATUS_CODES:
            raise ProviderOperationAmbiguousError(
                f"RunPod {operation} returned HTTP {response.status_code}; "
                "provider state must be reconciled"
            )
        _raise_for_provider_status(response, operation, normalized_id)
        if response.status_code != 204:
            raise ProviderResponseError(
                f"RunPod {operation} returned unexpected HTTP {response.status_code}"
            )

    def _worker_action(self, worker_id: str, action: Literal["start", "stop"]) -> Worker:
        normalized_id = _worker_id(worker_id)
        operation = f"{action} Pod {normalized_id}"
        try:
            response = self._client.post(
                f"pods/{quote(normalized_id, safe='')}/action",
                json={"action": action},
            )
        except httpx.TransportError as exc:
            raise ProviderOperationAmbiguousError(
                f"RunPod {operation} lost its response; provider state must be reconciled"
            ) from exc
        if response.status_code >= 500 or response.status_code in _AMBIGUOUS_MUTATION_STATUS_CODES:
            raise ProviderOperationAmbiguousError(
                f"RunPod {operation} returned HTTP {response.status_code}; "
                "provider state must be reconciled"
            )
        _raise_for_provider_status(response, operation, normalized_id)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                f"RunPod {operation} returned invalid JSON (HTTP {response.status_code})"
            ) from exc
        return _parse_pod(payload, operation=operation)

    def _reconcile_ambiguous_create(self, spec: WorkerSpec, *, cause: Exception) -> Worker:
        last_error: ProviderError | None = None
        for attempt in range(1, self._config.create_reconcile_attempts + 1):
            try:
                matches = [worker for worker in self.list_workers() if worker.name == spec.name]
            except ProviderError as exc:
                last_error = exc
                matches = []
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AmbiguousCreateError(
                    f"RunPod create result is ambiguous: {len(matches)} Pods have exact "
                    f"infra identity {spec.name!r}; no create retry was attempted"
                ) from cause
            if attempt < self._config.create_reconcile_attempts:
                self._backoff(attempt)

        detail = f"; last reconciliation error: {last_error}" if last_error else ""
        raise AmbiguousCreateError(
            "RunPod may have created a paid Pod, but no response was received and exact-name "
            f"reconciliation found no Pod with infra identity {spec.name!r}{detail}. "
            "The create request was not retried; inspect `infra worker list` before trying again."
        ) from cause

    def _get_json(
        self,
        path: str,
        *,
        operation: str,
        worker_id: str | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> Any:
        attempts = self._config.max_read_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == attempts:
                    raise ProviderUnavailableError(
                        f"RunPod {operation} failed after {attempts} attempt(s): {redact(exc)}"
                    ) from exc
                self._backoff(attempt)
                continue

            if _is_retryable(response.status_code):
                if attempt == attempts:
                    raise ProviderUnavailableError(
                        f"RunPod {operation} failed after {attempts} attempt(s): "
                        f"HTTP {response.status_code}"
                    )
                self._backoff(attempt)
                continue

            _raise_for_provider_status(response, operation, worker_id)
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderResponseError(
                    f"RunPod {operation} returned invalid JSON (HTTP {response.status_code})"
                ) from exc

        raise AssertionError("bounded RunPod retry loop exited unexpectedly")

    def _backoff(self, attempt: int) -> None:
        delay = self._config.retry_backoff_seconds * (2 ** (attempt - 1))
        self._sleep(min(delay, _MAX_RETRY_DELAY_SECONDS))


def _require_v2_base_url(api_url: str) -> None:
    path = urlsplit(api_url).path.rstrip("/")
    if not path.endswith("/v2"):
        raise ConfigurationError(
            "RunPod REST API v2 is required for worker lifecycle management; set "
            "runpod.api_url or WAVCSE_INFRA_RUNPOD_API_URL to https://api.runpod.io/v2"
        )


def _worker_id(worker_id: str) -> str:
    normalized_id = worker_id.strip()
    if not normalized_id:
        raise ProviderError("RunPod worker ID must not be empty")
    return normalized_id


def _catalog_params(cloud_type: CloudType, gpu_count: int) -> dict[str, str | int]:
    if gpu_count < 1:
        raise ProviderValidationError("RunPod GPU count must be at least 1")
    return {
        "include": "AVAILABILITY",
        "product": "POD",
        "count": gpu_count,
        "cloud": cloud_type.value,
    }


def _create_payload(spec: WorkerSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": spec.name,
        "cloud": spec.cloud_type.value,
        "gpu": {"id": spec.gpu_type, "count": spec.gpu_count},
        "disk": spec.container_disk_gb,
    }
    if spec.image is not None:
        payload["image"] = spec.image
    if spec.template_id is not None:
        payload["templateId"] = spec.template_id
    if spec.data_center_ids:
        payload["dataCenterIds"] = list(spec.data_center_ids)
    if spec.volume_gb:
        payload["mounts"] = {"persistent": {"size": spec.volume_gb, "path": spec.volume_mount_path}}
    elif spec.network_volume_id is not None:
        payload["mounts"] = {
            "network": [{"volumeId": spec.network_volume_id, "path": spec.volume_mount_path}]
        }
    if spec.start_ssh:
        payload["startSsh"] = True
        payload["ports"] = ["22/tcp"]
    return payload


def _graphql_create_input(spec: WorkerSpec) -> dict[str, Any]:
    """Build the documented GraphQL placement request for public-IP capacity."""

    payload: dict[str, Any] = {
        "name": spec.name,
        "cloudType": spec.cloud_type.value,
        "gpuTypeId": spec.gpu_type,
        "gpuCount": spec.gpu_count,
        "containerDiskInGb": spec.container_disk_gb,
        "volumeMountPath": spec.volume_mount_path,
        "startSsh": True,
        "supportPublicIp": True,
        "ports": "22/tcp",
    }
    if spec.image is not None:
        payload["imageName"] = spec.image
    if spec.template_id is not None:
        payload["templateId"] = spec.template_id
    if spec.data_center_ids:
        payload["dataCenterId"] = ",".join(spec.data_center_ids)
    if spec.volume_gb:
        payload["volumeInGb"] = spec.volume_gb
    if spec.network_volume_id is not None:
        payload["networkVolumeId"] = spec.network_volume_id
    return payload


def _parse_pod(payload: Any, *, operation: str) -> Worker:
    try:
        return _normalize_pod(_Pod.model_validate(payload))
    except ValidationError as exc:
        raise ProviderResponseError(
            f"RunPod {operation} returned an unexpected response: {_validation_summary(exc)}"
        ) from exc


def _normalize_pod(pod: _Pod) -> Worker:
    mounts = pod.mounts or _Mounts()
    persistent = mounts.persistent
    network = mounts.network[0] if mounts.network else None
    proxy = _connection_info(pod.id, "proxy", pod.ssh.proxy if pod.ssh is not None else None)
    direct = _connection_info(pod.id, "direct", pod.ssh.direct if pod.ssh is not None else None)
    return Worker(
        id=pod.id,
        name=pod.name,
        state=_normalize_status(pod.status),
        native_status=pod.status,
        gpu_type=pod.gpu.id if pod.gpu is not None else None,
        gpu_count=pod.gpu.count if pod.gpu is not None else None,
        cloud_type=_normalize_cloud(pod.cloud),
        hourly_cost=pod.cost,
        public_ip=direct.host if direct is not None else None,
        ssh_port=direct.port if direct is not None else None,
        exposed_ports=tuple(pod.ports),
        ssh_proxy=proxy,
        ssh_direct=direct,
        datacenter=pod.data_center_id,
        image=pod.image,
        template_id=pod.template,
        container_disk_gb=pod.disk,
        volume_gb=persistent.size if persistent is not None else None,
        volume_mount_path=(
            persistent.path
            if persistent is not None
            else network.path
            if network is not None
            else None
        ),
        network_volume_id=network.volume_id if network is not None else None,
        created_at=pod.created_at,
        last_started_at=pod.started_at,
    )


def _connection_info(
    worker_id: str,
    kind: Literal["proxy", "direct"],
    endpoint: _SshEndpoint | None,
) -> WorkerConnectionInfo | None:
    if endpoint is None:
        return None
    return WorkerConnectionInfo(
        provider_worker_id=worker_id,
        kind=kind,
        host=endpoint.host,
        port=endpoint.port,
        username=endpoint.username,
    )


def _normalize_gpu_offer(
    gpu: _GpuType,
    cloud_type: CloudType,
    gpu_count: int,
    requested_data_centers: Sequence[str],
) -> GpuOffer:
    supports_cloud = gpu.secure if cloud_type is CloudType.SECURE else gpu.community
    price = gpu.price.secure if cloud_type is CloudType.SECURE else gpu.price.community
    maximum = gpu.max_count.secure if cloud_type is CloudType.SECURE else gpu.max_count.community
    data_centers = tuple(
        GpuDataCenterAvailability(
            id=data_center.id,
            name=data_center.name,
            availability=_normalize_availability(data_center.availability),
        )
        for data_center in gpu.data_centers
        if not requested_data_centers or data_center.id in requested_data_centers
    )
    availability = _normalize_availability(gpu.availability)
    if requested_data_centers:
        availability = _best_availability(data_centers)
    if not supports_cloud or (maximum is not None and maximum < gpu_count):
        availability = Availability.NONE
    return GpuOffer(
        gpu_type_id=gpu.id,
        display_name=gpu.name or gpu.id,
        memory_gb=gpu.memory,
        cloud_type=cloud_type,
        gpu_count=gpu_count,
        maximum_gpu_count=maximum,
        availability=availability,
        price_per_gpu_hour=price,
        total_price_per_hour=(price * Decimal(gpu_count) if price is not None else None),
        data_centers=data_centers,
    )


def _normalize_public_ip_offer(
    gpu: _GraphqlGpuType,
    cloud_type: CloudType,
    gpu_count: int,
) -> GpuOffer:
    compatible = gpu.lowest_price
    price = compatible.uninterruptable_price if compatible is not None else None
    maximum = compatible.max_gpu_count if compatible is not None else None
    availability = Availability.NONE
    public_ip_capable: bool | None = None
    if compatible is not None:
        availability = _normalize_graphql_availability(compatible.stock_status)
        public_ip_capable = compatible.support_public_ip
        if (
            public_ip_capable is None
            and price is not None
            and availability
            not in {
                Availability.NONE,
                Availability.UNKNOWN,
            }
        ):
            # The result exists only after applying supportPublicIp=true to lowestPrice.
            # RunPod currently leaves the redundant result field null.
            public_ip_capable = True
        if compatible.available_gpu_counts and gpu_count not in compatible.available_gpu_counts:
            availability = Availability.NONE
        if compatible.support_public_ip is False:
            availability = Availability.NONE
    return GpuOffer(
        gpu_type_id=gpu.id,
        display_name=gpu.display_name or gpu.id,
        memory_gb=gpu.memory_gb,
        cloud_type=cloud_type,
        gpu_count=gpu_count,
        maximum_gpu_count=maximum,
        availability=availability,
        price_per_gpu_hour=price,
        total_price_per_hour=(price * Decimal(gpu_count) if price is not None else None),
        public_ip_capable=public_ip_capable,
    )


def _normalize_status(native_status: str | None) -> WorkerState:
    return {
        "PROVISIONING": WorkerState.PROVISIONING,
        "STARTING": WorkerState.STARTING,
        "RUNNING": WorkerState.RUNNING,
        "EXITED": WorkerState.STOPPED,
        "ERROR": WorkerState.ERROR,
        "TERMINATED": WorkerState.DESTROYED,
    }.get((native_status or "").upper(), WorkerState.UNKNOWN)


def _normalize_cloud(native_cloud: str | None) -> CloudType | None:
    try:
        return CloudType((native_cloud or "").upper())
    except ValueError:
        return None


def _normalize_availability(native_availability: str | None) -> Availability:
    try:
        return Availability((native_availability or "").upper())
    except ValueError:
        return Availability.UNKNOWN


def _normalize_graphql_availability(stock_status: str | None) -> Availability:
    normalized = (stock_status or "").strip().upper().replace(" ", "_")
    if normalized in {"AVAILABLE", "IN_STOCK"}:
        return Availability.HIGH
    if normalized in {"OUT_OF_STOCK", "UNAVAILABLE", "NONE"}:
        return Availability.NONE
    return _normalize_availability(normalized)


def _best_availability(data_centers: Sequence[GpuDataCenterAvailability]) -> Availability:
    order = {
        Availability.UNKNOWN: 0,
        Availability.NONE: 1,
        Availability.LOW: 2,
        Availability.MEDIUM: 3,
        Availability.HIGH: 4,
    }
    if not data_centers:
        return Availability.NONE
    return max((item.availability for item in data_centers), key=order.__getitem__)


def _is_retryable(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500


def _raise_for_provider_status(
    response: httpx.Response,
    operation: str,
    worker_id: str | None = None,
) -> None:
    status_code = response.status_code
    if 200 <= status_code < 300:
        return
    detail = _response_detail(response)
    suffix = f": {detail}" if detail else ""
    if status_code == 401:
        raise ProviderAuthenticationError(
            f"RunPod rejected RUNPOD_API_KEY while attempting to {operation} (HTTP 401)"
        )
    if status_code == 403:
        raise ProviderPermissionError(f"RunPod denied permission to {operation} (HTTP 403){suffix}")
    if status_code == 404 and worker_id is not None:
        raise ProviderNotFoundError(f"RunPod worker {worker_id} was not found (HTTP 404)")
    if status_code == 404:
        raise ProviderNotFoundError(f"RunPod could not {operation} (HTTP 404){suffix}")
    if status_code in {400, 413, 422}:
        raise ProviderValidationError(
            f"RunPod rejected the request to {operation} (HTTP {status_code}){suffix}"
        )
    if status_code == 409:
        raise ProviderConflictError(
            f"RunPod could not {operation} in the current state (HTTP 409){suffix}"
        )
    if status_code == 429:
        raise ProviderUnavailableError(f"RunPod rate limited {operation} (HTTP 429)")
    raise ProviderError(f"RunPod {operation} failed with HTTP {status_code}{suffix}")


def _response_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    detail = payload.get("detail") or payload.get("message") or payload.get("title")
    if not isinstance(detail, str) or not detail.strip():
        return None
    return redact(detail.strip())[:500]


def _validation_summary(exc: ValidationError) -> str:
    first_error = exc.errors(include_url=False, include_input=False)[0]
    location = ".".join(str(part) for part in first_error["loc"])
    return f"{location}: {first_error['msg']}"


def _graphql_error_detail(errors: Sequence[_GraphqlError]) -> str:
    return redact(errors[0].message.strip())[:500]
