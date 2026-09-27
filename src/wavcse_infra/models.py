"""Provider-neutral infrastructure models."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkerState(StrEnum):
    """Normalized worker states exposed by the stable CLI."""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    DESTROYED = "DESTROYED"
    UNKNOWN = "UNKNOWN"


class Worker(BaseModel):
    """Normalized read view of one provider worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["runpod"] = "runpod"
    id: str = Field(min_length=1)
    name: str | None = None
    state: WorkerState
    native_status: str | None = None
    gpu_type: str | None = None
    gpu_count: int | None = Field(default=None, ge=0)
    hourly_cost: Decimal | None = Field(default=None, ge=0)
    base_hourly_cost: Decimal | None = Field(default=None, ge=0)
    public_ip: str | None = None
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    datacenter: str | None = None
    image: str | None = None
    interruptible: bool | None = None
    last_started_at: datetime | None = None
