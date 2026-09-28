"""Controller-side orchestration of worker artifact transfers.

The controller presigns one object-scoped URL, streams the reviewed worker module to the
worker over direct SSH, and passes the bearer URL on the same stdin stream so it never
appears in a process argument list or log. The worker verifies size and SHA-256 before
materializing a download, and reports size plus digest after an upload so the controller
can verify the durable object.
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    ArtifactTransferError,
    SshCommandError,
)
from wavcse_infra.redaction import redact
from wavcse_infra.storage.s3 import PresignedUrl, S3Storage, StorageVerification
from wavcse_infra.storage.worker_transfer import (
    DOWNLOAD_OPERATION,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    UPLOAD_OPERATION,
    TransferInputError,
    validate_presigned_url,
    validate_worker_path,
)
from wavcse_infra.workers.ssh import (
    SshCommandResult,
    SshExecutor,
    WorkerSshWaiter,
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL_FIELDS = frozenset({"operation", "status", "path", "size_bytes", "sha256"})
_DIAGNOSTIC_LIMIT = 500
_WORKER_MODULE = "worker_transfer.py"


class ArtifactTransferResult(BaseModel):
    """Structured completion information reported by the worker transfer module."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal["download", "upload"]
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def sha256_is_canonical(cls, value: str) -> str:
        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return normalized


