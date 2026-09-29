"""Cache-aware input materialization inside one recorded job's preparation pass.

A worker that mounts a network volume keeps verified artifacts on it. These tests pin the
contract that makes that safe to use: the cache is consulted only when an input has a digest
and the worker has a mounted volume, a hit is recorded as such, and a cache problem never
becomes a job failure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from job_fakes import (
    CONNECTION,
    FakeCache,
    FakeStorage,
    FakeTransfer,
    job_context,
    job_spec_document,
    ready_worker_store,
    worker_record,
)

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.jobs.models import load_job_spec
from wavcse_infra.jobs.submit import JobSubmitter
from wavcse_infra.storage.worker_transfer import (
    CACHE_CORRUPT_MARKER,
    CACHE_MATERIALIZE_OPERATION,
    CACHE_ROOT_MISSING_MARKER,
)

CACHE_ROOT = "/workspace/cache"
ARTIFACT = "wavcse/embeddings/v1/voxceleb-minpooling.tar"
PAYLOAD = b"voxceleb-minpooling-archive"
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def _spec(**input_overrides: object):
    document = job_spec_document(
        inputs=[
            {
                "artifact": ARTIFACT,
                "destination": "voxceleb.tar",
                "sha256": DIGEST,
                **input_overrides,
            }
        ]
    )
    return load_job_spec(json.dumps(document))


def _context(
    tmp_path: Path,
    cache: FakeCache | None,
    *,
    volume_mount_path: str | None = CACHE_ROOT,
    with_cache: bool = True,
):
    transfer = FakeTransfer(materialize=True)
    storage = FakeStorage()
    storage.put_content(ARTIFACT, PAYLOAD)
    return job_context(
        tmp_path,
        transfer=transfer,
        storage=storage,
        jobs_config=JobsConfig(worker_root=str(tmp_path / "jobs-root")),
        worker_store=ready_worker_store(
            tmp_path,
            worker_record(network_volume_mount_path=volume_mount_path),
        ),
        cache=(cache if cache is not None else (FakeCache(transfer) if with_cache else None)),
    )


def _submit(tmp_path: Path, context, spec=None):
    return JobSubmitter(context).submit(spec if spec is not None else _spec(), worker_id="pod-123")


def test_a_cache_hit_is_recorded_as_the_input_provenance(tmp_path: Path) -> None:
    context = _context(tmp_path, FakeCache(FakeTransfer(materialize=True), hits={ARTIFACT}))

    record = _submit(tmp_path, context)

    assert record.inputs[0].materialized is True
    assert record.inputs[0].source == "cache"
    assert record.inputs[0].sha256 == DIGEST


def test_a_cache_hit_is_asked_for_the_worker_mount_and_the_declared_digest(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True), hits={ARTIFACT})
    context = _context(tmp_path, cache)

    record = _submit(tmp_path, context)

    assert len(cache.calls) == 1
    call = cache.calls[0]
    assert call["cache_root"] == CACHE_ROOT
    assert call["key"] == ARTIFACT
    assert call["expected_sha256"] == DIGEST
    assert call["artifact_label"] == ARTIFACT
    assert call["destination"].endswith(f"{record.job_id}/inputs/voxceleb.tar")
    assert cache.transfer.downloads == [], "a verified hit must not download anything"


def test_a_cache_miss_downloads_canonically_and_records_that(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True))
    context = _context(tmp_path, cache)

    record = _submit(tmp_path, context)

    assert record.inputs[0].source == "canonical"
    assert len(cache.transfer.downloads) == 1
    assert cache.transfer.downloads[0]["key"] == ARTIFACT
    assert cache.transfer.downloads[0]["expected_sha256"] == DIGEST


def test_destination_replacement_intent_is_passed_through_to_the_cache(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True), hits={ARTIFACT})
    context = _context(tmp_path, cache)

    _submit(tmp_path, context)

    assert cache.calls[0]["overwrite"] is False


def test_a_worker_without_a_network_volume_never_consults_the_cache(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True))
    context = _context(tmp_path, cache, volume_mount_path=None)

    record = _submit(tmp_path, context)

    assert cache.calls == []
    assert record.inputs[0].source == "canonical"


def test_a_context_without_a_cache_uses_the_transfer_directly(tmp_path: Path) -> None:
    context = _context(tmp_path, None, with_cache=False)

    record = _submit(tmp_path, context)

    assert record.inputs[0].source == "canonical"
    assert len(context.transfer.downloads) == 1


def test_an_input_without_a_digest_is_not_offered_to_the_cache(tmp_path: Path) -> None:
    """A content-addressed cache cannot answer a question about an unidentified artifact."""

    cache = FakeCache(FakeTransfer(materialize=True))
    context = _context(tmp_path, cache)

    record = _submit(
        tmp_path,
        context,
        spec=_spec(sha256=None, size_bytes=len(PAYLOAD), required=False),
    )

    assert cache.calls == []
    assert record.inputs[0].source == "canonical"
    assert record.inputs[0].materialized is True


def test_a_populate_failure_is_only_a_warning(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True))
    cache.populate_error = RuntimeError("the volume is full")
    context = _context(tmp_path, cache)

    record = _submit(tmp_path, context)

    assert record.inputs[0].materialized is True
    assert record.inputs[0].source == "canonical"
    assert any("did not cache the verified artifact" in warning for warning in cache.warnings)


def test_declared_expectations_reach_the_cache(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True), hits={ARTIFACT})
    context = _context(tmp_path, cache)

    _submit(tmp_path, context, spec=_spec(size_bytes=len(PAYLOAD)))

    assert cache.calls[0]["expected_size"] == len(PAYLOAD)


def test_the_cache_is_not_consulted_for_a_job_without_inputs(tmp_path: Path) -> None:
    cache = FakeCache(FakeTransfer(materialize=True))
    context = _context(tmp_path, cache)

    record = _submit(tmp_path, context, spec=load_job_spec(json.dumps(job_spec_document())))

    assert cache.calls == []
    assert record.inputs == ()


def test_provenance_survives_a_reconciliation_pass(tmp_path: Path) -> None:
    """A later pass re-derives the artifact from worker evidence, not from a local flag."""

    transfer = FakeTransfer(materialize=True)
    cache = FakeCache(transfer, hits={ARTIFACT})
    context = _context(tmp_path, cache)
    submitter = JobSubmitter(context)
    first = submitter.submit(_spec(), worker_id="pod-123")

    assert first.inputs[0].source == "cache"

    replayed = context.job_store.get(first.job_id)
    assert replayed is not None
    assert replayed.inputs[0].source == "cache"


@pytest.mark.parametrize("mount", ["/workspace/cache", "/workspace", "/cache"])
def test_any_provider_reported_mount_is_used_verbatim(tmp_path: Path, mount: str) -> None:
    cache = FakeCache(FakeTransfer(materialize=True), hits={ARTIFACT})
    context = _context(tmp_path, cache, volume_mount_path=mount)

    _submit(tmp_path, context)

    assert cache.calls[0]["cache_root"] == mount


class _RefusingCacheExecutor:
    """A worker whose cache refuses the artifact, as a damaged volume makes it do."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run_checked(self, connection, remote_argv, *, input_text=None, timeout_seconds=None):
        del connection, timeout_seconds
        self.calls.append((tuple(remote_argv), input_text))
        from wavcse_infra.errors import SshCommandError

        raise SshCommandError(
            f"remote command '{remote_argv[2]}' exited with status 1: "
            f"wavcse_transfer_error\t{self.reason}"
        )


