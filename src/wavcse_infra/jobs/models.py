"""Version 1 job specification, worker descriptor protocol, and durable job records.

A recorded job answers one question above all others: which source revision actually
executed. The specification therefore requires a full immutable commit, the worker
verifies `HEAD` after a detached checkout, and the verified object ID — never the
requested one alone — is what a record reports as executed.

Everything stored in a job specification is non-secret by construction. Secret runtime
values are referenced by environment-variable *name* only, and the resolved values are
delivered to the worker on the SSH stdin stream at start time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from wavcse_infra.errors import JobSpecError, StorageKeyError
from wavcse_infra.redaction import contains_bearer_material
from wavcse_infra.storage.keys import validate_object_key

JOB_SCHEMA_VERSION = 1
JOB_RECORD_SCHEMA_VERSION = 1
JOB_ID_PATTERN = r"^job-[0-9a-f]{16}$"
MAX_COMMAND_ARGUMENTS = 512
MAX_ARGUMENT_LENGTH = 8192
MAX_ENVIRONMENT_VALUE_LENGTH = 4096

_JOB_ID_REGEX = re.compile(JOB_ID_PATTERN)
_JOB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SECRET_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
# These name families are reserved by the controller, the infrastructure, or a cloud
# provider. Forwarding them could move a controller credential onto a disposable machine
# or let a job spec overwrite infrastructure provenance, so a job may never set them.
_RESERVED_ENVIRONMENT_PREFIXES = (
    "AWS_",
    "RUNPOD_",
    "WAVCSE_",
    "INFRA_",
    "SSH_",
)
# A non-secret environment entry must not be a credential by name; use environment_secrets.
_SECRET_HINT_FRAGMENTS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "APIKEY",
    "API_KEY",
    "PRIVATE_KEY",
)
# Worker-side directories that a declared output may never target: they hold the checked
# out source tree and internal runner state rather than experiment products.
_RESERVED_JOB_DIRECTORIES = frozenset({"source", "state"})
_DELETE_CHARACTER = 127
_PRINTABLE_ASCII_START = 32


class JobState(StrEnum):
    """Explicit lifecycle of one recorded job."""

    PENDING = "PENDING"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Terminal states are frozen: a new attempt is a new job ID, never a reopened record.
LEGAL_JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.PREPARING, JobState.FAILED, JobState.CANCELLED}),
    JobState.PREPARING: frozenset({JobState.RUNNING, JobState.FAILED, JobState.CANCELLED}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}
TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}
)


class JobPreparationPhase(StrEnum):
    """Durable controller record of which preparation step it had reached.

    This is evidence, not state: it says what the *controller* had issued when it last
    wrote the record, which is what lets a later reconciliation tell "the command never
    started" apart from "the command may have started and the workspace is gone".
    """

    INSTALLING_RUNNER = "installing_runner"
    PREPARING_SOURCE = "preparing_source"
    MATERIALIZING_INPUTS = "materializing_inputs"
    STARTING_COMMAND = "starting_command"


def can_transition(current: JobState, target: JobState) -> bool:
    """Return whether one job state change is legal."""

    if current is target:
        return True
    return target in LEGAL_JOB_TRANSITIONS[current]


def ensure_transition(current: JobState, target: JobState) -> None:
    """Reject an impossible job state change instead of writing a contradictory record."""

    if not can_transition(current, target):
        raise JobSpecError(f"Invalid job state transition {current.value} -> {target.value}")


class JobSource(BaseModel):
    """Repository and exact immutable commit that must actually execute."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1, max_length=2048)
    commit: str

    @field_validator("repository")
    @classmethod
    def anonymous_https_repository(cls, value: str) -> str:
        """Require an anonymous HTTPS remote so a worker receives no Git credential."""

        normalized = value.strip()
        if normalized != value:
            raise ValueError("repository must not have surrounding whitespace")
        if any(
            character.isspace() or ord(character) < _PRINTABLE_ASCII_START
            for character in normalized
        ):
            raise ValueError("repository must not contain whitespace or control characters")
        if not normalized.startswith("https://"):
            raise ValueError(
                "repository must be an anonymous https:// URL; SSH remotes would require "
                "delivering a GitHub credential or key to a disposable worker"
            )
        remainder = normalized[len("https://") :]
        if not remainder or remainder.startswith("/"):
            raise ValueError("repository must name a host")
        authority = remainder.split("/", maxsplit=1)[0]
        if "@" in authority:
            raise ValueError("repository must not embed credentials in its URL")
        if "?" in normalized or "#" in normalized:
            raise ValueError("repository must not contain a query string or fragment")
        return normalized

    @field_validator("commit")
    @classmethod
    def immutable_commit(cls, value: str) -> str:
        """Accept only a full Git object ID, never a branch, tag, prefix, or `HEAD`."""

        normalized = value.strip().lower()
        if not _GIT_COMMIT_PATTERN.fullmatch(normalized):
            raise ValueError(
                "commit must be a full 40 or 64 character hexadecimal commit ID; branches, "
                "tags, and short prefixes are not reproducible"
            )
        return normalized