class ArtifactUploadOutcome(BaseModel):
    """Worker upload result plus the controller's verification of the stored object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: ArtifactTransferResult
    verification: StorageVerification


def load_worker_transfer_source() -> str:
    """Return the reviewed worker transfer module streamed to the worker over SSH."""

    return (
        resources.files("wavcse_infra.storage").joinpath(_WORKER_MODULE).read_text(encoding="utf-8")
    )


def presigned_url_assignment(url: str) -> str:
    """Render the generated prologue line that carries the bearer URL on stdin."""

    try:
        validated = validate_presigned_url(url)
    except TransferInputError as exc:
        raise ArtifactTransferError(f"Cannot use this transfer target: {exc}") from exc
    return f"WAVCSE_PRESIGNED_URL = {validated!r}\n"


def _worker_path(path: str, *, label: str) -> str:
    """Validate a worker path with the same rule the worker itself applies."""

    try:
        return validate_worker_path(path, label=label)
    except TransferInputError as exc:
        raise ArtifactTransferError(str(exc)) from exc


def parse_transfer_output(output: str, *, expected_operation: str) -> ArtifactTransferResult:
    """Parse the worker transfer protocol strictly into a structured result."""

    lines = output.splitlines()
    schema_lines = [
        (index, line.partition("\t")[2])
        for index, line in enumerate(lines)
        if line.partition("\t")[0] == SCHEMA_KEY
    ]
    if not schema_lines:
        raise ArtifactTransferError("Worker returned transfer output without schema version 1")
    if len(schema_lines) != 1:
        raise ArtifactTransferError("Worker returned multiple transfer schema declarations")
    protocol_start, schema_version = schema_lines[0]
    if schema_version != SCHEMA_VERSION:
        raise ArtifactTransferError("Worker returned an unsupported transfer schema version")

    values: dict[str, str] = {}
    for line in lines[protocol_start + 1 :]:
        if not line:
            continue
        key, separator, value = line.partition("\t")
        if not separator or not key:
            raise ArtifactTransferError("Worker returned malformed transfer output")
        if key in values:
            raise ArtifactTransferError("Worker returned a duplicate transfer field")
        values[key] = value

    unexpected = set(values) - _PROTOCOL_FIELDS
    if unexpected:
        raise ArtifactTransferError("Worker returned unexpected transfer fields")
    missing = _PROTOCOL_FIELDS - set(values)
    if missing:
        raise ArtifactTransferError(
            "Worker transfer result is missing fields: " + ", ".join(sorted(missing))
        )
    if values["status"] != "ok":
        raise ArtifactTransferError("Worker reported an unsuccessful transfer status")
    if values["operation"] != expected_operation:
        raise ArtifactTransferError("Worker reported a different transfer operation")
    try:
        size_bytes = int(values["size_bytes"])
    except ValueError as exc:
        raise ArtifactTransferError("Worker reported a non-integer transfer size") from exc
    if size_bytes < 0:
        raise ArtifactTransferError("Worker reported a negative transfer size")
    try:
        return ArtifactTransferResult(
            operation=values["operation"],
            path=values["path"],
            size_bytes=size_bytes,
            sha256=values["sha256"],
        )
    except ValidationError as exc:
        raise ArtifactTransferError("Worker reported an invalid transfer result") from exc


class WorkerArtifactTransfer:
    """Presign, execute, and verify one worker artifact transfer."""

    def __init__(
        self,
        waiter: WorkerSshWaiter,
        executor: SshExecutor,
        ssh_config: SshConfig,
    ) -> None:
        self._waiter = waiter
        self._executor = executor
        self._config = ssh_config

    def download(
        self,
        worker_id: str,
        *,
        storage: S3Storage,
        key: str,
        destination: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        overwrite: bool = False,
        expires_in_seconds: int | None = None,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> ArtifactTransferResult:
        """Materialize one artifact atomically on a READY worker."""

        _worker_path(destination, label="download destination")
        presigned = storage.presign_download(key, expires_in_seconds=expires_in_seconds)
        arguments = ["--destination", destination]
        if expected_size is not None:
            arguments += ["--expected-size", str(expected_size)]
        if expected_sha256 is not None:
            arguments += ["--expected-sha256", expected_sha256]
        if overwrite:
            arguments.append("--overwrite")
        return self._execute(
            worker_id,
            presigned,
            DOWNLOAD_OPERATION,
            arguments,
            wait_timeout_seconds=wait_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )

    def upload(
        self,
        worker_id: str,
        *,
        storage: S3Storage,
        source: str,
        key: str,
        overwrite: bool = False,
        expires_in_seconds: int | None = None,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> ArtifactUploadOutcome:
        """Upload one worker artifact and verify the durable stored object."""

        _worker_path(source, label="upload source")
        storage.require_writable(key, overwrite=overwrite)
        presigned = storage.presign_upload(
            key, expires_in_seconds=expires_in_seconds, overwrite=overwrite
        )
        result = self._execute(
            worker_id,
            presigned,
            UPLOAD_OPERATION,
            ["--source", source],
            wait_timeout_seconds=wait_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
        verification = storage.verify_object(key, expected_size=result.size_bytes)
        return ArtifactUploadOutcome(result=result, verification=verification)

    def _execute(
        self,
        worker_id: str,
        presigned: PresignedUrl,
        operation: str,
        arguments: list[str],
        *,
        wait_timeout_seconds: float | None,
        command_timeout_seconds: float | None,
    ) -> ArtifactTransferResult:
        ready = self._waiter.wait(worker_id, timeout_seconds=wait_timeout_seconds)
        timeout = (
            self._config.transfer_timeout_seconds
            if command_timeout_seconds is None
            else command_timeout_seconds
        )
        try:
            result = self._executor.run_checked(
                ready.connection,
                ("python3", "-", operation, *arguments),
                input_text=_worker_input(presigned),
                timeout_seconds=timeout,
            )
        except SshCommandError as exc:
            raise ArtifactTransferError(
                f"{operation.capitalize()} failed on RunPod worker {worker_id}: {exc}"
            ) from exc
        try:
            parsed = parse_transfer_output(result.stdout, expected_operation=operation)
            if parsed.path != arguments[1]:
                raise ArtifactTransferError("Worker reported a different transfer path")
            return parsed
        except ArtifactTransferError as exc:
            raise ArtifactTransferError(f"{exc}; {_remote_diagnostics(result)}") from exc


def _worker_input(presigned: PresignedUrl) -> str:
    """Build the single stdin stream carried by the SSH session.

    The generated assignment line precedes the reviewed module source, so the bearer URL
    is never an argument of any process on the controller or the worker.
    """

    return (
        presigned_url_assignment(presigned.reveal())
        + f"WAVCSE_IF_NONE_MATCH = {presigned.if_none_match!r}\n"
        + load_worker_transfer_source()
    )


def _remote_diagnostics(result: SshCommandResult) -> str:
    return "; ".join(
        (
            _stream_diagnostic("stdout", result.stdout),
            _stream_diagnostic("stderr", result.stderr),
        )
    )


def _stream_diagnostic(name: str, value: str) -> str:
    cleaned = redact(value.strip())
    if not cleaned:
        return f"remote {name}=<empty>"
    return f"remote {name}={cleaned[:_DIAGNOSTIC_LIMIT]!r}"
