"""Persist declared job outputs through the Phase 5 artifact pipeline.

An output counts as durable only after the worker streams it through a presigned PUT and
the controller verifies the stored object's size. A file that merely exists on a
disposable worker is never treated as a persisted result.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from wavcse_infra.errors import ArtifactTransferError, InfraError, JobExecutionError
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
    """Upload one declared output and record the controller-verified stored object."""

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
    except ArtifactTransferError:
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
        size_bytes=outcome.result.size_bytes,
        sha256=outcome.result.sha256,
        verified_size_bytes=outcome.verification.size_bytes,
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
