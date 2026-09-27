"""Read-only client for RunPod REST API v1 Pod endpoints."""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError

from wavcse_infra.config import RunPodConfig, Settings
from wavcse_infra.credentials import SsmClient, resolve_runpod_api_key
from wavcse_infra.errors import (
    ConfigurationError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderNotFoundError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from wavcse_infra.models import Worker, WorkerState
from wavcse_infra.redaction import redact

_RETRYABLE_STATUS_CODES = frozenset({429})
_MAX_RETRY_DELAY_SECONDS = 30.0
_POD_READ_PARAMS = {"includeMachine": "true"}


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _Gpu(_WireModel):
    id: str | None = None
    count: int | None = Field(default=None, ge=0)
    display_name: str | None = Field(default=None, alias="displayName")


class _MachineGpuType(_WireModel):
    id: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")


class _Machine(_WireModel):
    gpu_type_id: str | None = Field(default=None, alias="gpuTypeId")
    gpu_type: _MachineGpuType | None = Field(default=None, alias="gpuType")
    gpu_display_name: str | None = Field(default=None, alias="gpuDisplayName")
    location: str | None = None
    data_center_id: str | None = Field(default=None, alias="dataCenterId")


class _Pod(_WireModel):
    id: str = Field(min_length=1)
    name: str | None = None
    adjusted_cost_per_hour: Decimal | None = Field(default=None, alias="adjustedCostPerHr")
    cost_per_hour: Decimal | None = Field(default=None, alias="costPerHr")
    desired_status: str | None = Field(default=None, alias="desiredStatus")
    gpu: _Gpu | None = None
    machine: _Machine | None = None
    port_mappings: dict[str, int] | None = Field(default=None, alias="portMappings")
    public_ip: str | None = Field(default=None, alias="publicIp")
    image: str | None = None
    interruptible: bool | None = None
    last_started_at: str | None = Field(default=None, alias="lastStartedAt")


_POD_LIST_ADAPTER = TypeAdapter(list[_Pod])


class RunPodClient:
    """Bounded-retry client for safe RunPod Pod reads."""

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

    def __enter__(self) -> RunPodClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def list_workers(self) -> list[Worker]:
        """Return all Pods visible to the configured RunPod account."""

        payload = self._get_json("pods", operation="list Pods")
        try:
            pods = _POD_LIST_ADAPTER.validate_python(payload)
            return [_normalize_pod(pod) for pod in pods]
        except ValidationError as exc:
            raise ProviderResponseError(
                f"RunPod list Pods returned an unexpected response: {_validation_summary(exc)}"
            ) from exc

    def get_worker(self, worker_id: str) -> Worker:
        """Return one Pod by immutable provider ID."""

        normalized_id = worker_id.strip()
        if not normalized_id:
            raise ProviderError("RunPod worker ID must not be empty")
        encoded_id = quote(normalized_id, safe="")
        payload = self._get_json(
            f"pods/{encoded_id}",
            operation=f"show Pod {normalized_id}",
            worker_id=normalized_id,
        )
        try:
            pod = _Pod.model_validate(payload)
            return _normalize_pod(pod)
        except ValidationError as exc:
            raise ProviderResponseError(
                f"RunPod show Pod {normalized_id} returned an unexpected response: "
                f"{_validation_summary(exc)}"
            ) from exc

    def _get_json(
        self,
        path: str,
        *,
        operation: str,
        worker_id: str | None = None,
    ) -> Any:
        attempts = self._config.max_read_attempts
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.get(path, params=_POD_READ_PARAMS)
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

            _raise_for_provider_status(response.status_code, operation, worker_id)
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


def _is_retryable(status_code: int) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500


def _raise_for_provider_status(status_code: int, operation: str, worker_id: str | None) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in {401, 403}:
        raise ProviderAuthenticationError(
            f"RunPod rejected RUNPOD_API_KEY while attempting to {operation} (HTTP {status_code})"
        )
    if status_code == 404 and worker_id is not None:
        raise ProviderNotFoundError(f"RunPod worker {worker_id} was not found (HTTP 404)")
    raise ProviderError(f"RunPod {operation} failed with HTTP {status_code}")


def _normalize_pod(pod: _Pod) -> Worker:
    machine = pod.machine
    machine_gpu = machine.gpu_type if machine is not None else None
    gpu_type = _first_present(
        pod.gpu.display_name if pod.gpu is not None else None,
        machine.gpu_display_name if machine is not None else None,
        machine_gpu.display_name if machine_gpu is not None else None,
        machine.gpu_type_id if machine is not None else None,
        pod.gpu.id if pod.gpu is not None else None,
    )
    port_mappings = pod.port_mappings or {}
    hourly_cost = (
        pod.adjusted_cost_per_hour if pod.adjusted_cost_per_hour is not None else pod.cost_per_hour
    )
    return Worker(
        id=pod.id,
        name=pod.name,
        state=_normalize_status(pod.desired_status),
        native_status=pod.desired_status,
        gpu_type=gpu_type,
        gpu_count=pod.gpu.count if pod.gpu is not None else None,
        hourly_cost=hourly_cost,
        base_hourly_cost=pod.cost_per_hour,
        public_ip=pod.public_ip,
        ssh_port=port_mappings.get("22"),
        datacenter=(
            _first_present(machine.data_center_id, machine.location)
            if machine is not None
            else None
        ),
        image=pod.image,
        interruptible=pod.interruptible,
        last_started_at=pod.last_started_at,
    )


def _normalize_status(native_status: str | None) -> WorkerState:
    return {
        "RUNNING": WorkerState.RUNNING,
        "EXITED": WorkerState.STOPPED,
        "TERMINATED": WorkerState.DESTROYED,
    }.get((native_status or "").upper(), WorkerState.UNKNOWN)


def _first_present(*values: str | None) -> str | None:
    return next((value for value in values if value), None)


def _validation_summary(exc: ValidationError) -> str:
    first_error = exc.errors(include_url=False, include_input=False)[0]
    location = ".".join(str(part) for part in first_error["loc"])
    return f"{location}: {first_error['msg']}"
