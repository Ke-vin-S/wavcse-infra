"""Offline coverage for the worker-side rebuildable artifact cache."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import io
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from wavcse_infra.storage import worker_transfer
from wavcse_infra.storage.worker_transfer import (
    CACHE_ARTIFACTS_RELATIVE,
    CACHE_CORRUPT_MARKER,
    CACHE_ENTRY_CONTENT_NAME,
    CACHE_ENTRY_METADATA_NAME,
    CACHE_MARKER_NAME,
    CACHE_MATERIALIZE_OPERATION,
    CACHE_MISS_EXIT_CODE,
    CACHE_MISS_MARKER,
    CACHE_POPULATE_OPERATION,
    CACHE_ROOT_MISSING_MARKER,
    CACHE_STAGING_RELATIVE,
    CACHE_STATS_OPERATION,
    DESTINATION_EXISTS_MARKER,
    ERROR_KEY,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    TRANSFER_IN_PROGRESS_MARKER,
    CacheIntegrityFailure,
    CacheMissError,
    CacheRootError,
    TransferError,
    TransferInputError,
    TransferVerificationError,
    cache_materialize,
    cache_populate,
    cache_stats,
    main,
)

PAYLOAD = b"wavcse-immutable-artifact" * 64
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
LOCK_SUFFIX = ".wavcse-transfer.lock"


def _cache_root(tmp_path: Path, name: str = "cache") -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _source(tmp_path: Path, payload: bytes = PAYLOAD, name: str = "artifact.tar") -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def _entry(root: Path, digest: str = DIGEST) -> Path:
    return root / CACHE_ARTIFACTS_RELATIVE / digest[:2] / digest


def _populate(root: Path, source: Path, *, digest: str = DIGEST, size: int | None = None) -> dict:
    return cache_populate(
        str(root),
        str(source),
        expected_sha256=digest,
        expected_size=len(PAYLOAD) if size is None else size,
    )


# --- layout and identity ---------------------------------------------------------------


def test_populate_publishes_one_content_addressed_entry(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    source = _source(tmp_path)

    result = _populate(root, source)

    entry = _entry(root)
    assert result["sha256"] == DIGEST
    assert result["size_bytes"] == str(len(PAYLOAD))
    assert entry.joinpath(CACHE_ENTRY_CONTENT_NAME).read_bytes() == PAYLOAD
    assert sorted(path.name for path in entry.iterdir()) == [
        CACHE_ENTRY_CONTENT_NAME,
        CACHE_ENTRY_METADATA_NAME,
    ]


def test_populate_writes_a_marker_describing_the_directory(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))

    marker = json.loads(root.joinpath(CACHE_MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["schema_version"] == "1"
    assert "S3" in marker["purpose"]


def test_populate_records_the_artifact_key_in_metadata(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    cache_populate(
        str(root),
        str(_source(tmp_path)),
        expected_sha256=DIGEST,
        expected_size=len(PAYLOAD),
        artifact="wavcse/embeddings/v1/voxceleb-minpooling.tar",
    )

    metadata = json.loads(
        _entry(root).joinpath(CACHE_ENTRY_METADATA_NAME).read_text(encoding="utf-8")
    )
    assert metadata["artifact"] == "wavcse/embeddings/v1/voxceleb-minpooling.tar"
    assert metadata["sha256"] == DIGEST
    assert metadata["size_bytes"] == len(PAYLOAD)


def test_the_cache_never_persists_bearer_material(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))

    written = b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    for forbidden in (
        b"X-Amz-Signature",
        b"X-Amz-Credential",
        b"Authorization",
        b"Bearer",
        b"RUNPOD_API_KEY",
        b"AKIA",
    ):
        assert forbidden not in written


def test_identical_content_is_cached_once(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    first = _source(tmp_path, name="one.tar")
    second = _source(tmp_path, name="two.tar")

    _populate(root, first)
    _populate(root, second)

    assert cache_stats(str(root))["entries"] == "1"


def test_the_same_filename_with_different_content_occupies_two_entries(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    other = b"different bytes entirely"
    other_digest = hashlib.sha256(other).hexdigest()
    source = _source(tmp_path, payload=PAYLOAD, name="artifact.tar")
    other_source = _source(tmp_path, payload=other, name="artifact.tar.other")

    _populate(root, source)
    cache_populate(
        str(root),
        str(other_source),
        expected_sha256=other_digest,
        expected_size=len(other),
    )

    assert cache_stats(str(root))["entries"] == "2"
    assert _entry(root).joinpath(CACHE_ENTRY_CONTENT_NAME).read_bytes() == PAYLOAD
    assert _entry(root, other_digest).joinpath(CACHE_ENTRY_CONTENT_NAME).read_bytes() == other


# --- hits ------------------------------------------------------------------------------


def test_materialize_places_a_verified_copy_at_the_destination(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "job" / "inputs" / "artifact.tar"
    destination.parent.mkdir(parents=True)

    result = cache_materialize(
        str(root),
        str(destination),
        expected_sha256=DIGEST,
        expected_size=len(PAYLOAD),
    )

    assert result["path"] == str(destination)
    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD


def test_materialize_refuses_to_replace_an_existing_destination(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"something else")

    with pytest.raises(TransferVerificationError, match=DESTINATION_EXISTS_MARKER):
        cache_materialize(str(root), str(destination), expected_sha256=DIGEST)


def test_materialize_replaces_an_existing_destination_when_asked(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"stale")

    cache_materialize(
        str(root),
        str(destination),
        expected_sha256=DIGEST,
        overwrite=True,
    )

    assert destination.read_bytes() == PAYLOAD


def test_materialize_leaves_no_staging_behind(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "placed" / "artifact.tar"
    destination.parent.mkdir()

    cache_materialize(str(root), str(destination), expected_sha256=DIGEST)

    assert destination.read_bytes() == PAYLOAD
    assert list(destination.parent.glob("*.wavcse-partial*")) == []


# --- misses and integrity --------------------------------------------------------------


def test_an_unknown_digest_is_a_miss(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    with pytest.raises(CacheMissError, match=CACHE_MISS_MARKER):
        cache_materialize(str(root), str(tmp_path / "missing.tar"), expected_sha256="a" * 64)


def test_a_corrupted_entry_is_refused_quarantined_and_rebuilt(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    source = _source(tmp_path)
    _populate(root, source)
    entry = _entry(root)
    entry.joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(b"X" * len(PAYLOAD))

    with pytest.raises(CacheIntegrityFailure, match=CACHE_CORRUPT_MARKER):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)

    assert not entry.exists(), "a contradictory entry must never stay in place"
    quarantined = list((root / CACHE_STAGING_RELATIVE).iterdir())
    assert len(quarantined) == 1
    assert quarantined[0].joinpath(CACHE_ENTRY_CONTENT_NAME).read_bytes() == b"X" * len(PAYLOAD)

    _populate(root, source)
    assert cache_stats(str(root))["entries"] == "1"


def test_a_truncated_entry_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    entry = _entry(root)
    entry.joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(PAYLOAD[:10])

    with pytest.raises(CacheIntegrityFailure):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_a_partial_entry_without_metadata_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    entry = _entry(root)
    entry.mkdir(parents=True)
    entry.joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(PAYLOAD)

    with pytest.raises(CacheIntegrityFailure):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_metadata_describing_a_different_digest_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    entry = _entry(root)
    metadata_path = entry.joinpath(CACHE_ENTRY_METADATA_NAME)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["sha256"] = "b" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(CacheIntegrityFailure):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_a_recorded_size_that_contradicts_the_request_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))

    with pytest.raises(CacheIntegrityFailure, match="bytes were expected"):
        cache_materialize(
            str(root),
            str(tmp_path / "out.tar"),
            expected_sha256=DIGEST,
            expected_size=len(PAYLOAD) + 1,
        )


def test_an_entry_directory_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(PAYLOAD)
    outside.joinpath(CACHE_ENTRY_METADATA_NAME).write_text(
        json.dumps({"schema_version": 1, "sha256": DIGEST, "size_bytes": len(PAYLOAD)}),
        encoding="utf-8",
    )
    entry = _entry(root)
    entry.parent.mkdir(parents=True)
    entry.symlink_to(outside)

    with pytest.raises(CacheRootError, match="symbolic link"):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_a_symlinked_prefix_directory_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    digests = root / CACHE_ARTIFACTS_RELATIVE
    digests.mkdir(parents=True)
    digests.joinpath(DIGEST[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(CacheRootError):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_a_symlinked_content_file_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(PAYLOAD)
    entry = _entry(root)
    entry.mkdir(parents=True)
    entry.joinpath(CACHE_ENTRY_CONTENT_NAME).symlink_to(outside)
    entry.joinpath(CACHE_ENTRY_METADATA_NAME).write_text(
        json.dumps({"schema_version": 1, "sha256": DIGEST, "size_bytes": len(PAYLOAD)}),
        encoding="utf-8",
    )

    with pytest.raises((CacheIntegrityFailure, CacheMissError)):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_a_symlinked_cache_root_is_refused(tmp_path: Path) -> None:
    real = _cache_root(tmp_path, "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(CacheRootError):
        cache_stats(str(link))


def test_a_missing_cache_root_is_reported_as_unavailable(tmp_path: Path) -> None:
    with pytest.raises(CacheRootError, match=CACHE_ROOT_MISSING_MARKER):
        cache_stats(str(tmp_path / "absent"))


# --- input validation ------------------------------------------------------------------


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 63, "a" * 65, "g" * 64, "../../etc/passwd", "../" * 21 + "a" * 22],
)
def test_a_malformed_digest_never_reaches_the_filesystem(tmp_path: Path, digest: str) -> None:
    root = _cache_root(tmp_path)
    with pytest.raises(TransferInputError):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=digest)


def test_an_absent_digest_is_refused_for_a_content_addressed_cache(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    with pytest.raises(TransferInputError, match="cannot identify an artifact without one"):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=None)  # type: ignore[arg-type]


def test_a_relative_destination_is_refused(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    with pytest.raises(TransferInputError, match="must be an absolute path"):
        cache_materialize(str(root), "relative.tar", expected_sha256=DIGEST)


def test_populate_refuses_a_symlinked_source(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    real = _source(tmp_path)
    link = tmp_path / "linked.tar"
    link.symlink_to(real)

    with pytest.raises(TransferInputError, match="could not be opened"):
        _populate(root, link)

    assert cache_stats(str(root))["entries"] == "0"


def test_populate_refuses_content_that_does_not_match_the_requested_digest(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)

    with pytest.raises(TransferVerificationError, match=r"hashes to"):
        cache_populate(
            str(root),
            str(_source(tmp_path)),
            expected_sha256="0" * 64,
            expected_size=len(PAYLOAD),
        )

    assert cache_stats(str(root))["entries"] == "0"
    assert list((root / CACHE_STAGING_RELATIVE).iterdir()) == []


def test_populate_refuses_a_size_that_does_not_match(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)

    with pytest.raises(TransferVerificationError, match="bytes were recorded"):
        cache_populate(
            str(root),
            str(_source(tmp_path)),
            expected_sha256=DIGEST,
            expected_size=len(PAYLOAD) + 1,
        )

    assert cache_stats(str(root))["entries"] == "0"


# --- atomicity and concurrency ---------------------------------------------------------


def test_concurrent_writers_publish_exactly_one_complete_entry(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    sources = [_source(tmp_path, name=f"writer-{index}.tar") for index in range(6)]
    failures: list[str] = []

    def writer(path: Path) -> None:
        try:
            _populate(root, path)
        except Exception as exc:  # reported through the assertion below
            failures.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(source,)) for source in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    entry = _entry(root)
    assert sorted(path.name for path in entry.iterdir()) == [
        CACHE_ENTRY_CONTENT_NAME,
        CACHE_ENTRY_METADATA_NAME,
    ]
    assert hashlib.sha256(entry.joinpath(CACHE_ENTRY_CONTENT_NAME).read_bytes()).hexdigest() == (
        DIGEST
    )
    assert cache_stats(str(root))["entries"] == "1"
    assert list((root / CACHE_STAGING_RELATIVE).iterdir()) == []


def test_an_incomplete_staging_directory_is_never_visible_as_an_entry(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    staging = root / CACHE_STAGING_RELATIVE / f"populate-{DIGEST[:12]}-abandoned"
    staging.mkdir(parents=True)
    staging.joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(PAYLOAD[:5])

    assert cache_stats(str(root))["entries"] == "0"
    with pytest.raises(CacheMissError):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_stats_reports_recorded_metadata_without_hashing_any_artifact(tmp_path: Path) -> None:
    """Inspecting a volume must stay cheap, so stats trusts metadata and use verifies it."""

    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    _entry(root).joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(b"Y" * len(PAYLOAD))

    stats = cache_stats(str(root))

    assert stats["entries"] == "1", "stats must not re-read the artifact"
    assert stats["cached_bytes"] == str(len(PAYLOAD))
    assert stats["unverified_entries"] == "0"
    with pytest.raises(CacheIntegrityFailure):
        cache_materialize(str(root), str(tmp_path / "out.tar"), expected_sha256=DIGEST)


def test_stats_counts_an_entry_without_usable_metadata_separately(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    broken = _entry(root, "c" * 64)
    broken.mkdir(parents=True)
    broken.joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(b"unverified")

    stats = cache_stats(str(root))

    assert stats["entries"] == "1"
    assert stats["unverified_entries"] == "1"
    assert stats["cached_bytes"] == str(len(PAYLOAD))
    assert stats["marker_schema_version"] == "1"


def test_stats_reports_staged_bytes(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    staging = root / CACHE_STAGING_RELATIVE
    staging.mkdir()
    staging.joinpath("leftover").write_bytes(b"0" * 4096)

    assert cache_stats(str(root))["staging_bytes"] == "4096"


def test_stats_reports_an_absent_marker(tmp_path: Path) -> None:
    assert cache_stats(str(_cache_root(tmp_path)))["marker_schema_version"] == "absent"


# --- streamed entry point ---------------------------------------------------------------


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, url=None, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_main_reports_a_miss_with_its_own_exit_status(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    code, _, err = _run(
        [
            CACHE_MATERIALIZE_OPERATION,
            "--root",
            str(root),
            "--expected-sha256",
            "0" * 64,
            "--destination",
            str(tmp_path / "out.tar"),
        ]
    )

    assert code == CACHE_MISS_EXIT_CODE
    assert err.startswith(f"{ERROR_KEY}\t")
    assert CACHE_MISS_MARKER in err


def test_main_materializes_and_reports_the_transfer_protocol(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "out.tar"

    code, out, _ = _run(
        [
            CACHE_MATERIALIZE_OPERATION,
            "--root",
            str(root),
            "--expected-sha256",
            DIGEST,
            "--expected-size",
            str(len(PAYLOAD)),
            "--destination",
            str(destination),
        ]
    )

    lines = out.splitlines()
    assert code == 0
    assert lines[0] == f"{SCHEMA_KEY}\t{SCHEMA_VERSION}"
    assert f"operation\t{CACHE_MATERIALIZE_OPERATION}" in lines
    assert "status\tok" in lines
    assert f"sha256\t{DIGEST}" in lines
    assert destination.read_bytes() == PAYLOAD


def test_main_populates_the_cache(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)

    code, out, _ = _run(
        [
            CACHE_POPULATE_OPERATION,
            "--root",
            str(root),
            "--source",
            str(_source(tmp_path)),
            "--expected-sha256",
            DIGEST,
            "--expected-size",
            str(len(PAYLOAD)),
        ]
    )

    assert code == 0
    assert f"operation\t{CACHE_POPULATE_OPERATION}" in out.splitlines()
    assert cache_stats(str(root))["entries"] == "1"


def test_main_stats_emits_the_fact_protocol(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))

    code, out, _ = _run([CACHE_STATS_OPERATION, "--root", str(root)])

    values = dict(line.split("\t", 1) for line in out.splitlines()[1:])
    assert code == 0
    assert values["operation"] == CACHE_STATS_OPERATION
    assert values["status"] == "ok"
    assert values["entries"] == "1"
    assert values["root"] == str(root)


def test_main_reports_a_missing_root_without_a_traceback(tmp_path: Path) -> None:
    code, out, err = _run([CACHE_STATS_OPERATION, "--root", str(tmp_path / "absent")])

    assert code == 1
    assert out == ""
    assert CACHE_ROOT_MISSING_MARKER in err


def test_cache_operations_never_require_a_presigned_url(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    assert worker_transfer.WAVCSE_PRESIGNED_URL == ""


def test_the_worker_module_imports_only_the_standard_library() -> None:
    """A worker has Python 3 and nothing else, so this program may not need an install."""

    source = Path(worker_transfer.__file__).read_text(encoding="utf-8")
    assert "from __future__ import annotations" not in source
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"typing"}, (
        f"worker module imports non-stdlib modules: {sorted(imported - sys.stdlib_module_names)}"
    )


# --- destination discipline ------------------------------------------------------------


def test_materialize_refuses_an_existing_destination_with_the_shared_marker(tmp_path: Path) -> None:
    """A cache hit must obey the same destination rules as a download."""

    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "placed" / "artifact.tar"
    destination.parent.mkdir()
    destination.write_bytes(b"something else")

    with pytest.raises(TransferVerificationError, match=DESTINATION_EXISTS_MARKER):
        cache_materialize(str(root), str(destination), expected_sha256=DIGEST)


def test_materialize_takes_the_destination_lock_for_a_concurrent_transfer(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "placed" / "artifact.tar"
    destination.parent.mkdir()
    lock_path = destination.parent / f"{destination.name}{LOCK_SUFFIX}"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(TransferError, match=TRANSFER_IN_PROGRESS_MARKER):
            cache_materialize(str(root), str(destination), expected_sha256=DIGEST)
    finally:
        os.close(descriptor)


def test_materialize_quarantines_an_entry_that_changed_while_it_was_copied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-copy rewrite is a cache integrity event, never an artifact failure."""

    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    destination = tmp_path / "placed" / "artifact.tar"
    destination.parent.mkdir()
    original = worker_transfer._copy_verified

    def racing_copy(content: str, descriptor: int) -> tuple[int, str]:
        size, _digest = original(content, descriptor)
        return size, "0" * 64

    monkeypatch.setattr(worker_transfer, "_copy_verified", racing_copy)

    with pytest.raises(CacheIntegrityFailure, match=CACHE_CORRUPT_MARKER):
        cache_materialize(str(root), str(destination), expected_sha256=DIGEST)

    assert not _entry(root).exists(), "the contradicted entry must be quarantined"
    assert not destination.exists()


