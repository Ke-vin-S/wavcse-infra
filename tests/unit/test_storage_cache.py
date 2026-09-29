"""Offline coverage for the controller-side rebuildable cache orchestration."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from job_fakes import CONNECTION, WORKER_ID, FakeStorage, FakeTransfer

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    ArtifactDestinationExistsError,
    ArtifactTransferError,
    ArtifactTransferInProgressError,
    CacheError,
    SshCommandError,
)
from wavcse_infra.models import Worker, WorkerConnectionInfo, WorkerState
from wavcse_infra.storage.cache import CacheStats, WorkerArtifactCache, parse_cache_stats
from wavcse_infra.storage.transfer import ArtifactTransferResult
from wavcse_infra.storage.worker_transfer import (
    CACHE_CORRUPT_MARKER,
    CACHE_MATERIALIZE_OPERATION,
    CACHE_MISS_MARKER,
    CACHE_POPULATE_OPERATION,
    CACHE_ROOT_MISSING_MARKER,
    CACHE_STATS_OPERATION,
    DESTINATION_EXISTS_MARKER,
    DOWNLOAD_OPERATION,
    ERROR_KEY,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    TRANSFER_IN_PROGRESS_MARKER,
    TRANSIENT_FAILURE_MARKER,
    UPLOAD_OPERATION,
    VERIFY_OPERATION,
)
from wavcse_infra.workers.ssh import SshCommandResult, SshWaitResult

DIGEST = "a" * 64
CACHE_ROOT = "/workspace/cache"
DESTINATION = "/workspace/wavcse-jobs/job-1/inputs/artifact.tar"
SSH_CONFIG = SshConfig()


class FakeWaiter:
    """Return the shared endpoint without ever connecting."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> SshWaitResult:
        del timeout_seconds
        self.calls.append(worker_id)
        return SshWaitResult(
            worker=Worker(id=worker_id, state=WorkerState.RUNNING),
            connection=CONNECTION,
        )


