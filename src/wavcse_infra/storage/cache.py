"""Controller-side orchestration of a worker's rebuildable artifact cache.

A disposable worker that mounts a RunPod network volume keeps verified artifacts on it, so a
later experiment can materialize an immutable input without paying for the download again.
This module is only the controller half: it streams the reviewed worker program, decides
whether the cache may be trusted, and falls through to canonical storage when it may not.

Three rules shape every decision here:

* the cache never decides what an artifact is - the requested SHA-256 does. An input without
  a digest is downloaded from canonical storage directly, because a content-addressed cache
  cannot answer a question about an artifact whose identity is unknown;
* a cache problem is never an artifact failure. A miss, a quarantined entry, or an unusable
  cache root is reported and the canonical download proceeds, so a warm cache can only ever
  make a job faster, never make it fail;
* a cache hit still verifies. The worker hashes the entry before it copies it and hashes the
  copy as it places it, so a hit carries exactly the integrity guarantee of a fresh download.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    ArtifactDestinationExistsError,
    ArtifactTransferError,
    ArtifactTransferInProgressError,
    CacheError,
    InfraError,
    SshError,
)
from wavcse_infra.storage.s3 import S3Storage
from wavcse_infra.storage.transfer import (
    ArtifactTransferResult,
    WorkerArtifactTransfer,
    load_worker_transfer_source,
    parse_transfer_output,
    remote_operation_failure,
)
from wavcse_infra.storage.worker_transfer import (
    CACHE_MATERIALIZE_OPERATION,
    CACHE_MISS_MARKER,
    CACHE_POPULATE_OPERATION,
    CACHE_ROOT_MISSING_MARKER,
    CACHE_STATS_FIELDS,
    CACHE_STATS_OPERATION,
    DESTINATION_EXISTS_MARKER,
    ERROR_KEY,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    TRANSFER_IN_PROGRESS_MARKER,
)
from wavcse_infra.workers.ssh import SshCommandResult, SshExecutor, WorkerSshWaiter


def _ignore(message: str) -> None:
    """Default sink for a non-fatal cache warning."""

    del message


def _cache_detail(detail: str) -> str:
    """Return the worker's own reason from inside the transport wrapper around it."""

    _, separator, reason = detail.partition(f"{ERROR_KEY}\t")
    return (reason if separator else detail).strip()[:200]


@dataclass(frozen=True)
class MaterializationOutcome:
    """One materialized input plus where its bytes came from."""

    result: ArtifactTransferResult
    source: Literal["cache", "canonical"]