class _CacheWaiter:
    def wait(self, worker_id: str, *, timeout_seconds: float | None = None):
        del timeout_seconds
        from wavcse_infra.workers.ssh import SshWaitResult

        return SshWaitResult(worker=_running_worker(worker_id), connection=CONNECTION)


def _running_worker(worker_id: str):
    from wavcse_infra.models import Worker, WorkerState

    return Worker(id=worker_id, state=WorkerState.RUNNING)


@pytest.mark.parametrize(
    "reason",
    [
        "cache path is a symbolic link; the cache never follows one",
        CACHE_ROOT_MISSING_MARKER,
        CACHE_CORRUPT_MARKER,
        "the controller stopped waiting",
    ],
)
def test_a_worker_side_cache_refusal_never_fails_a_required_input(
    tmp_path: Path,
    reason: str,
) -> None:
    """The real collaborator, driven by a refusing worker: the job still gets its input.

    A damaged or hostile volume can make every cache operation fail. The artifact is
    available from canonical storage, so the volume is optional and the artifact is not.
    """

    from wavcse_infra.storage.cache import WorkerArtifactCache

    transfer = FakeTransfer(materialize=True)
    executor = _RefusingCacheExecutor(reason)
    context = _context(
        tmp_path,
        WorkerArtifactCache(
            _CacheWaiter(),  # type: ignore[arg-type]
            executor,  # type: ignore[arg-type]
            SshConfig(),
            transfer,  # type: ignore[arg-type]
            warn=lambda message: None,
        ),
    )

    record = _submit(tmp_path, context)

    assert record.inputs[0].materialized is True
    assert record.inputs[0].source == "canonical"
    assert next(argv[2] for argv, _ in executor.calls) == CACHE_MATERIALIZE_OPERATION
    assert len(transfer.downloads) == 1


def test_the_job_records_no_cache_provenance_when_the_cache_was_not_used(tmp_path: Path) -> None:
    context = _context(tmp_path, None, with_cache=False)

    record = _submit(tmp_path, context)

    assert record.inputs[0].source == "canonical"