class FakeExecutor:
    """Record every streamed program and replay one scripted outcome per operation."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, str, str]] = {}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        del connection, timeout_seconds
        argv = tuple(remote_argv)
        self.calls.append((argv, input_text))
        exit_code, stdout, stderr = self.responses.get(argv[2], (0, "", ""))
        if exit_code != 0:
            raise SshCommandError(
                f"remote command '{argv[2]}' exited with status {exit_code}: {stderr}"
            )
        return SshCommandResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _transfer_output(operation: str, *, path: str, size: int = 11, digest: str = DIGEST) -> str:
    return "\n".join(
        (
            f"{SCHEMA_KEY}\t{SCHEMA_VERSION}",
            f"operation\t{operation}",
            "status\tok",
            f"path\t{path}",
            f"size_bytes\t{size}",
            f"sha256\t{digest}",
            "",
        )
    )


def _stats_output(**overrides: str) -> str:
    values = {
        "root": CACHE_ROOT,
        "entries": "3",
        "cached_bytes": "16106127360",
        "staging_bytes": "0",
        "unverified_entries": "0",
        "marker_schema_version": "1",
    }
    values.update(overrides)
    lines = [f"{SCHEMA_KEY}\t{SCHEMA_VERSION}", "operation\tcache-stats", "status\tok"]
    lines.extend(f"{key}\t{value}" for key, value in values.items())
    return "\n".join(lines) + "\n"


def _cache(
    executor: FakeExecutor,
    transfer: FakeTransfer | None = None,
    *,
    warnings: list[str] | None = None,
) -> WorkerArtifactCache:
    warn = (warnings if warnings is not None else []).append
    return WorkerArtifactCache(
        FakeWaiter(),  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        SSH_CONFIG,
        transfer if transfer is not None else FakeTransfer(),  # type: ignore[arg-type]
        warn=warn,
    )


def _materialize(cache: WorkerArtifactCache, **overrides: object):  # type: ignore[no-untyped-def]
    values: dict[str, object] = {
        "cache_root": CACHE_ROOT,
        "storage": FakeStorage(),
        "key": "embeddings/v1/voxceleb.tar",
        "destination": DESTINATION,
        "expected_size": 11,
        "expected_sha256": DIGEST,
    }
    values.update(overrides)
    return cache.materialize(WORKER_ID, **values)  # type: ignore[arg-type]


def _hit_executor() -> FakeExecutor:
    executor = FakeExecutor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        0,
        _transfer_output(CACHE_MATERIALIZE_OPERATION, path=DESTINATION),
        "",
    )
    return executor


def _miss_executor() -> FakeExecutor:
    executor = FakeExecutor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        3,
        "",
        f"{ERROR_KEY}\t{CACHE_MISS_MARKER}: {DIGEST}",
    )
    executor.responses[CACHE_POPULATE_OPERATION] = (
        0,
        _transfer_output(CACHE_POPULATE_OPERATION, path=f"{CACHE_ROOT}/artifacts/x"),
        "",
    )
    return executor


# --- protocol parsing ------------------------------------------------------------------


def test_cache_stats_protocol_is_parsed_strictly() -> None:
    stats = parse_cache_stats(_stats_output())

    assert stats == CacheStats(
        root=CACHE_ROOT,
        entries=3,
        cached_bytes=16106127360,
        staging_bytes=0,
        unverified_entries=0,
        marker_schema_version="1",
    )


def test_an_absent_marker_is_reported_as_none() -> None:
    stats = parse_cache_stats(_stats_output(marker_schema_version="absent"))
    assert stats.marker_schema_version is None


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("", "exactly one schema declaration"),
        (f"{SCHEMA_KEY}\t2\nstatus\tok\n", "unsupported cache protocol"),
        (f"{SCHEMA_KEY}\t1\n", "did not report successful cache statistics"),
        (
            _stats_output().replace("operation\tcache-stats", "operation\tcache-materialize"),
            "did not report successful cache statistics",
        ),
        (_stats_output() + "extra\t1\n", "unexpected cache statistics fields"),
        (_stats_output(entries="many"), "non-integer entries"),
        (_stats_output(cached_bytes="-1"), "negative cached_bytes"),
        (_stats_output(root="relative/path"), "non-absolute cache root"),
    ],
)
def test_a_malformed_stats_protocol_is_refused(output: str, message: str) -> None:
    with pytest.raises(ArtifactTransferError, match=message):
        parse_cache_stats(output)


def test_a_duplicated_stats_field_is_refused() -> None:
    with pytest.raises(ArtifactTransferError, match="duplicate cache statistics field"):
        parse_cache_stats(_stats_output() + "entries\t4\n")


def test_a_stats_protocol_missing_the_root_is_refused() -> None:
    output = "\n".join(
        line for line in _stats_output().splitlines() if not line.startswith("root\t")
    )
    with pytest.raises(ArtifactTransferError, match="missing the cache root"):
        parse_cache_stats(output + "\n")


# --- materialization -------------------------------------------------------------------


def test_a_verified_cache_hit_is_materialized_without_downloading() -> None:
    transfer = FakeTransfer()
    warnings: list[str] = []

    outcome = _materialize(_cache(_hit_executor(), transfer, warnings=warnings))

    assert outcome.source == "cache"
    assert outcome.result.sha256 == DIGEST
    assert transfer.downloads == []
    assert warnings == []


def test_a_cache_miss_falls_through_to_canonical_storage_and_then_populates() -> None:
    executor = _miss_executor()
    transfer = FakeTransfer()

    outcome = _materialize(
        _cache(executor, transfer),
        artifact_label="embeddings/v1/voxceleb.tar",
    )

    assert outcome.source == "canonical"
    assert [call["key"] for call in transfer.downloads] == ["embeddings/v1/voxceleb.tar"]
    assert [argv[2] for argv, _ in executor.calls] == [
        CACHE_MATERIALIZE_OPERATION,
        CACHE_POPULATE_OPERATION,
    ]
    populate_arguments = executor.calls[1][0]
    assert populate_arguments[populate_arguments.index("--source") + 1] == DESTINATION
    assert populate_arguments[populate_arguments.index("--artifact") + 1] == (
        "embeddings/v1/voxceleb.tar"
    )


def test_a_quarantined_entry_is_treated_as_a_miss_and_rebuilt() -> None:
    executor = _miss_executor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        3,
        "",
        f"{ERROR_KEY}\t{CACHE_CORRUPT_MARKER}: {DIGEST}",
    )
    transfer = FakeTransfer()

    outcome = _materialize(_cache(executor, transfer))

    assert outcome.source == "canonical"
    assert len(transfer.downloads) == 1


def test_a_missing_cache_root_degrades_to_canonical_storage_with_a_warning() -> None:
    executor = FakeExecutor()
    for operation in (CACHE_MATERIALIZE_OPERATION, CACHE_POPULATE_OPERATION):
        executor.responses[operation] = (
            1,
            "",
            f"{ERROR_KEY}\t{CACHE_ROOT_MISSING_MARKER}: {CACHE_ROOT}",
        )
    warnings: list[str] = []
    transfer = FakeTransfer()

    outcome = _materialize(_cache(executor, transfer, warnings=warnings))

    assert outcome.source == "canonical"
    assert len(transfer.downloads) == 1
    assert len(warnings) == 2, "the unusable cache is reported for the lookup and the store"
    assert all("could not be used" in warning for warning in warnings)
    assert all("materializing from canonical storage" in warning for warning in warnings)


def test_an_interrupted_cache_lookup_falls_through_instead_of_failing() -> None:
    executor = _miss_executor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\t{TRANSIENT_FAILURE_MARKER}: the controller stopped waiting",
    )
    warnings: list[str] = []
    transfer = FakeTransfer()

    outcome = _materialize(_cache(executor, transfer, warnings=warnings))

    assert outcome.source == "canonical"
    assert any("could not be used" in warning for warning in warnings)


def test_a_failed_populate_never_fails_the_materialization() -> None:
    executor = _miss_executor()
    executor.responses[CACHE_POPULATE_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\tthat artifact hashes to something else",
    )
    warnings: list[str] = []

    outcome = _materialize(_cache(executor, FakeTransfer(), warnings=warnings))

    assert outcome.source == "canonical"
    assert any("could not be used" in warning for warning in warnings)


def test_a_worker_refusal_the_controller_cannot_classify_still_degrades() -> None:
    """A hostile or damaged volume must never be able to fail a job that S3 can serve."""

    executor = _miss_executor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\tcache path is a symbolic link; the cache never follows one",
    )
    warnings: list[str] = []
    transfer = FakeTransfer()

    outcome = _materialize(_cache(executor, transfer, warnings=warnings))

    assert outcome.source == "canonical"
    assert len(transfer.downloads) == 1
    assert any("could not be used" in warning for warning in warnings)
    assert any("symbolic link" in warning for warning in warnings)


def test_a_destination_owned_by_another_writer_is_reconcilable_not_a_failure() -> None:
    executor = _miss_executor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\t{DESTINATION_EXISTS_MARKER}",
    )

    with pytest.raises(ArtifactDestinationExistsError):
        _materialize(_cache(executor, FakeTransfer()))


def test_a_destination_with_a_live_transfer_is_reconcilable_not_a_failure() -> None:
    executor = _miss_executor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\tanother transfer for that path {TRANSFER_IN_PROGRESS_MARKER}",
    )

    with pytest.raises(ArtifactTransferInProgressError):
        _materialize(_cache(executor, FakeTransfer()))


def test_a_cache_protocol_violation_is_visible_rather_than_hidden() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (0, "garbage output\n", "")

    with pytest.raises(ArtifactTransferError, match="without schema version"):
        _materialize(_cache(executor))


def test_a_cache_result_for_another_path_is_refused() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_MATERIALIZE_OPERATION] = (
        0,
        _transfer_output(CACHE_MATERIALIZE_OPERATION, path="/somewhere/else.tar"),
        "",
    )

    with pytest.raises(ArtifactTransferError, match="reported cache work on a different path"):
        _materialize(_cache(executor))


def test_a_destination_that_appeared_is_reported_to_the_caller() -> None:
    """The canonical path owns that decision, so no caller can guess about the bytes."""

    executor = _miss_executor()
    transfer = FakeTransfer()
    transfer.destination_exists = True

    with pytest.raises(ArtifactDestinationExistsError):
        _materialize(_cache(executor, transfer))


def test_an_input_without_a_digest_uses_only_the_canonical_path() -> None:
    executor = FakeExecutor()
    transfer = FakeTransfer()

    outcome = _materialize(_cache(executor, transfer), expected_sha256=None)

    assert outcome.source == "canonical"
    assert executor.calls == [], "a digestless artifact has no cache identity to ask about"
    assert len(transfer.downloads) == 1


def test_the_canonical_path_receives_the_declared_expectations() -> None:
    transfer = FakeTransfer()

    _materialize(_cache(FakeExecutor(), transfer), expected_sha256=None, expected_size=4096)

    assert transfer.downloads[0]["expected_sha256"] is None
    assert transfer.downloads[0]["expected_size"] == 4096
    assert transfer.downloads[0]["destination"] == DESTINATION


# --- the streamed channel ---------------------------------------------------------------


def test_cache_operations_never_send_a_presigned_url() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_STATS_OPERATION] = (0, _stats_output(), "")

    _cache(executor).stats(WORKER_ID, cache_root=CACHE_ROOT)

    for argv, input_text in executor.calls:
        assert argv[:3] == ("python3", "-", CACHE_STATS_OPERATION)
        assert input_text is not None
        assert "X-Amz-Signature" not in input_text
        # The transfer path prepends a generated assignment line; the cache path prepends
        # nothing at all, so the streamed text starts with the reviewed module itself.
        assert input_text.startswith('"""Worker-side artifact')


