"""Provider-neutral infrastructure models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkerState(StrEnum):
    """Normalized worker states exposed by the stable CLI."""

    PROVISIONING = "PROVISIONING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    TERMINATING = "TERMINATING"
    DESTROYED = "DESTROYED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class WorkerReadinessState(StrEnum):
    """Controller-observed readiness, separate from provider lifecycle state."""

    NOT_READY = "NOT_READY"
    SSH_READY = "SSH_READY"
    BOOTSTRAPPED = "BOOTSTRAPPED"
    GPU_HEALTHY = "GPU_HEALTHY"
    READY = "READY"
    FAILED = "FAILED"


class HealthCheckStatus(StrEnum):
    """Normalized outcome of one worker readiness check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CloudType(StrEnum):
    """RunPod cloud tiers represented without provider wire objects."""

    SECURE = "SECURE"
    COMMUNITY = "COMMUNITY"


class Availability(StrEnum):
    """Normalized provider capacity indication."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


class WorkerConnectionInfo(BaseModel):
    """One provider-reported SSH endpoint; connectivity is not implied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_worker_id: str = Field(min_length=1)
    kind: Literal["proxy", "direct"]
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Reject endpoint text that could alter an OpenSSH argv."""

        normalized = value.strip()
        if (
            not normalized
            or normalized.startswith("-")
            or any(character.isspace() or ord(character) < 32 for character in normalized)
        ):
            raise ValueError("SSH host is malformed")
        return normalized

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        """Restrict SSH usernames to the portable account-name subset RunPod returns."""

        normalized = value.strip()
        if not normalized or any(
            not (character.isalnum() or character in "._-") for character in normalized
        ):
            raise ValueError("SSH username is malformed")
        return normalized


class Worker(BaseModel):
    """Normalized provider-authoritative view of one worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["runpod"] = "runpod"
    id: str = Field(min_length=1)
    name: str | None = None
    state: WorkerState
    native_status: str | None = None
    gpu_type: str | None = None
    gpu_count: int | None = Field(default=None, ge=0)
    cloud_type: CloudType | None = None
    hourly_cost: Decimal | None = Field(default=None, ge=0)
    public_ip: str | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    exposed_ports: tuple[str, ...] = ()
    ssh_proxy: WorkerConnectionInfo | None = None
    ssh_direct: WorkerConnectionInfo | None = None
    datacenter: str | None = None
    image: str | None = None
    template_id: str | None = None
    container_disk_gb: int | None = Field(default=None, ge=0)
    volume_gb: int | None = Field(default=None, ge=0)
    volume_mount_path: str | None = None
    network_volume_id: str | None = None
    interruptible: bool | None = None
    created_at: datetime | None = None
    last_started_at: datetime | None = None


class GpuDataCenterAvailability(BaseModel):
    """Availability for a GPU configuration in one provider data center."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str | None = None
    availability: Availability


class GpuOffer(BaseModel):
    """Normalized current catalog view for one GPU/cloud/count selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["runpod"] = "runpod"
    gpu_type_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    memory_gb: int | None = Field(default=None, ge=0)
    cloud_type: CloudType
    gpu_count: int = Field(ge=1)
    maximum_gpu_count: int | None = Field(default=None, ge=0)
    availability: Availability
    price_per_gpu_hour: Decimal | None = Field(default=None, ge=0)
    total_price_per_hour: Decimal | None = Field(default=None, ge=0)
    data_centers: tuple[GpuDataCenterAvailability, ...] = ()


class WorkerSpec(BaseModel):
    """Explicit provider-neutral request used to create one GPU worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=191)
    gpu_type: str = Field(min_length=1)
    gpu_count: int = Field(ge=1)
    cloud_type: CloudType
    image: str | None = Field(default=None, min_length=1)
    template_id: str | None = Field(default=None, min_length=1)
    container_disk_gb: int = Field(default=20, ge=1)
    volume_gb: int = Field(default=0, ge=0)
    volume_mount_path: str = Field(default="/workspace", min_length=1)
    network_volume_id: str | None = Field(default=None, min_length=1)
    data_center_ids: tuple[str, ...] = ()
    interruptible: bool = False
    start_ssh: bool = False

    @model_validator(mode="after")
    def validate_image_and_storage(self) -> WorkerSpec:
        """Reject ambiguous image and storage choices before provider calls."""

        if (self.image is None) == (self.template_id is None):
            raise ValueError("exactly one of image or template_id must be provided")
        if self.network_volume_id is not None and self.volume_gb:
            raise ValueError("volume_gb and network_volume_id are mutually exclusive")
        if self.volume_gb and self.volume_gb < 10:
            raise ValueError("volume_gb must be 0 or at least 10 GB")
        if any(not data_center_id.strip() for data_center_id in self.data_center_ids):
            raise ValueError("data center IDs must not be empty")
        return self


class WorkerCreationPlan(BaseModel):
    """Validated request plus the provider offer used for cost confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: WorkerSpec
    offer: GpuOffer
    max_hourly_price: Decimal | None = Field(default=None, ge=0)


class WorkerHealthCheck(BaseModel):
    """One normalized check from a worker health inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    status: HealthCheckStatus
    detail: str = Field(min_length=1)


class WorkerGpuInfo(BaseModel):
    """GPU facts reported by vendor tooling on the worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor: Literal["NVIDIA"] = "NVIDIA"
    count: int = Field(ge=0)
    models: tuple[str, ...] = ()
    memory_mib: tuple[int, ...] = ()
    driver_version: str | None = None
    cuda_version: str | None = None


class WorkerHealthReport(BaseModel):
    """Normalized provider, SSH, bootstrap, disk, tool, and GPU readiness report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_worker_id: str = Field(min_length=1)
    provider_state: WorkerState
    readiness_state: WorkerReadinessState
    connection: WorkerConnectionInfo
    bootstrap_version_expected: str = Field(min_length=1)
    bootstrap_version_observed: str | None = None
    disk_path: str | None = None
    disk_available_bytes: int | None = Field(default=None, ge=0)
    git_version: str | None = None
    python_version: str | None = None
    uv_version: str | None = None
    gpu: WorkerGpuInfo | None = None
    checks: tuple[WorkerHealthCheck, ...]

    @property
    def ready(self) -> bool:
        return self.readiness_state is WorkerReadinessState.READY