class JobCommand(BaseModel):
    """The exact argv to execute; a shell command string is not representable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str | None = None

    @field_validator("argv")
    @classmethod
    def argv_is_a_reviewed_argument_vector(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Reject a shell-style command string or an argument that cannot be passed safely."""

        return _validate_argv(value)

    @field_validator("working_directory")
    @classmethod
    def working_directory_stays_inside_the_checkout(cls, value: str | None) -> str | None:
        """Allow only a relative subdirectory of the verified source checkout."""

        if value is None:
            return None
        return _validate_relative_path(value, "command.working_directory")


class JobSetup(BaseModel):
    """Optional explicit environment-preparation argv run before the job command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    argv: tuple[str, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def argv_is_a_reviewed_argument_vector(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Apply the same argv rules as the job command itself."""

        return _validate_argv(value)


class JobRuntime(BaseModel):
    """Bounded execution controls, non-secret environment, and secret name references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: int | None = Field(default=None, ge=1, le=604800)
    environment: dict[str, str] = Field(default_factory=dict)
    environment_secrets: tuple[str, ...] = ()

    @field_validator("environment")
    @classmethod
    def environment_is_non_secret(cls, value: dict[str, str]) -> dict[str, str]:
        """Refuse credential-shaped names and bearer-shaped values in literal variables."""

        for name, entry in value.items():
            if not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"environment variable name is malformed: {name!r}")
            if _is_reserved_environment_name(name):
                raise ValueError(
                    f"environment must never set the reserved infrastructure variable "
                    f"{name!r}; a worker never receives controller credentials and a job "
                    "never overrides infrastructure provenance"
                )
            upper = name.upper()
            if any(fragment in upper for fragment in _SECRET_HINT_FRAGMENTS):
                raise ValueError(
                    f"environment variable {name!r} looks like a credential; declare it in "
                    "runtime.environment_secrets instead of a literal value"
                )
            if not entry:
                raise ValueError(f"environment variable {name!r} must not be empty")
            if len(entry) > MAX_ENVIRONMENT_VALUE_LENGTH:
                raise ValueError(
                    f"environment variable {name!r} must not exceed "
                    f"{MAX_ENVIRONMENT_VALUE_LENGTH} characters"
                )
            if any(character in entry for character in ("\n", "\r", "\0")):
                raise ValueError(
                    f"environment variable {name!r} must not contain newlines or NUL bytes"
                )
            if contains_bearer_material(entry):
                raise ValueError(
                    f"environment variable {name!r} contains presigned URL or credential "
                    "material; job specifications must never persist bearer values"
                )
        return value

    @field_validator("environment_secrets")
    @classmethod
    def secret_names_are_explicit_and_allowed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Keep secret values out of the document while preventing controller leakage."""

        if len(set(value)) != len(value):
            raise ValueError("runtime.environment_secrets must not repeat a variable name")
        for name in value:
            if not _SECRET_NAME_PATTERN.fullmatch(name):
                raise ValueError(
                    f"runtime.environment_secrets entry is malformed: {name!r}; use an "
                    "uppercase environment variable name"
                )
            if _is_reserved_environment_name(name):
                raise ValueError(
                    f"runtime.environment_secrets must never request the reserved "
                    f"infrastructure variable {name!r}"
                )
        return value


