"""Provider-neutral infrastructure models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProviderKind(StrEnum):
    """Identity of a worker's authoritative provider."""

    RUNPOD = "runpod"
    COLAB = "colab"


class ExecutionTransport(StrEnum):
    """How the controller executes on a worker, independently of its provider."""

    SSH = "ssh"
    COLAB_EXEC = "colab_exec"


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


# Ordered ladder of proven capabilities. `FAILED` is not a rung: it is a distinct marker
# that the last full health inspection found a failing check, so it ranks below every
# proven capability and any weaker but successful observation may replace it.
READINESS_RANK: dict[WorkerReadinessState, int] = {
    WorkerReadinessState.FAILED: 0,
    WorkerReadinessState.NOT_READY: 1,
    WorkerReadinessState.SSH_READY: 2,
    WorkerReadinessState.BOOTSTRAPPED: 3,
    WorkerReadinessState.GPU_HEALTHY: 4,
    WorkerReadinessState.READY: 5,
}


def readiness_rank(state: WorkerReadinessState) -> int:
    """Return the position of one readiness state on the proven-capability ladder."""

    return READINESS_RANK[state]


def readiness_at_least(
    current: WorkerReadinessState,
    minimum: WorkerReadinessState,
) -> bool:
    """Return whether `current` already establishes at least `minimum`."""

    return readiness_rank(current) >= readiness_rank(minimum)


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


class VolumeType(StrEnum):
    """RunPod network volume storage tiers, which are immutable after creation."""

    STANDARD = "STANDARD"
    HIGH_PERFORMANCE = "HIGH_PERFORMANCE"


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

    provider: ProviderKind = ProviderKind.RUNPOD
    execution_transport: ExecutionTransport = ExecutionTransport.SSH
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
    # `mounts` is a required field of the v2 Pod response, so the difference between "the
    # provider reported no mount" and "the provider reported nothing at all" is real evidence.
    # Without it, an absent mount and a sparse create answer would be indistinguishable, and
    # a placement check could only guess.
    mounts_reported: bool = False
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

    provider: ProviderKind = ProviderKind.RUNPOD
    gpu_type_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    memory_gb: int | None = Field(default=None, ge=0)
    cloud_type: CloudType
    gpu_count: int = Field(ge=1)
    maximum_gpu_count: int | None = Field(default=None, ge=0)
    availability: Availability
    price_per_gpu_hour: Decimal | None = Field(default=None, ge=0)
    total_price_per_hour: Decimal | None = Field(default=None, ge=0)
    public_ip_capable: bool | None = None
    data_centers: tuple[GpuDataCenterAvailability, ...] = ()


class WorkerSpec(BaseModel):
    """RunPod Pod creation request; other providers have distinct allocation contracts."""

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
    require_direct_ssh: bool = False

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
        if self.require_direct_ssh and not self.start_ssh:
            raise ValueError("require_direct_ssh requires start_ssh")
        return self


class WorkerCreationPlan(BaseModel):
    """Validated request plus the provider offer used for cost confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: WorkerSpec
    offer: GpuOffer
    max_hourly_price: Decimal | None = Field(default=None, ge=0)


# RunPod requires a network volume to live inside one data center, and a Pod that mounts it
# must be placed in that same data center. These bounds are the provider's own documented
# contract for `POST /v2/network-volumes`, so an out-of-range size fails here rather than
# after a paid call.
MIN_NETWORK_VOLUME_SIZE_GB = 10
MAX_NETWORK_VOLUME_SIZE_GB = 4096


class NetworkVolume(BaseModel):
    """Normalized provider-authoritative view of one network volume.

    A network volume is persistent, provider-attached working storage. It is never
    canonical: the provider reports capacity and placement only, never artifact contents,
    and losing it is recoverable from canonical object storage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["runpod"] = "runpod"
    id: str = Field(min_length=1)
    name: str | None = None
    size_gb: int = Field(ge=0)
    datacenter: str = Field(min_length=1)
    volume_type: VolumeType | None = None


class NetworkVolumeSpec(BaseModel):
    """Explicit request used to create one network volume.

    `name` is the infra identity: an operator-supplied prefix plus a generated
    high-entropy suffix. The provider does not require volume names to be unique, so that
    exact name is what reconciles an ambiguous create.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=191)
    size_gb: int = Field(ge=MIN_NETWORK_VOLUME_SIZE_GB, le=MAX_NETWORK_VOLUME_SIZE_GB)
    datacenter: str = Field(min_length=1)
    volume_type: VolumeType | None = None

    @field_validator("datacenter")
    @classmethod
    def validate_datacenter(cls, value: str) -> str:
        """Reject data-center text that could not be one exact provider identifier."""

        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("data center must be one exact provider identifier")
        return normalized


class DataCenterInfo(BaseModel):
    """Normalized catalog view of one provider data center.

    `network_volume_types` is empty when the data center cannot host a network volume at
    all, which is what makes this usable as a placement guard rather than as decoration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    name: str | None = None
    region: str | None = None
    network_volume_types: tuple[VolumeType, ...] = ()
    global_network: bool | None = None
    gpu_availability: tuple[GpuDataCenterAvailability, ...] = ()

    def supports_network_volume(self, volume_type: VolumeType | None = None) -> bool:
        """Return whether this data center can host the requested storage tier."""

        if not self.network_volume_types:
            return False
        return volume_type is None or volume_type in self.network_volume_types


class NetworkVolumeBillingRecord(BaseModel):
    """One provider-reported time bucket of network volume storage charges."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    volume_id: str = Field(min_length=1)
    total_amount_usd: Decimal = Field(ge=0)
    standard_amount_usd: Decimal = Field(default=Decimal(0), ge=0)
    high_performance_amount_usd: Decimal = Field(default=Decimal(0), ge=0)
    start_time: datetime | None = None
    end_time: datetime | None = None


class NetworkVolumeBilling(BaseModel):
    """Provider-reported storage charges, which is the only place a real price is exposed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[NetworkVolumeBillingRecord, ...] = ()
    total_amount_usd: Decimal = Field(ge=0)
    unique_volume_count: int = Field(default=0, ge=0)


class NetworkVolumeCreationPlan(BaseModel):
    """Validated volume request plus the catalog facts shown before confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: NetworkVolumeSpec
    data_center: DataCenterInfo
    # RunPod exposes no network volume price through its API, so this is the provider's
    # published list price, labelled as such wherever it is displayed. `None` when no
    # published rate applies to the requested tier.
    published_list_price_usd_per_gb_month: Decimal | None = Field(default=None, ge=0)
    # The largest volume the published rate above is quoted for. RunPod publishes a
    # different rate for larger volumes of the same tier, so a request above this bound has
    # no estimate rather than one computed with a rate that does not apply to it.
    published_list_price_max_size_gb: int | None = Field(default=None, ge=1)

    @property
    def estimated_monthly_cost_usd(self) -> Decimal | None:
        """Return the list-price monthly cost of the requested size, when known."""

        if self.published_list_price_usd_per_gb_month is None:
            return None
        if (
            self.published_list_price_max_size_gb is not None
            and self.spec.size_gb > self.published_list_price_max_size_gb
        ):
            return None
        return (self.published_list_price_usd_per_gb_month * Decimal(self.spec.size_gb)).quantize(
            Decimal("0.01")
        )


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
