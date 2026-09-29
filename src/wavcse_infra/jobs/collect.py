"""Persist declared job outputs through the Phase 5 artifact pipeline.

An output counts as durable only after the worker streams it through a presigned PUT and
the controller reads the stored object back and proves its bytes. A file that merely
exists on a disposable worker is never treated as a persisted result, and neither is an
object whose metadata merely looks right: the recorded digest is only ever recorded
against an object that was hashed and matched.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from wavcse_infra.errors import (
    ArtifactTransferError,
    InfraError,
    JobExecutionError,
    ReconcilableOperationError,
    StorageObjectExistsError,
    StorageVerificationError,
)
from wavcse_infra.jobs.models import JobInput, JobOutput, JobOutputRecord
from wavcse_infra.storage.s3 import S3Storage
from wavcse_infra.storage.transfer import WorkerArtifactTransfer


def resolve_output_source(job_directory: str, output: JobOutput) -> str:
    """Return the absolute worker path of one declared output inside the job workspace."""

    return _resolve_within(job_directory, output.path, "output path")


def resolve_input_destination(job_directory: str, job_input: JobInput) -> str:
    """Return the absolute worker path one declared input materializes to."""

    return _resolve_within(job_directory, f"inputs/{job_input.destination}", "input destination")


def persist_output(
    transfer: WorkerArtifactTransfer,
    storage: S3Storage,
    worker_id: str,
    *,
    job_directory: str,
    output: JobOutput,
) -> JobOutputRecord:
    """Upload one declared output and record the controller-verified stored object.

    The worker's reported digest describes the bytes it streamed, but only canonical
    storage can say what was stored. The object is therefore read back and hashed against
    that digest, so the digest this record carries is bound to bytes the controller itself
    observed at the declared key rather than to the worker's claim about them.
    """

    source = resolve_output_source(job_directory, output)
    try:
        outcome = transfer.upload(
            worker_id,
            storage=storage,
            source=source,
            key=output.artifact,
            allowed_root=job_directory,
            overwrite=output.overwrite,
        )
        verification = storage.verify_object_content(
            output.artifact,
            expected_size=outcome.result.size_bytes,
            expected_sha256=outcome.result.sha256,
        )
    except ArtifactTransferError:
        raise
    except StorageObjectExistsError:
        # An object is already at the key: the caller decides from canonical evidence
        # whether it is this output or something that must not be replaced.
        raise
    except StorageVerificationError:
        # The upload returned success but the stored bytes do not match what the worker
        # reported; nothing about this output may be recorded as verified.
        raise
    except ReconcilableOperationError:
        # The upload's outcome is unknown, not failed. Wrapping it would hide that from the
        # caller and turn a lost observation into a terminal result.
        raise
    except InfraError as exc:
        raise JobExecutionError(
            f"Could not persist output {output.path!r} as {output.artifact!r}: {exc}"
        ) from exc
    return JobOutputRecord(
        path=output.path,
        artifact=output.artifact,
        required=output.required,
        persisted=True,
        size_bytes=verification.size_bytes,
        sha256=outcome.result.sha256,
        verified_size_bytes=verification.size_bytes,
    )


def failed_output_record(output: JobOutput, reason: str) -> JobOutputRecord:
    """Return a durable record of an output that could not be persisted."""

    return JobOutputRecord(
        path=output.path,
        artifact=output.artifact,
        required=output.required,
        persisted=False,
        failure_reason=reason[:1000],
    )


def _resolve_within(job_directory: str, relative: str, label: str) -> str:
    """Compose one worker path and refuse anything that leaves the job workspace."""

    root = PurePosixPath(job_directory)
    if not root.is_absolute():
        raise JobExecutionError(f"job workspace is not absolute: {job_directory!r}")
    resolved = root.joinpath(relative)
    normalized = resolved.as_posix()
    if not normalized.startswith(root.as_posix().rstrip("/") + "/"):
        raise JobExecutionError(f"{label} escapes the job workspace: {relative!r}")
    if any(part in {"", ".", ".."} for part in PurePosixPath(relative).parts):
        raise JobExecutionError(f"{label} is not a normalized relative path: {relative!r}")
    return normalized