class JobInput(BaseModel):
    """One Phase 5 artifact to materialize inside the job workspace before execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: str = Field(min_length=1)
    destination: str
    required: bool = True
    manifest: str | None = Field(default=None, min_length=1)
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)

    @field_validator("artifact", "manifest")
    @classmethod
    def artifact_keys_are_relative(cls, value: str | None) -> str | None:
        """Validate every storage key with the single Phase 5 namespace rule."""

        if value is None:
            return None
        try:
            return validate_object_key(value)
        except StorageKeyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("destination")
    @classmethod
    def destination_is_relative(cls, value: str) -> str:
        """Materialize only beneath the job's own inputs directory."""

        return _validate_relative_path(value, "input destination")

    @field_validator("sha256")
    @classmethod
    def sha256_is_canonical(cls, value: str | None) -> str | None:
        """Require a canonical lowercase hexadecimal SHA-256 digest."""

        if value is None:
            return None
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("input sha256 must be 64 lowercase hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def one_verification_source(self) -> JobInput:
        """Take verification expectations from a manifest or an explicit digest, not both."""

        if self.manifest is not None and (self.sha256 is not None or self.size_bytes is not None):
            raise ValueError(
                "an input must declare either a manifest or explicit sha256/size_bytes, not both"
            )
        if self.required and self.manifest is None and self.sha256 is None:
            raise ValueError(
                "a required input must declare a manifest or sha256; object size alone "
                "does not verify its content"
            )
        return self


class JobOutput(BaseModel):
    """One worker-produced path that must be persisted through Phase 5 before success."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    artifact: str = Field(min_length=1)
    required: bool = True
    overwrite: bool = False

    @field_validator("path")
    @classmethod
    def path_is_relative(cls, value: str) -> str:
        """Persist only inside the job workspace and never the checkout or runner state."""

        normalized = _validate_relative_path(value, "output path")
        if normalized.split("/")[0] in _RESERVED_JOB_DIRECTORIES:
            raise ValueError(
                f"output path must not target the reserved job directory "
                f"{normalized.split('/')[0]!r}"
            )
        return normalized

    @field_validator("artifact")
    @classmethod
    def artifact_key_is_relative(cls, value: str) -> str:
        """Validate the destination key with the Phase 5 namespace rule."""

        try:
            return validate_object_key(value)
        except StorageKeyError as exc:
            raise ValueError(str(exc)) from exc


class JobTracking(BaseModel):
    """Non-secret research metadata passed through to the research process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def metadata_is_flat_non_secret_text(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep pass-through metadata small, flat, and free of bearer material."""

        if len(value) > 64:
            raise ValueError("tracking.metadata must not exceed 64 entries")
        for key, entry in value.items():
            if not _ENVIRONMENT_NAME_PATTERN.fullmatch(key):
                raise ValueError(f"tracking metadata key is malformed: {key!r}")
            if not entry or len(entry) > 1024:
                raise ValueError(
                    f"tracking metadata {key!r} must be non-empty and at most 1024 characters"
                )
            if contains_bearer_material(entry):
                raise ValueError(
                    f"tracking metadata {key!r} contains presigned URL or credential material"
                )
        return value


class JobSpec(BaseModel):
    """Complete version 1 recorded-job request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    name: str = Field(min_length=1, max_length=81)
    source: JobSource
    command: JobCommand
    setup: JobSetup | None = None
    runtime: JobRuntime = JobRuntime()
    inputs: tuple[JobInput, ...] = ()
    outputs: tuple[JobOutput, ...] = ()
    tracking: JobTracking = JobTracking()

    @field_validator("name")
    @classmethod
    def name_is_a_safe_label(cls, value: str) -> str:
        """Keep the human name usable in logs, paths, and run labels."""

        normalized = value.strip()
        if not _JOB_NAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "name must start alphanumerically and contain only letters, digits, '.', "
                "'_' and '-'"
            )
        return normalized

    @model_validator(mode="after")
    def no_duplicate_declarations(self) -> JobSpec:
        """Reject ambiguous duplicate inputs and outputs before any worker is contacted."""

        artifacts = [job_input.artifact for job_input in self.inputs]
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("inputs must not declare the same artifact twice")
        destinations = [job_input.destination for job_input in self.inputs]
        if len(set(destinations)) != len(destinations):
            raise ValueError("inputs must not materialize to the same destination twice")
        paths = [output.path for output in self.outputs]
        if len(set(paths)) != len(paths):
            raise ValueError("outputs must not declare the same path twice")
        output_artifacts = [output.artifact for output in self.outputs]
        if len(set(output_artifacts)) != len(output_artifacts):
            raise ValueError("outputs must not declare the same artifact key twice")
        return self

    @property
    def secret_environment_names(self) -> tuple[str, ...]:
        """Return the explicit controller-side variable names this job needs."""

        return self.runtime.environment_secrets


def load_job_spec(text: str, *, source: str = "job specification") -> JobSpec:
    """Parse and validate a JSON job specification with an actionable failure message."""

    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JobSpecError(f"{source} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JobSpecError(f"{source} must be a JSON object")
    try:
        return JobSpec.model_validate(payload)
    except ValidationError as exc:
        raise JobSpecError(f"{source} is invalid: " + _format_validation_error(exc)) from exc


def job_spec_json(spec: JobSpec) -> str:
    """Serialize a normalized specification for durable, reviewable storage."""

    return json.dumps(spec.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def validate_job_id(job_id: str) -> str:
    """Return a canonical job ID or reject it before it can reach a worker path."""

    normalized = job_id.strip()
    if not _JOB_ID_REGEX.fullmatch(normalized):
        raise JobSpecError(
            f"Job ID is malformed: {job_id!r}; expected the local form job-<16 hexadecimal>"
        )
    return normalized


class JobInputRecord(BaseModel):
    """Durable outcome of materializing one declared input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact: str
    destination: str
    required: bool
    worker_path: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    materialized: bool = False
    failure_reason: str | None = None
    # Where the verified bytes came from. None means no materialization happened; "cache" is
    # the worker's rebuildable network-volume cache, which is verified before and after the
    # copy, so it is a provenance record rather than a weaker guarantee.
    source: Literal["canonical", "cache"] | None = None


class JobOutputRecord(BaseModel):
    """Durable outcome of persisting one declared output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    artifact: str
    required: bool
    persisted: bool = False
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    verified_size_bytes: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None


class JobProvenance(BaseModel):
    """Non-secret facts that identify how one job was executed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = "runpod"
    worker_id: str
    worker_name: str | None = None
    worker_bootstrap_version: str | None = None
    gpu_models: tuple[str, ...] = ()
    gpu_count: int | None = Field(default=None, ge=0)
    known_hourly_price: Decimal | None = Field(default=None, ge=0)
    infra_version: str
    spec_schema_version: int = JOB_SCHEMA_VERSION
    mlflow_owner: Literal["wavCSE"] = "wavCSE"


class JobRecord(BaseModel):
    """Durable non-secret local record of one submitted job."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = JOB_RECORD_SCHEMA_VERSION
    job_id: str
    name: str
    spec: JobSpec
    state: JobState
    state_reason: str | None = None
    worker_id: str
    job_directory: str
    log_path: str
    requested_commit: str
    executed_commit: str | None = None
    exit_code: int | None = None
    pid: int | None = Field(default=None, ge=0)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancellation_requested_at: datetime | None = None
    worker_absent: bool = False
    remote_status: str | None = None
    log_bytes: int | None = Field(default=None, ge=0)
    preparation_phase: JobPreparationPhase | None = None
    interrupted_at: datetime | None = None
    reconciliation_required: bool = False
    provenance: JobProvenance
    inputs: tuple[JobInputRecord, ...] = ()
    outputs: tuple[JobOutputRecord, ...] = ()
    failure_reason: str | None = None

    @field_validator("job_id")
    @classmethod
    def job_id_is_canonical(cls, value: str) -> str:
        """Keep the durable key identical to the validated identifier form."""

        return validate_job_id(value)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES


def _validate_argv(value: tuple[str, ...]) -> tuple[str, ...]:
    """Reject argument vectors that cannot be executed as an argv without a shell."""

    if len(value) > MAX_COMMAND_ARGUMENTS:
        raise ValueError(f"argv must not exceed {MAX_COMMAND_ARGUMENTS} arguments")
    for argument in value:
        if not argument:
            raise ValueError("argv must not contain an empty argument")
        if len(argument) > MAX_ARGUMENT_LENGTH:
            raise ValueError(f"argv arguments must not exceed {MAX_ARGUMENT_LENGTH} characters")
        if "\n" in argument or "\r" in argument or "\0" in argument:
            raise ValueError("argv arguments must not contain newlines or NUL bytes")
        if contains_bearer_material(argument):
            raise ValueError("argv arguments must not contain credentials or presigned URLs")
    return value


def _validate_relative_path(value: str, label: str) -> str:
    """Return a safe relative path or reject it before it becomes a worker path."""

    if value != value.strip():
        raise ValueError(f"{label} must not start or end with whitespace: {value!r}")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if value.startswith("/"):
        raise ValueError(f"{label} must be relative: {value!r}")
    if "\\" in value:
        raise ValueError(f"{label} must use forward slashes only: {value!r}")
    for character in value:
        if character.isspace():
            raise ValueError(f"{label} must not contain whitespace: {value!r}")
        if ord(character) < _PRINTABLE_ASCII_START or ord(character) == _DELETE_CHARACTER:
            raise ValueError(f"{label} must not contain control characters: {value!r}")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(
            f"{label} must not contain empty, '.' or '..' segments: {value!r}; declare a "
            "normalized relative path"
        )
    return value


def _is_reserved_environment_name(name: str) -> bool:
    """Return whether a variable name belongs to the controller or the infrastructure."""

    upper = name.upper()
    if any(upper.startswith(prefix) for prefix in _RESERVED_ENVIRONMENT_PREFIXES):
        return True
    return "PRIVATE_KEY" in upper


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)