def test_cache_operations_stream_the_reviewed_worker_program() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_STATS_OPERATION] = (0, _stats_output(), "")

    _cache(executor).stats(WORKER_ID, cache_root=CACHE_ROOT)

    _, input_text = executor.calls[0]
    assert input_text is not None
    assert "def cache_materialize(" in input_text


def test_stats_reports_the_worker_facts() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_STATS_OPERATION] = (0, _stats_output(entries="7"), "")

    stats = _cache(executor).stats(WORKER_ID, cache_root=CACHE_ROOT)

    assert stats.entries == 7
    assert stats.root == CACHE_ROOT
    arguments = executor.calls[0][0]
    assert arguments[arguments.index("--root") + 1] == CACHE_ROOT


def test_an_unavailable_cache_root_is_reported_as_a_cache_failure() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_STATS_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\t{CACHE_ROOT_MISSING_MARKER}: {CACHE_ROOT}",
    )

    with pytest.raises(CacheError, match="no usable artifact cache"):
        _cache(executor).stats(WORKER_ID, cache_root=CACHE_ROOT)


def test_a_definitive_remote_cache_failure_is_classified_as_a_transfer_failure() -> None:
    executor = FakeExecutor()
    executor.responses[CACHE_STATS_OPERATION] = (
        1,
        "",
        f"{ERROR_KEY}\tthe cache root could not be read",
    )

    with pytest.raises(ArtifactTransferError, match="Cache-stats failed"):
        _cache(executor).stats(WORKER_ID, cache_root=CACHE_ROOT)


def test_the_reported_operation_literal_matches_the_worker_constants() -> None:
    """A drift between these two sides would surface as an unparseable worker result."""

    declared = set(
        ArtifactTransferResult.model_fields["operation"].annotation.__args__  # type: ignore[union-attr]
    )
    assert declared == {
        DOWNLOAD_OPERATION,
        UPLOAD_OPERATION,
        VERIFY_OPERATION,
        CACHE_MATERIALIZE_OPERATION,
        CACHE_POPULATE_OPERATION,
    }