class CacheStats(BaseModel):
    """Recorded contents of one worker's rebuildable cache.

    `cached_bytes` comes from each entry's own metadata document rather than from re-reading
    the artifacts, so inspecting a full volume stays cheap. Verification happens when an
    entry is used, which is where it matters.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str = Field(min_length=1)
    entries: int = Field(ge=0)
    cached_bytes: int = Field(ge=0)
    staging_bytes: int = Field(ge=0)
    unverified_entries: int = Field(ge=0)
    marker_schema_version: str | None = None


def parse_cache_stats(output: str) -> CacheStats:
    """Parse the worker cache-stats protocol strictly into a structured result."""

    lines = output.splitlines()
    schema_lines = [
        (index, line.partition("\t")[2])
        for index, line in enumerate(lines)
        if line.partition("\t")[0] == SCHEMA_KEY
    ]
    if len(schema_lines) != 1:
        raise ArtifactTransferError(
            "Worker returned cache statistics without exactly one schema declaration"
        )
    protocol_start, schema_version = schema_lines[0]
    if schema_version != SCHEMA_VERSION:
        raise ArtifactTransferError("Worker returned an unsupported cache protocol version")
    values: dict[str, str] = {}
    for line in lines[protocol_start + 1 :]:
        if not line:
            continue
        key, separator, value = line.partition("\t")
        if not separator or not key:
            raise ArtifactTransferError("Worker returned malformed cache statistics")
        if key in values:
            raise ArtifactTransferError("Worker returned a duplicate cache statistics field")
        values[key] = value
    if values.get("operation") != CACHE_STATS_OPERATION or values.get("status") != "ok":
        raise ArtifactTransferError("Worker did not report successful cache statistics")
    unexpected = set(values) - {"operation", "status", *CACHE_STATS_FIELDS}
    if unexpected:
        raise ArtifactTransferError("Worker returned unexpected cache statistics fields")
    numbers: dict[str, int] = {}
    for name in ("entries", "cached_bytes", "staging_bytes", "unverified_entries"):
        raw = values.get(name)
        if raw is None:
            raise ArtifactTransferError(f"Worker cache statistics are missing {name}")
        try:
            parsed = int(raw)
        except ValueError as exc:
            raise ArtifactTransferError(
                f"Worker reported a non-integer {name} for the cache"
            ) from exc
        if parsed < 0:
            raise ArtifactTransferError(f"Worker reported a negative {name} for the cache")
        numbers[name] = parsed
    root = values.get("root")
    if not root:
        raise ArtifactTransferError("Worker cache statistics are missing the cache root")
    if not root.startswith("/"):
        raise ArtifactTransferError("Worker reported a non-absolute cache root")
    marker = values.get("marker_schema_version")
    return CacheStats(
        root=root,
        marker_schema_version=None if marker in {None, "absent", ""} else marker,
        **numbers,
    )


class WorkerArtifactCache:
    """Materialize declared inputs cache-first, and never fail a job because of the cache."""

    def __init__(
        self,
        waiter: WorkerSshWaiter,
        executor: SshExecutor,
        ssh_config: SshConfig,
        transfer: WorkerArtifactTransfer,
        *,
        warn: Callable[[str], None] = _ignore,
    ) -> None:
        self._waiter = waiter
        self._executor = executor
        self._config = ssh_config
        self._transfer = transfer
        self._warn = warn

    def materialize(
        self,
        worker_id: str,
        *,
        cache_root: str,
        storage: S3Storage,
        key: str,
        destination: str,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
        overwrite: bool = False,
        artifact_label: str | None = None,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> MaterializationOutcome:
        """Place one artifact on the worker, preferring a verified local cache entry.

        A verified hit is materialized locally and returned immediately. Everything else -
        no digest, no entry, a quarantined entry, an unusable cache root, or an interrupted
        lookup - falls through to the canonical presigned download, and a successful download
        is then offered to the cache so the next job does not repeat it. Only the canonical
        path's own outcome can make this raise: everything about the cache degrades to a
        warning, so a warm cache can only make a job faster, never make it fail.
        """

        if expected_sha256 is None:
            return MaterializationOutcome(
                result=self._transfer.download(
                    worker_id,
                    storage=storage,
                    key=key,
                    destination=destination,
                    expected_size=expected_size,
                    expected_sha256=None,
                    overwrite=overwrite,
                    wait_timeout_seconds=wait_timeout_seconds,
                    command_timeout_seconds=command_timeout_seconds,
                ),
                source="canonical",
            )

        arguments = [
            "--root",
            cache_root,
            "--expected-sha256",
            expected_sha256,
            "--destination",
            destination,
        ]
        if expected_size is not None:
            arguments += ["--expected-size", str(expected_size)]
        if overwrite:
            arguments.append("--overwrite")
        cached = self._run_cache_operation(
            worker_id,
            operation=CACHE_MATERIALIZE_OPERATION,
            arguments=arguments,
            expected_operation=CACHE_MATERIALIZE_OPERATION,
            expected_path=destination,
            wait_timeout_seconds=wait_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
        if cached is not None:
            return MaterializationOutcome(result=cached, source="cache")

        downloaded = self._transfer.download(
            worker_id,
            storage=storage,
            key=key,
            destination=destination,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            overwrite=overwrite,
            wait_timeout_seconds=wait_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
        self._populate(
            worker_id,
            cache_root=cache_root,
            source=destination,
            expected_size=downloaded.size_bytes,
            expected_sha256=downloaded.sha256,
            artifact_label=artifact_label,
            wait_timeout_seconds=wait_timeout_seconds,
            command_timeout_seconds=command_timeout_seconds,
        )
        return MaterializationOutcome(result=downloaded, source="canonical")

    def stats(
        self,
        worker_id: str,
        *,
        cache_root: str,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> CacheStats:
        """Report one worker's recorded cache contents."""

        try:
            result = self._run(
                worker_id,
                (CACHE_STATS_OPERATION, "--root", cache_root),
                wait_timeout_seconds=wait_timeout_seconds,
                command_timeout_seconds=command_timeout_seconds,
            )
        except SshError as exc:
            if CACHE_ROOT_MISSING_MARKER in str(exc):
                raise CacheError(
                    f"Worker {worker_id} has no usable artifact cache at {cache_root}: the "
                    "mount is absent or is not a directory"
                ) from exc
            raise remote_operation_failure(worker_id, CACHE_STATS_OPERATION, exc) from exc
        return parse_cache_stats(result.stdout)

    def _populate(
        self,
        worker_id: str,
        *,
        cache_root: str,
        source: str,
        expected_size: int,
        expected_sha256: str,
        artifact_label: str | None,
        wait_timeout_seconds: float | None,
        command_timeout_seconds: float | None,
    ) -> None:
        """Offer one verified artifact to the cache, reporting rather than raising failure.

        The artifact is already materialized and verified at this point, so nothing the cache
        does may turn into a job failure - including a controller interruption, which says
        nothing about either the artifact or the cache.
        """

        arguments = [
            "--root",
            cache_root,
            "--source",
            source,
            "--expected-sha256",
            expected_sha256,
            "--expected-size",
            str(expected_size),
        ]
        if artifact_label is not None:
            arguments += ["--artifact", artifact_label]
        try:
            self._run_cache_operation(
                worker_id,
                operation=CACHE_POPULATE_OPERATION,
                arguments=arguments,
                expected_operation=CACHE_POPULATE_OPERATION,
                expected_path=None,
                wait_timeout_seconds=wait_timeout_seconds,
                command_timeout_seconds=command_timeout_seconds,
            )
        except InfraError as exc:
            self._warn(f"worker {worker_id} did not cache the verified artifact at {source}: {exc}")

    def _run_cache_operation(
        self,
        worker_id: str,
        *,
        operation: str,
        arguments: list[str],
        expected_operation: str,
        expected_path: str | None,
        wait_timeout_seconds: float | None,
        command_timeout_seconds: float | None,
    ) -> ArtifactTransferResult | None:
        """Run one cache operation, returning `None` when the cache cannot answer.

        `None` is the whole "the cache is not useful here" vocabulary, and a caller resolves
        it by using canonical storage. Everything the worker reports as a refusal belongs
        there, including a miss, a quarantined entry, an unusable cache root, an unsafe cache
        path, an existing destination owned by another writer, and an interrupted lookup. A
        cache that cannot answer must never fail an artifact that canonical storage can
        still provide.

        Only two things do not belong there. A destination that already exists is the
        caller's decision, because it is the caller that can verify what is at it. And a
        response the controller cannot parse is a defect in reviewed code, so it is raised.
        """

        try:
            result = self._run(
                worker_id,
                (operation, *arguments),
                wait_timeout_seconds=wait_timeout_seconds,
                command_timeout_seconds=command_timeout_seconds,
            )
        except SshError as exc:
            detail = str(exc)
            if CACHE_MISS_MARKER in detail:
                return None
            if DESTINATION_EXISTS_MARKER in detail:
                raise ArtifactDestinationExistsError(
                    f"Cache {operation} did not run on worker {worker_id} because the "
                    f"destination already exists: {_cache_detail(detail)}"
                ) from exc
            if TRANSFER_IN_PROGRESS_MARKER in detail:
                raise ArtifactTransferInProgressError(
                    f"Cache {operation} did not start on worker {worker_id} because another "
                    f"transfer still owns that destination: {_cache_detail(detail)}"
                ) from exc
            self._warn(
                f"the artifact cache on worker {worker_id} could not be used "
                f"({operation}: {_cache_detail(detail)}); materializing from canonical "
                "storage instead"
            )
            return None
        parsed = parse_transfer_output(result.stdout, expected_operation=expected_operation)
        if expected_path is not None and parsed.path != expected_path:
            raise ArtifactTransferError(
                f"Worker {worker_id} reported cache work on a different path than requested"
            )
        return parsed

    def _run(
        self,
        worker_id: str,
        argv: tuple[str, ...],
        *,
        wait_timeout_seconds: float | None,
        command_timeout_seconds: float | None,
    ) -> SshCommandResult:
        """Wait for the worker, then stream the reviewed module for one cache operation.

        No presigned URL is ever generated or sent: bytes only enter the cache from a file the
        canonical download already verified, so the mounted volume never receives bearer
        material and cannot become a credential store.
        """

        ready = self._waiter.wait(worker_id, timeout_seconds=wait_timeout_seconds)
        timeout = (
            self._config.command_timeout_seconds
            if command_timeout_seconds is None
            else command_timeout_seconds
        )
        return self._executor.run_checked(
            ready.connection,
            ("python3", "-", *argv),
            input_text=load_worker_transfer_source(),
            timeout_seconds=timeout,
        )