# --- directory components a hostile volume could substitute ----------------------------


def test_populate_refuses_a_symlinked_staging_root(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.joinpath(CACHE_STAGING_RELATIVE).symlink_to(outside, target_is_directory=True)

    with pytest.raises(CacheRootError, match="symbolic link"):
        _populate(root, _source(tmp_path))

    assert list(outside.iterdir()) == [], "nothing may be staged outside the cache root"


def test_populate_refuses_a_symlinked_shard_directory(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    shards = root / CACHE_ARTIFACTS_RELATIVE
    shards.mkdir(parents=True)
    shards.joinpath(DIGEST[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(CacheRootError, match="symbolic link"):
        _populate(root, _source(tmp_path))

    assert list(outside.iterdir()) == []


def test_quarantine_never_writes_outside_a_symlinked_staging_root(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    _populate(root, _source(tmp_path))
    _entry(root).joinpath(CACHE_ENTRY_CONTENT_NAME).write_bytes(b"Z" * len(PAYLOAD))
    outside = tmp_path / "outside"
    outside.mkdir()
    root.joinpath(CACHE_STAGING_RELATIVE).rmdir()
    root.joinpath(CACHE_STAGING_RELATIVE).symlink_to(outside, target_is_directory=True)

    with pytest.raises(CacheIntegrityFailure):
        cache_materialize(str(root), str(tmp_path / "placed.tar"), expected_sha256=DIGEST)

    assert list(outside.iterdir()) == []


def test_stats_refuses_a_symlinked_artifacts_root(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    root.joinpath("artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(CacheRootError, match="symbolic link"):
        cache_stats(str(root))


def test_stats_never_follows_a_symlinked_staging_root(tmp_path: Path) -> None:
    """A symlinked staging root is not walked: os.walk follows its own argument."""

    root = _cache_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.bin").write_bytes(b"Z" * 4096)
    root.joinpath(CACHE_STAGING_RELATIVE).symlink_to(outside, target_is_directory=True)

    stats = cache_stats(str(root))

    assert stats["staging_bytes"] == "0", "bytes outside the cache must never be counted"


def test_a_symlinked_marker_is_not_read(tmp_path: Path) -> None:
    root = _cache_root(tmp_path)
    secret = tmp_path / "elsewhere.json"
    secret.write_text('{"schema_version": "9"}', encoding="utf-8")
    root.joinpath(CACHE_MARKER_NAME).symlink_to(secret)

    assert cache_stats(str(root))["marker_schema_version"] == "absent"
