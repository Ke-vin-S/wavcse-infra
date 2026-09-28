"""Provider-neutral infrastructure models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    kind: Literal["proxy", "direct"]
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    username: str = Field(min_length=1)


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
