"""Worker-side artifact download and upload over one presigned S3 URL.

This file runs on a disposable GPU worker, not on the controller. The controller streams
it to `python3 -` over direct SSH and prepends two generated assignments, including
the bearer URL, so the signature never appears in a controller log, a
worker process argument list, or shell history:

    WAVCSE_PRESIGNED_URL = 'https://...'

Three properties follow from that transport and must be preserved:

* stdlib only - a worker has Python 3, but not the wavcse-infra package, boto3, or the
  AWS CLI. `urllib` streams bodies without buffering a multi-gigabyte object in memory;
* no `from __future__` import and no reliance on this docstring - the generated
  assignment is prepended before the first statement of this file;
* the URL is never printed, and every error message strips URLs before it is written.

Downloads use two transports. Objects below `PARALLEL_DOWNLOAD_THRESHOLD_BYTES`, or objects
without a known expected size, stream over a single connection. Larger objects with a known
expected size are fetched as inclusive HTTP byte ranges by a bounded rolling window of
workers: each range is written at its own offset in a staging file, and the whole assembled
artifact must match the expected size and SHA-256 before it is placed at the destination.

Only an invocation that supplies the expected SHA-256 resumes earlier state, because that
digest is the only immutable identity a worker can verify. Those runs stage into a
deterministic `<destination>.wavcse-partial` file and record durable ranges in
`<destination>.wavcse-partial.json`; the record holds no bearer material, so a later
invocation with a fresh presigned URL can resume it, while unreadable or incompatible
records are discarded. Without a digest the object is still fetched in parallel, but from a
private one-shot staging file with no persisted state, so a different artifact can never
reuse the same ranges.

One destination has one transfer at a time, serialized by
`<destination>.wavcse-transfer.lock`, which outlives the staging file it protects.

Usage on the worker:

    python3 - download --destination <absolute-path> [--expected-size N]
                       [--expected-sha256 HEX] [--concurrency N] [--overwrite]
    python3 - upload --source <absolute-path>
    python3 - verify --destination <absolute-path> [--expected-size N] [--expected-sha256 HEX]
    python3 - cache-materialize --root <absolute-cache-root> --expected-sha256 HEX
                                [--expected-size N] --destination <absolute-path> [--overwrite]
    python3 - cache-populate --root <absolute-cache-root> --source <absolute-path>
                             --expected-sha256 HEX [--expected-size N] [--artifact KEY]
    python3 - cache-stats --root <absolute-cache-root>

`verify` never downloads and never writes: it reports the size and SHA-256 of an artifact
that is already placed, fails if it is absent, incomplete, or mismatched, and reports
whether another transfer currently holds the destination lock. A controller that stopped
waiting for a download uses it to decide, from worker evidence, whether the transfer
finished, is still running, or died.

The `cache-*` operations manage the rebuildable content-addressed cache that lives on a
mounted network volume, so a disposable worker can materialize a declared input without
downloading it again. None of them takes a presigned URL: bytes only ever enter the cache
from a file the canonical download already verified, so no bearer material is ever written
to persistent storage. The cache is an optimization over canonical storage, never a source
of truth: an entry is used only when its recorded digest, recorded size, and actual bytes
all agree with the request, and anything else is quarantined and rebuilt.

Result protocol on stdout, one tab-separated key per line:

    wavcse_transfer_schema	1
    operation	download
    status	ok
    path	/workspace/embeddings/voxceleb-minpooling.tar
    size_bytes	21474836480
    sha256	<64 lowercase hexadecimal characters>

`cache-stats` reports facts instead of one artifact, so it replaces the last three lines:

    wavcse_transfer_schema	1
    operation	cache-stats
    status	ok
    root	/workspace/cache
    entries	4
    cached_bytes	17179869184
    staging_bytes	0
    unverified_entries	0
    marker_schema_version	1

Failures write `wavcse_transfer_error	<redacted message>` to stderr and exit nonzero. A
cache miss is an expected outcome rather than a failure: it exits with status
`CACHE_MISS_EXIT_CODE` so a caller may fall through to the canonical download.
"""

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TextIO

# Prefer an injected assignment line (see module docstring) and degrade to empty so this
# module remains importable for review and unit tests on the controller.
WAVCSE_PRESIGNED_URL: str = globals().get("WAVCSE_PRESIGNED_URL", "")
WAVCSE_IF_NONE_MATCH: bool = globals().get("WAVCSE_IF_NONE_MATCH", False)

SCHEMA_KEY = "wavcse_transfer_schema"
SCHEMA_VERSION = "1"
ERROR_KEY = "wavcse_transfer_error"
DOWNLOAD_OPERATION = "download"
UPLOAD_OPERATION = "upload"
VERIFY_OPERATION = "verify"
# These markers are the machine-readable part of the sanitized error line for a
# destination that already exists. The controller imports them from this module, so the
# worker and the controller always agree on the classification of an inspect result.
TRANSFER_IN_PROGRESS_MARKER = "is already in progress on this worker"
DESTINATION_EXISTS_MARKER = "already exists; pass --overwrite to replace it deliberately"
DESTINATION_ABSENT_MARKER = "destination is absent"
DESTINATION_INCOMPLETE_MARKER = "destination is incomplete"
DESTINATION_MISMATCH_MARKER = "destination does not match"
# A bounded attempt that ran out of retries is not a failed artifact: the resumable state
# beside the destination is kept, so the caller retries with freshly issued credentials.
TRANSIENT_FAILURE_MARKER = "transient transfer failure; resumable state was kept"
DEFAULT_TIMEOUT_SECONDS = 60.0
CHUNK_SIZE_BYTES = 1024 * 1024
MAX_SINGLE_PUT_BYTES = 5_000_000_000
# Downloads at or above this size use parallel ranged requests when the expected size is
# known. Smaller artifacts stay on one sequential connection so tiny transfers keep the
# simple path.
PARALLEL_DOWNLOAD_THRESHOLD_BYTES = 64 * 1024 * 1024
DEFAULT_DOWNLOAD_CONCURRENCY = 8
MAX_DOWNLOAD_CONCURRENCY = 16
# Inclusive HTTP byte-range granularity, which is also the resume granularity.
DOWNLOAD_RANGE_SIZE_BYTES = 16 * 1024 * 1024
DEFAULT_RANGE_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 1.0
MAX_RETRY_BACKOFF_SECONDS = 15.0
# Version 2 requires the caller's expected SHA-256 in the resume identity, so a version 1
# record is never reused.
RESUME_SCHEMA_VERSION = 2
RESUME_STATE_LIMIT_BYTES = 1024 * 1024
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# Failures that a bounded retry may resolve. Opening and reading a response both map here.
# `http.client.HTTPException` covers IncompleteRead and RemoteDisconnected, which are not
# OSError subclasses.
_RETRYABLE_TRANSPORT_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    OSError,
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_RANGE_PATTERN = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")
_URL_PATTERN = re.compile(r"https?://[^\s]*", re.IGNORECASE)
_SIGNED_PARAMETER_PATTERN = re.compile(
    r"(?i)\b(x-amz-(?:signature|credential|security-token)|awsaccesskeyid)"
    r"(\s*[:=]\s*)[^\s&;,]+"
)
_PARTIAL_SUFFIX = ".wavcse-partial"
# `<destination>.wavcse-partial.json` records which inclusive ranges of
# `<destination>.wavcse-partial` are durable. It holds no bearer material.
_METADATA_SUFFIX = ".wavcse-partial.json"
# `<destination>.wavcse-transfer.lock` is the destination-wide transfer lock. It is never
# unlinked, so its lock lifetime never depends on the staging file being renamed.
_LOCK_SUFFIX = ".wavcse-transfer.lock"
# Placement no longer creates an intermediate name: the destination is linked straight
# from the verified inode. This prefix is kept only so a `.wavcse-stage-*` alias left by an
# earlier build's crash between linking and replacing its rename source is recognised and
# released instead of wedging the resumable state.
_STAGE_LINK_SUFFIX = ".wavcse-stage-"
# Linux descriptor path used to hard-link an open inode rather than a pathname.
_PROC_FD_ROOT = "/proc/self/fd"
_DELETE_CHARACTER = 127
_PRINTABLE_ASCII_START = 32

# --- Rebuildable artifact cache on a mounted network volume -------------------------------
#
# Layout beneath the cache root (the mount point of a network volume):
#
#   cache.json                                  marker: schema version and purpose
#   artifacts/sha256/<first-two-hex>/<digest>/content
#   artifacts/sha256/<first-two-hex>/<digest>/metadata.json
#   staging/                                    in-progress work; safe to delete
#
# An entry directory exists only after a complete, verified artifact has been renamed into
# place, so a partially written artifact can never be mistaken for a complete one. The
# digest is the identity, never a filename, so two artifacts with the same name and
# different content occupy different directories and can never collide.
CACHE_SCHEMA_VERSION = "1"
CACHE_MARKER_NAME = "cache.json"
CACHE_MARKER_PURPOSE = "rebuildable artifact cache; canonical storage is S3"
CACHE_ARTIFACTS_RELATIVE = "artifacts/sha256"
CACHE_STAGING_RELATIVE = "staging"
CACHE_ENTRY_CONTENT_NAME = "content"
CACHE_ENTRY_METADATA_NAME = "metadata.json"
CACHE_ENTRY_METADATA_SCHEMA_VERSION = 1
CACHE_MATERIALIZE_OPERATION = "cache-materialize"
CACHE_POPULATE_OPERATION = "cache-populate"
CACHE_STATS_OPERATION = "cache-stats"
CACHE_STATS_FIELDS = (
    "root",
    "entries",
    "cached_bytes",
    "staging_bytes",
    "unverified_entries",
    "marker_schema_version",
)
# A miss is ordinary control flow - the caller falls through to the canonical download - so
# it needs an exit status that cannot be confused with a real failure.
CACHE_MISS_EXIT_CODE = 3
# Markers are the machine-readable part of the sanitized error line, imported by the
# controller so both sides agree on how an outcome is classified.
CACHE_MISS_MARKER = "no verified cache entry for this artifact digest"
CACHE_CORRUPT_MARKER = "cache entry contradicted its recorded identity and was quarantined"
CACHE_ROOT_MISSING_MARKER = "cache root is not an available directory"


class TransferError(Exception):
    """Base class for worker-side transfer failures with user-facing messages."""


class TransferInputError(TransferError):
    """Raised when the request itself is incomplete or unsafe."""


class TransferVerificationError(TransferError):
    """Raised when a transferred artifact fails its size or checksum requirement."""


class CacheMissError(TransferError):
    """Raised when the cache holds no verified entry for the requested artifact.

    This is an expected outcome, not a failure: the caller downloads from canonical storage
    instead. It exists as a type so the exit status and marker can say so unambiguously.
    """


class CacheIntegrityFailure(TransferError):
    """Raised when a cache entry's bytes contradict the identity it claims.

    The entry is quarantined before this is raised, so a corrupt entry can never be accepted
    and can never wedge every later lookup for the same digest.
    """


class CacheRootError(TransferInputError):
    """Raised when the cache root is missing, unsafe, or not usable as a directory."""


class _RetryableRangeError(TransferError):
    """A byte-range failure that a bounded retry may resolve."""


class _IncompleteBodyError(_RetryableRangeError):
    """A byte-range response ended before the requested bytes arrived."""


class _RangeProtocolError(TransferError):
    """A byte-range response violated the HTTP range contract and is not retryable."""


class _LocalWriteError(TransferError):
    """A local spindle/filesystem failure while persisting a byte range."""


class _SharedStagingError(TransferError):
    """A staging file has more than one hard link, so it may not be written through."""


class _RangeUnsupportedError(TransferError):
    """The endpoint ignored `Range` and returned the whole object instead."""


class _HashingReader:
    """Hash exactly the bytes handed to the HTTP client during a streaming PUT."""

    def __init__(self, source: object) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        self.digest.update(chunk)
        self.size += len(chunk)
        return chunk


def validate_presigned_url(url: str) -> str:
    """Return an accepted presigned URL, or raise for an unsafe transport target."""

    if not url or url != url.strip():
        raise TransferInputError("the presigned URL is missing or has surrounding whitespace")
    if any(character.isspace() or ord(character) < _PRINTABLE_ASCII_START for character in url):
        raise TransferInputError("the presigned URL must not contain whitespace or control bytes")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise TransferInputError(
            "the presigned URL must be an absolute https URL; other schemes are refused"
        )
    return url


def validate_worker_path(path: str, *, label: str) -> str:
    """Return an accepted absolute worker path, or raise an actionable error."""

    if not path or path != path.strip():
        raise TransferInputError(f"{label} is missing or has surrounding whitespace")
    if not os.path.isabs(path):
        raise TransferInputError(
            f"{label} must be an absolute path so it cannot depend on the remote "
            f"login directory: {path!r}"
        )
    if path.endswith(os.sep):
        raise TransferInputError(f"{label} must name a file, not a directory: {path!r}")
    return path


def sha256_file(path: str, *, chunk_size: int = CHUNK_SIZE_BYTES) -> tuple[int, str]:
    """Stream a file and return its byte size and lowercase SHA-256 digest."""

    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as source:
            while True:
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise TransferError(f"could not read {path!r}: {_safe_text(exc)}") from exc
    return size, digest.hexdigest()


def sha256_descriptor(descriptor: int, *, chunk_size: int = CHUNK_SIZE_BYTES) -> tuple[int, str]:
    """Stream an already-open descriptor and return its size and SHA-256 digest.

    Reading from the descriptor, rather than re-opening the pathname, is what makes a
    verification unable to be redirected: the bytes hashed are the bytes of the inode that
    was inspected, whatever else happens to the name afterwards.
    """

    digest = hashlib.sha256()
    size = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    except OSError as exc:
        raise TransferError(f"could not read the artifact: {_safe_text(exc)}") from exc
    return size, digest.hexdigest()


def download_concurrency(value: int) -> int:
    """Return an accepted bounded worker download concurrency."""

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= MAX_DOWNLOAD_CONCURRENCY
    ):
        raise TransferInputError(
            f"download concurrency must be an integer between 1 and {MAX_DOWNLOAD_CONCURRENCY}"
        )
    return value


def transfer_active(destination: str) -> bool:
    """Return whether another process currently holds one destination's transfer lock.

    The lock is an advisory `flock` that outlives the staging file it protects, so asking
    the kernel is the only reliable liveness test for a transfer that a controller may have
    stopped waiting for.
    """

    path = f"{destination}{_LOCK_SUFFIX}"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        _validate_staging_file(descriptor, path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def verify_existing(
    destination: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> dict[str, str]:
    """Report one already-placed artifact, failing unless it satisfies the expectations.

    This never downloads, never writes, and never removes anything: it is the read-only
    evidence a controller needs after its own bounded command stopped waiting for a
    transfer it started. A destination that does not exist, is still growing, or does not
    match is reported as a failure so a caller cannot mistake it for a completed artifact.
    """

    target = validate_worker_path(destination, label="verify destination")
    expected_digest = _optional_sha256(expected_sha256)
    if expected_size is not None and expected_size < 0:
        raise TransferInputError("expected size must not be negative")
    if transfer_active(target):
        raise TransferError(
            f"verify: another transfer for {target!r} {TRANSFER_IN_PROGRESS_MARKER}"
        )
    # One no-follow open supplies every fact below. The artifact is never re-opened by
    # name, so nothing that happens to the pathname afterwards can change which bytes are
    # inspected, hashed, or reported.
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        staging = os.path.lexists(f"{target}{_PARTIAL_SUFFIX}")
        marker = DESTINATION_INCOMPLETE_MARKER if staging else DESTINATION_ABSENT_MARKER
        raise TransferVerificationError(f"verify: {marker}: {target!r}") from exc
    except OSError as exc:
        raise TransferVerificationError(
            f"verify: {DESTINATION_MISMATCH_MARKER}: {target!r} could not be opened "
            f"for verification: {_safe_text(exc)}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise TransferVerificationError(
                f"verify: {DESTINATION_MISMATCH_MARKER}: {target!r} is not a regular file"
            )
        if expected_size is not None and opened.st_size != expected_size:
            raise TransferVerificationError(
                f"verify: {DESTINATION_INCOMPLETE_MARKER}: {target!r} is {opened.st_size} "
                f"bytes, but {expected_size} bytes were expected"
            )
        size, digest = sha256_descriptor(descriptor)
        try:
            named = os.stat(target, follow_symlinks=False)
        except OSError as exc:
            raise TransferError(
                f"{TRANSIENT_FAILURE_MARKER}: {target!r} disappeared while it was being "
                f"verified: {_safe_text(exc)}"
            ) from exc
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise TransferError(
                f"{TRANSIENT_FAILURE_MARKER}: {target!r} was replaced while it was being "
                "verified; nothing is known about the entry now at that path"
            )
    finally:
        os.close(descriptor)
    if expected_digest is not None and digest != expected_digest:
        raise TransferVerificationError(
            f"verify: {DESTINATION_MISMATCH_MARKER}: {target!r} has SHA-256 {digest}, "
            f"but {expected_digest} was expected"
        )
    return {"path": target, "size_bytes": str(size), "sha256": digest}


def download(
    url: str,
    destination: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    overwrite: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
) -> dict[str, str]:
    """Download one object, verify the complete artifact, then place it atomically.

    A known expected size at or above `PARALLEL_DOWNLOAD_THRESHOLD_BYTES` selects the
    parallel ranged transport. That transport only resumes earlier partial state when the
    caller also supplies the expected SHA-256, because the digest is the only immutable
    identity a worker can actually verify. Without it, an interrupted transfer cannot be
    told apart from a different artifact of the same size, so no previous ranges are
    reused: the object is still fetched in parallel, but from a fresh private staging file
    and with no persisted state. Every other object keeps the single-connection path. Both
    paths verify the expected size and, when supplied, the whole-object SHA-256 before the
    destination path is touched.
    """

    validate_presigned_url(url)
    target = validate_worker_path(destination, label="download destination")
    expected_digest = _optional_sha256(expected_sha256)
    if expected_size is not None and expected_size < 0:
        raise TransferInputError("expected size must not be negative")
    workers = download_concurrency(concurrency)
    _require_destination_absent(target, overwrite)
    parent = os.path.dirname(target) or os.sep
    if not os.path.isdir(parent):
        raise TransferInputError(f"download directory does not exist: {parent!r}")
    # One destination has one transfer at a time, whatever transport it uses: a small,
    # size-unknown, or non-resumable download must serialize with a ranged one too.
    lock = _acquire_destination_lock(target)
    try:
        # Re-check under the lock: the unlocked check above is only a fast path.
        _discard_completed_placement_residue(target)
        _require_destination_absent(target, overwrite)
        if expected_size is not None and expected_size >= PARALLEL_DOWNLOAD_THRESHOLD_BYTES:
            return _ranged_download(
                url, target, parent, expected_size, expected_digest, overwrite, timeout, workers
            )
        return _sequential_download(
            url, target, parent, expected_size, expected_digest, overwrite, timeout
        )
    finally:
        os.close(lock)


def _require_destination_absent(target: str, overwrite: bool) -> None:
    """Refuse any existing destination entry, including a dangling symlink."""

    if not overwrite and os.path.lexists(target):
        raise TransferVerificationError(f"{target!r} {DESTINATION_EXISTS_MARKER}")


def _validate_staging_file(descriptor: int, path: str) -> None:
    """Require an opened staging file to be our own regular file, using the descriptor."""

    try:
        info = os.fstat(descriptor)
    except OSError as exc:
        raise TransferError(f"could not inspect {path!r}: {_safe_text(exc)}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise TransferError(f"{path!r} is not a regular file; refusing to use it as staging")
    if info.st_nlink != 1:
        raise _SharedStagingError(
            f"{path!r} has {info.st_nlink} hard links; refusing to use a shared file as staging"
        )
    _recheck_staging_path(path, descriptor)


def _recheck_staging_path(
    path: str,
    descriptor: int,
    *,
    directory_fd: int | None = None,
) -> None:
    """Require the pathname to still name the inode behind the descriptor."""

    try:
        current = os.fstat(descriptor)
        if directory_fd is None:
            named = os.stat(path, follow_symlinks=False)
        else:
            named = os.stat(os.path.basename(path), dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise TransferError(f"could not verify staging file {path!r}: {_safe_text(exc)}") from exc
    if (named.st_dev, named.st_ino) != (current.st_dev, current.st_ino):
        raise TransferError(f"{path!r} was replaced by another file while the transfer was running")


def _create_staging_file(target: str) -> tuple[str, int]:
    """Create a private, unpredictable staging file that cannot already exist."""

    path = f"{target}{_PARTIAL_SUFFIX}-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666
        )
    except OSError as exc:
        raise TransferError(f"could not create staging file {path!r}: {_safe_text(exc)}") from exc
    try:
        _validate_staging_file(descriptor, path)
    except BaseException:
        os.close(descriptor)
        _remove_quietly(path)
        raise
    return path, descriptor


def _release_own_stage_links(path: str, descriptor: int) -> None:
    """Release our own `.wavcse-stage-*` aliases of a staging inode that has extra links.

    A crash between creating a placement link and finishing the transfer can leave a
    second name for this inode. Only names in our own staging namespace that resolve to
    this exact inode are released; a hard link to any other file is left alone, so the
    staging file is still refused rather than written through.
    """

    try:
        verified = os.fstat(descriptor)
    except OSError as exc:
        raise TransferError(f"could not inspect {path!r}: {_safe_text(exc)}") from exc
    staging_name = os.path.basename(path)
    if not staging_name.endswith(_PARTIAL_SUFFIX):
        raise _SharedStagingError(
            f"{path!r} has {verified.st_nlink} hard links; refusing to use a shared file as staging"
        )
    prefix = f"{staging_name[: -len(_PARTIAL_SUFFIX)]}{_STAGE_LINK_SUFFIX}"
    directory = os.path.dirname(path) or os.sep
    try:
        with os.scandir(directory) as entries:
            candidates = [entry.name for entry in entries if entry.name.startswith(prefix)]
    except OSError as exc:
        raise TransferError(f"could not inspect {directory!r}: {_safe_text(exc)}") from exc
    for name in candidates:
        candidate = os.path.join(directory, name)
        try:
            entry = os.lstat(candidate)
        except OSError:
            continue
        if (entry.st_dev, entry.st_ino) == (verified.st_dev, verified.st_ino):
            _remove_quietly(candidate)
    try:
        remaining = os.fstat(descriptor).st_nlink
    except OSError as exc:
        raise TransferError(f"could not inspect {path!r}: {_safe_text(exc)}") from exc
    if remaining != 1:
        raise _SharedStagingError(
            f"{path!r} has {remaining} hard links; refusing to use a shared file as staging"
        )


def _open_resumable_staging(path: str) -> int:
    """Open or create the deterministic resumable staging file for one destination."""

    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o666)
    except OSError as exc:
        raise TransferError(
            f"could not open resumable staging file {path!r}: {_safe_text(exc)}"
        ) from exc
    try:
        try:
            _validate_staging_file(descriptor, path)
        except _SharedStagingError:
            # Only a placement-link crash residue in our own staging namespace is
            # recoverable; anything else keeps the shared-file refusal.
            _release_own_stage_links(path, descriptor)
            _validate_staging_file(descriptor, path)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _acquire_destination_lock(target: str) -> int:
    """Take the destination-wide transfer lock, or refuse to touch the destination.

    The lock has its own deterministic file that is never unlinked, so the critical
    section keeps exclusive ownership of one logical destination even while the staging
    data file is renamed or removed during completion.
    """

    path = f"{target}{_LOCK_SUFFIX}"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise TransferError(
            f"could not open the transfer lock for {target!r}: {_safe_text(exc)}"
        ) from exc
    try:
        _validate_staging_file(descriptor, path)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TransferError(
                f"another transfer for {target!r} {TRANSFER_IN_PROGRESS_MARKER}"
            ) from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _discard_resumable_state_locked(target: str) -> None:
    """Remove leftover resumable state while holding the destination lock."""

    _remove_quietly(f"{target}{_PARTIAL_SUFFIX}")
    _remove_quietly(f"{target}{_METADATA_SUFFIX}")


def _discard_completed_placement_residue(target: str) -> None:
    """Drop a staging name that is only a hard link to an already-placed artifact.

    A crash between placement and cleanup can leave the staging pathname sharing the
    destination's inode. That link is redundant rather than resumable, so it must not
    block, or be mistaken for, a later transfer.
    """

    staging = f"{target}{_PARTIAL_SUFFIX}"
    try:
        staged = os.lstat(staging)
        placed = os.lstat(target)
    except OSError:
        return
    if (staged.st_dev, staged.st_ino) != (placed.st_dev, placed.st_ino):
        return
    _remove_quietly(staging)
    _remove_quietly(f"{target}{_METADATA_SUFFIX}")


def _sequential_download(
    url: str,
    target: str,
    parent: str,
    expected_size: int | None,
    expected_digest: str | None,
    overwrite: bool,
    timeout: float,
) -> dict[str, str]:
    """Stream one object over a single connection into a private staging file."""

    if expected_size is not None:
        # This artifact is too small for ranged transfer, so any resumable state recorded
        # for the destination describes a different artifact and can be removed. The
        # caller holds the destination lock, so no other transfer can be using it.
        _discard_resumable_state_locked(target)
    staging, descriptor = _create_staging_file(target)
    complete = False
    try:
        written, observed = _stream_download(url, descriptor, timeout, expected_size)
        if expected_size is not None and written != expected_size:
            raise TransferVerificationError(
                f"downloaded {written} bytes, but {expected_size} bytes were expected"
            )
        if expected_digest is not None and observed != expected_digest:
            raise TransferVerificationError(
                f"downloaded artifact SHA-256 is {observed}, but {expected_digest} was expected"
            )
        _place_staging(staging, descriptor, target, overwrite)
        complete = True
        _fsync_directory(parent)
    finally:
        if not complete:
            _remove_quietly(staging)
        os.close(descriptor)
    return {"path": target, "size_bytes": str(written), "sha256": observed}


def _ranged_download(
    url: str,
    target: str,
    parent: str,
    size: int,
    expected_digest: str | None,
    overwrite: bool,
    timeout: float,
    workers: int,
) -> dict[str, str]:
    """Fetch one object as parallel byte ranges, then verify the whole artifact.

    The caller holds the destination lock for the whole critical section, so removing or
    creating staging state here cannot race another transfer for the same destination.
    """

    resumable = expected_digest is not None
    metadata: str | None = None
    identity: dict[str, object] | None = None
    if resumable:
        staging = f"{target}{_PARTIAL_SUFFIX}"
        metadata = f"{target}{_METADATA_SUFFIX}"
        identity = {
            "destination": target,
            "size_bytes": size,
            "sha256": expected_digest,
            "range_size_bytes": DOWNLOAD_RANGE_SIZE_BYTES,
        }
        descriptor = _open_resumable_staging(staging)
    else:
        # Without a digest this invocation cannot tell its object apart from whatever
        # produced the leftover state, so the state is discarded rather than reused.
        _discard_resumable_state_locked(target)
        staging, descriptor = _create_staging_file(target)
    keep_state = resumable
    written = 0
    observed = ""
    try:
        try:
            written, observed, keep_state = _transfer_ranges(
                url, descriptor, metadata, identity, size, timeout, workers
            )
        except TransferVerificationError:
            # The bytes on disk cannot become the expected artifact: never resume them.
            keep_state = False
            raise
        if written != size:
            keep_state = False
            raise TransferVerificationError(
                f"downloaded {written} bytes, but {size} bytes were expected"
            )
        if expected_digest is not None and observed != expected_digest:
            keep_state = False
            raise TransferVerificationError(
                f"downloaded artifact SHA-256 is {observed}, but {expected_digest} was expected"
            )
        _place_staging(staging, descriptor, target, overwrite)
    finally:
        if not keep_state:
            _remove_quietly(staging)
            if metadata is not None:
                _remove_quietly(metadata)
        os.close(descriptor)
    if metadata is not None:
        _remove_quietly(metadata)
    _fsync_directory(parent)
    return {"path": target, "size_bytes": str(written), "sha256": observed}


def _transfer_ranges(
    url: str,
    descriptor: int,
    metadata: str | None,
    identity: dict[str, object] | None,
    size: int,
    timeout: float,
    workers: int,
) -> tuple[int, str, bool]:
    """Run the ranged phase and report the staged size, digest, and reuse safety.

    Returns the size and digest of the complete staging file plus whether its state is
    still a valid resume point.
    """

    if identity is not None and metadata is not None:
        completed = _load_or_reset_resume_state(descriptor, metadata, identity, size)
        keep_state = True
    else:
        completed = set()
        keep_state = False
    try:
        _fetch_all_ranges(url, descriptor, metadata, identity, size, completed, timeout, workers)
        os.fsync(descriptor)
        written, observed = _hash_open_file(descriptor)
        return written, observed, keep_state
    except _RangeUnsupportedError:
        # The endpoint ignored every `Range` header. Reuse the single connection over the
        # whole object; the complete-artifact checks still apply, and the range record is
        # dropped because this attempt is no longer resumable.
        if metadata is not None:
            _remove_quietly(metadata)
        written, observed = _stream_download(url, descriptor, timeout, size)
        return written, observed, False


def _range_count(size: int) -> int:
    return max(1, -(-size // DOWNLOAD_RANGE_SIZE_BYTES))


def _range_bounds(index: int, size: int, range_size: int) -> tuple[int, int]:
    """Return the inclusive first and last byte of one zero-based range index."""

    start = index * range_size
    return start, min(start + range_size, size) - 1


def _fetch_all_ranges(
    url: str,
    descriptor: int,
    metadata: str | None,
    identity: dict[str, object] | None,
    size: int,
    completed: set[int],
    timeout: float,
    workers: int,
) -> None:
    """Fetch every missing range through a rolling window of at most `workers` futures."""

    count = _range_count(size)
    # A generator keeps memory flat even for an absurd expected size.
    pending = (index for index in range(count) if index not in completed)
    window = max(1, workers)
    stop = threading.Event()
    pool = ThreadPoolExecutor(max_workers=window)
    inflight: dict[Future[bool], int] = {}
    failure: BaseException | None = None
    dirty = False

    def submit_next() -> bool:
        index = next(pending, None)
        if index is None:
            return False
        future = pool.submit(_fetch_range_with_retries, url, descriptor, index, size, timeout, stop)
        inflight[future] = index
        return True

    def record(future: Future[bool]) -> None:
        nonlocal dirty
        index = inflight.pop(future)
        if future.result():
            completed.add(index)
            dirty = True

    def persist() -> None:
        nonlocal dirty
        if dirty and metadata is not None and identity is not None:
            _save_resume_state(metadata, identity, completed)
            dirty = False

    try:
        try:
            while len(inflight) < window and submit_next():
                pass
            while inflight:
                done, _ = wait(set(inflight), return_when=FIRST_COMPLETED)
                for future in done:
                    record(future)
                persist()
                if stop.is_set():
                    break
                while len(inflight) < window and submit_next():
                    pass
        except BaseException as exc:
            failure = exc
    finally:
        # Stop scheduling, then join everything already running so no range write can
        # continue after this call returns.
        stop.set()
        try:
            for future in list(inflight):
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            for future, index in inflight.items():
                if (
                    future.done()
                    and not future.cancelled()
                    and future.exception() is None
                    and future.result()
                ):
                    completed.add(index)
                    # A range that finished while the transfer was shutting down is durable
                    # work, so it must be recorded as resumable state like any other.
                    dirty = True
        finally:
            # Persisting runs even if shutting down or harvesting raised, so completed
            # work is never silently lost.
            persist()
    if failure is not None:
        if isinstance(failure, TransferError) or not isinstance(failure, Exception):
            raise failure
        raise TransferError(f"the ranged download failed: {_safe_text(failure)}") from failure


def _fetch_range_with_retries(
    url: str,
    descriptor: int,
    index: int,
    size: int,
    timeout: float,
    stop: threading.Event,
) -> bool:
    """Fetch one range with bounded retries; return whether it wrote the range."""

    start, end = _range_bounds(index, size, DOWNLOAD_RANGE_SIZE_BYTES)
    delay = RETRY_BACKOFF_SECONDS
    for attempt in range(1, DEFAULT_RANGE_ATTEMPTS + 1):
        if stop.is_set():
            return False
        try:
            _fetch_range(url, descriptor, start, end, size, timeout)
            return True
        except _RangeUnsupportedError:
            stop.set()
            raise
        except _RetryableRangeError as exc:
            if attempt >= DEFAULT_RANGE_ATTEMPTS:
                raise TransferError(
                    f"{TRANSIENT_FAILURE_MARKER}: byte range {start}-{end} failed after "
                    f"{attempt} attempts: {_safe_text(exc)}"
                ) from exc
            time.sleep(min(delay, MAX_RETRY_BACKOFF_SECONDS))
            delay *= 2
    return False


def _fetch_range(
    url: str,
    descriptor: int,
    start: int,
    end: int,
    size: int,
    timeout: float,
) -> None:
    """Write one inclusive byte range at its own offset after validating the response.

    Opening the response and consuming its body belong to the same controlled attempt, so
    a connection reset, timeout, or truncated body during a read is retried exactly like a
    failure to open, and no raw urllib or socket exception can escape unsanitized.
    """

    request = urllib.request.Request(url)
    request.add_header("Range", f"bytes={start}-{end}")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise _classify_http_error(exc) from exc
    except _RETRYABLE_TRANSPORT_ERRORS as exc:
        raise _retryable_transport_error("the byte range request failed", exc) from exc
    try:
        with response:
            _consume_range(response, descriptor, start, end, size)
    except _RETRYABLE_TRANSPORT_ERRORS as exc:
        raise _retryable_transport_error("the byte range body failed", exc) from exc


def _consume_range(
    response: object,
    descriptor: int,
    start: int,
    end: int,
    size: int,
) -> None:
    """Validate one ranged response and write its bytes at the requested offset."""

    status = getattr(response, "status", None)
    if status == 200:
        raise _RangeUnsupportedError("the storage endpoint ignored the byte range")
    if status != 206:
        raise _RangeProtocolError(
            f"the storage endpoint returned HTTP {status} for a byte range; HTTP 206 is required"
        )
    total = _validate_content_range(response, start, end)
    if total is not None and total != size:
        raise TransferVerificationError(
            f"the object announces {total} bytes, but {size} bytes were expected"
        )
    expected_length = end - start + 1
    announced = _content_length(response)
    if announced is not None and announced != expected_length:
        raise _RangeProtocolError(
            f"the byte range announced {announced} bytes, but {expected_length} "
            "bytes were requested"
        )
    offset = start
    remaining = expected_length
    while remaining > 0:
        chunk = response.read(min(CHUNK_SIZE_BYTES, remaining))  # type: ignore[attr-defined]
        if not chunk:
            raise _IncompleteBodyError(
                f"the byte range ended after {expected_length - remaining} of "
                f"{expected_length} bytes"
            )
        try:
            _pwrite_all(descriptor, chunk, offset)
        except OSError as exc:
            raise _LocalWriteError(f"could not write the artifact: {_safe_text(exc)}") from exc
        offset += len(chunk)
        remaining -= len(chunk)
    if response.read(1):  # type: ignore[attr-defined]
        raise _RangeProtocolError(
            "the storage endpoint returned more bytes than the requested byte range"
        )
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise _LocalWriteError(f"could not persist the artifact: {_safe_text(exc)}") from exc


def _retryable_transport_error(label: str, exc: BaseException) -> _RetryableRangeError:
    """Map one transport failure to a retryable error with URL-free diagnostic text."""

    reason = getattr(exc, "reason", None)
    return _RetryableRangeError(f"{label}: {_safe_text(reason if reason is not None else exc)}")


def _classify_http_error(exc: urllib.error.HTTPError) -> TransferError:
    """Map one ranged HTTP failure to a retryable or terminal transfer error."""

    if exc.code in RETRYABLE_HTTP_STATUSES:
        return _RetryableRangeError(
            f"the storage endpoint returned HTTP {exc.code} for a byte range"
        )
    if exc.code in (401, 403):
        return TransferError(
            f"the storage endpoint rejected the download with HTTP {exc.code}; the "
            "presigned URL may have expired and must be generated again"
        )
    return TransferError(f"the storage endpoint rejected the download with HTTP {exc.code}")


def _validate_content_range(response: object, start: int, end: int) -> int | None:
    """Validate one 206 `Content-Range` header and return its announced total size."""

    header = _header(response, "Content-Range")
    if header is None:
        raise _RangeProtocolError("a ranged response must include a Content-Range header")
    parsed = _CONTENT_RANGE_PATTERN.fullmatch(header.strip())
    if parsed is None:
        raise _RangeProtocolError("the storage endpoint returned a malformed Content-Range header")
    first, last = int(parsed.group(1)), int(parsed.group(2))
    if first != start or last != end:
        raise _RangeProtocolError(
            f"the storage endpoint returned bytes {first}-{last} for the requested "
            f"range {start}-{end}"
        )
    total_token = parsed.group(3)
    total = None if total_token == "*" else int(total_token)
    if total is not None and last >= total:
        raise _RangeProtocolError(
            "the storage endpoint returned a Content-Range that exceeds the announced size"
        )
    return total


def _pwrite_all(descriptor: int, data: bytes, offset: int) -> None:
    """Write every byte at an explicit offset so concurrent ranges cannot interleave."""

    view = memoryview(data)
    while view:
        written = os.pwrite(descriptor, view, offset)
        if written <= 0:
            raise OSError("the write made no progress")
        view = view[written:]
        offset += written


def _hash_open_file(descriptor: int) -> tuple[int, str]:
    """Stream one open artifact and return its size and lowercase SHA-256 digest."""

    try:
        size = os.fstat(descriptor).st_size
        digest = hashlib.sha256()
        offset = 0
        while offset < size:
            chunk = os.pread(descriptor, CHUNK_SIZE_BYTES, offset)
            if not chunk:
                break
            digest.update(chunk)
            offset += len(chunk)
    except OSError as exc:
        raise TransferError(f"could not read the downloaded artifact: {_safe_text(exc)}") from exc
    if offset != size:
        raise TransferError(
            f"could not read the complete downloaded artifact: {offset} of {size} bytes"
        )
    return size, digest.hexdigest()


def _load_or_reset_resume_state(
    descriptor: int,
    metadata: str,
    identity: dict[str, object],
    size: int,
) -> set[int]:
    """Return resumable completed ranges, discarding state that cannot be trusted."""

    count = _range_count(size)
    state = _read_resume_state(metadata)
    if state is not None:
        completed = state.get("completed_ranges")
        usable = (
            state.get("schema_version") == RESUME_SCHEMA_VERSION
            and state.get("artifact") == identity
            and isinstance(completed, list)
            and all(
                isinstance(index, int) and not isinstance(index, bool) and 0 <= index < count
                for index in completed
            )
        )
        if usable:
            return set(completed)
    _remove_quietly(metadata)
    try:
        os.ftruncate(descriptor, 0)
    except OSError as exc:
        raise TransferError(f"could not reset partial download state: {_safe_text(exc)}") from exc
    _save_resume_state(metadata, identity, set())
    return set()


def _read_resume_state(path: str) -> dict[str, object] | None:
    """Read one resume record, refusing symlinks, non-regular, and shared files."""

    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        chunks: list[bytes] = []
        remaining = RESUME_STATE_LIMIT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if len(payload) > RESUME_STATE_LIMIT_BYTES:
        return None
    try:
        state = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _save_resume_state(metadata: str, identity: dict[str, object], completed: set[int]) -> None:
    """Atomically persist which ranges are durable, so a later invocation can resume."""

    payload = json.dumps(
        {
            "schema_version": RESUME_SCHEMA_VERSION,
            "artifact": identity,
            "completed_ranges": sorted(completed),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = f"{metadata}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except OSError as exc:
        raise TransferError(f"could not persist download resume state: {_safe_text(exc)}") from exc
    try:
        _validate_staging_file(descriptor, temporary)
        try:
            _pwrite_all(descriptor, payload, 0)
            os.fsync(descriptor)
        except OSError as exc:
            raise TransferError(
                f"could not persist download resume state: {_safe_text(exc)}"
            ) from exc
    except BaseException:
        os.close(descriptor)
        _remove_quietly(temporary)
        raise
    os.close(descriptor)
    try:
        os.replace(temporary, metadata)
    except OSError as exc:
        _remove_quietly(temporary)
        raise TransferError(f"could not persist download resume state: {_safe_text(exc)}") from exc
    _fsync_directory(os.path.dirname(metadata) or os.sep)


def _open_destination_directory(target: str) -> int:
    """Open the destination directory so placement is anchored to it, not to its path."""

    directory = os.path.dirname(target) or os.sep
    try:
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise TransferError(
            f"could not open destination directory {directory!r}: {_safe_text(exc)}"
        ) from exc


def _unlink_in(directory_fd: int, name: str) -> None:
    """Remove one directory-relative name, ignoring absence or races."""

    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        return


def _link_verified_inode(
    descriptor: int,
    staging_name: str,
    directory_fd: int,
    destination_name: str,
) -> None:
    """Create one destination name from the inode behind an open descriptor.

    The destination is addressed relative to an open directory descriptor, which also
    selects the `linkat` path in `os.link` where `follow_symlinks` is honoured.
    """

    if os.path.isdir(_PROC_FD_ROOT):
        # linkat(AT_SYMLINK_FOLLOW) on the descriptor path links the open inode, so a
        # staging pathname swapped after validation cannot change what is placed.
        os.link(
            f"{_PROC_FD_ROOT}/{descriptor}",
            destination_name,
            follow_symlinks=True,
            dst_dir_fd=directory_fd,
        )
        return
    # A worker without procfs cannot address an inode by descriptor, so link the staging
    # name only after re-proving that it still names that inode; `_verify_placed` then
    # removes the entry again if a substitution won the remaining window.
    _recheck_staging_path(staging_name, descriptor, directory_fd=directory_fd)
    os.link(
        staging_name,
        destination_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _unlink_existing_destination(directory_fd: int, name: str, target: str) -> None:
    """Remove the destination entry an `--overwrite` transfer was asked to replace."""

    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TransferError(f"could not replace {target!r}: {_safe_text(exc)}") from exc


def _verify_placed(directory_fd: int, name: str, descriptor: int, target: str) -> None:
    """Require the destination entry to be the verified inode and no other file.

    Placement never moves a pathname, but this is the final proof that the bytes at the
    destination are the bytes that were verified: anything else is unlinked and reported.
    """

    try:
        placed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        verified = os.fstat(descriptor)
    except OSError as exc:
        raise TransferError(f"could not verify {target!r}: {_safe_text(exc)}") from exc
    if (placed.st_dev, placed.st_ino) == (verified.st_dev, verified.st_ino):
        return
    _unlink_in(directory_fd, name)
    raise TransferError(
        f"{target!r} did not receive the verified artifact; the substituted entry was removed"
    )


def _place_staging(staging: str, descriptor: int, target: str, overwrite: bool) -> None:
    """Create the destination from the verified staging inode, never from a pathname.

    `--overwrite` removes the previous destination entry first and then creates the new
    one from the verified inode, so the destination never becomes whatever a pathname
    happened to name at placement time. Between those two steps the destination name is
    briefly absent; a crash there leaves the verified staging file and its range record
    intact (`st_nlink` stays 1), so a later invocation resumes normally.
    """

    _recheck_staging_path(staging, descriptor)
    staging_name = os.path.basename(staging)
    target_name = os.path.basename(target)
    directory_fd = _open_destination_directory(target)
    try:
        if overwrite:
            _unlink_existing_destination(directory_fd, target_name, target)
        try:
            _link_verified_inode(descriptor, staging_name, directory_fd, target_name)
        except FileExistsError as exc:
            # A hard link atomically fails if the final path now exists, so a racing
            # writer is never overwritten and its file is never replaced by ours.
            raise TransferVerificationError(
                f"{target!r} appeared during the download; refusing to replace it"
            ) from exc
        except OSError as exc:
            raise TransferError(f"could not materialize {target!r}: {_safe_text(exc)}") from exc
        _verify_placed(directory_fd, target_name, descriptor, target)
        try:
            os.unlink(staging_name, dir_fd=directory_fd)
        except OSError:
            _remove_quietly(staging)
    finally:
        os.close(directory_fd)


def upload(
    url: str,
    source: str,
    *,
    if_none_match: bool = False,
    allowed_root: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Stream one local artifact to a presigned PUT URL and report its digest."""

    validate_presigned_url(url)
    origin = validate_worker_path(source, label="upload source")
    try:
        with _open_upload_source(origin, allowed_root) as body:
            size = os.fstat(body.fileno()).st_size
            if size > MAX_SINGLE_PUT_BYTES:
                raise TransferInputError(
                    "single PUT uploads are limited to 5 GB; package a smaller artifact"
                )
            hashed_body = _HashingReader(body)
            request = urllib.request.Request(url, data=hashed_body, method="PUT")
            request.add_header("Content-Length", str(size))
            request.add_header("Content-Type", "application/octet-stream")
            if if_none_match:
                request.add_header("If-None-Match", "*")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                _require_success(response, "upload")
                response.read(1024)
            if hashed_body.size != size:
                raise TransferVerificationError(
                    f"uploaded {hashed_body.size} bytes, but the source size was {size} bytes"
                )
            digest = hashed_body.digest.hexdigest()
    except urllib.error.HTTPError as exc:
        raise TransferError(
            f"the storage endpoint rejected the upload with HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TransferError(
            f"{TRANSIENT_FAILURE_MARKER}: the upload request failed: {_safe_text(exc.reason)}"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise TransferError(
            f"{TRANSIENT_FAILURE_MARKER}: the upload failed: {_safe_text(exc)}"
        ) from exc
    return {"path": origin, "size_bytes": str(size), "sha256": digest}


def cache_materialize(
    root: str,
    destination: str,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
    overwrite: bool = False,
) -> dict[str, str]:
    """Place one artifact at a destination from a verified cache entry, never from the name.

    The entry's recorded identity, recorded size, and actual bytes must all agree with the
    request before a single byte is copied. The copy is then verified again as it is written
    and placed atomically through the same inode-anchored path a canonical download uses, so
    a cache hit is exactly as strong a guarantee as a fresh download. A cache that cannot
    satisfy that is reported as a miss, and the caller downloads instead.
    """

    origin = validate_worker_path(destination, label="destination")
    digest = _required_sha256(expected_sha256, operation="cache-materialize")
    root_directory = _require_cache_root(root)
    content, _size = _verified_cache_entry(
        root_directory,
        digest,
        expected_size=expected_size,
    )
    # One destination has one writer, for every transport, and an existing destination is
    # refused rather than replaced on a guess. A cache hit must obey the same rules as a
    # download, otherwise the two paths would disagree about a destination they share.
    lock = _acquire_destination_lock(origin)
    try:
        _require_destination_absent(origin, overwrite)
        staging = f"{origin}{_PARTIAL_SUFFIX}-{os.getpid()}-{secrets.token_hex(6)}"
        descriptor = os.open(
            staging,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            _validate_staging_file(descriptor, staging)
            copied_size, copied_digest = _copy_verified(content, descriptor)
            if copied_digest != digest:
                # The entry changed underneath the copy. That is a cache integrity event, not
                # an artifact failure: quarantine it so the next attempt rebuilds from
                # canonical storage instead of failing this artifact.
                _quarantine_cache_entry(root_directory, os.path.dirname(content), digest)
                raise CacheIntegrityFailure(
                    f"{CACHE_CORRUPT_MARKER}: the cached artifact for {digest} changed while "
                    "it was copied"
                )
            _place_staging(staging, descriptor, origin, overwrite)
        finally:
            os.close(descriptor)
            _remove_quietly(staging)
    finally:
        os.close(lock)
    return {"path": origin, "size_bytes": str(copied_size), "sha256": copied_digest}


def cache_populate(
    root: str,
    source: str,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
    artifact: str | None = None,
) -> dict[str, str]:
    """Publish one already-downloaded artifact into the cache, atomically and idempotently.

    The artifact is copied into `staging/`, verified as it is copied, described by a metadata
    document that records the identity it satisfied, and only then renamed into its final
    digest-addressed directory in one step. A concurrent writer that published first wins:
    the loser verifies the winner's entry and reports it rather than replacing anything. This
    is why two writers can never produce a falsely complete artifact, and why a partial
    artifact can never appear at an entry path.
    """

    digest = _required_sha256(expected_sha256, operation="cache-populate")
    root_directory = _require_cache_root(root)
    source_path = validate_worker_path(source, label="populate source")
    entry_directory = _cache_prefix_directory(root_directory, digest)

    existing = _adoptable_cache_entry(root_directory, digest, expected_size=expected_size)
    if existing is not None:
        return {"path": entry_directory, "size_bytes": str(existing[1]), "sha256": digest}

    staging_root = _cache_staging_root(root_directory)
    stage_directory = tempfile.mkdtemp(prefix=f"populate-{digest[:12]}-", dir=staging_root)
    try:
        content_path = os.path.join(stage_directory, CACHE_ENTRY_CONTENT_NAME)
        size, copied_digest = _copy_source_into(source_path, content_path)
        if copied_digest != digest:
            raise TransferVerificationError(
                f"the artifact at {source_path!r} hashes to {copied_digest}, but {digest} was "
                "recorded for it; nothing was published to the cache"
            )
        if expected_size is not None and size != expected_size:
            raise TransferVerificationError(
                f"the artifact at {source_path!r} is {size} bytes, but {expected_size} bytes "
                "were recorded for it; nothing was published to the cache"
            )
        _write_cache_entry_metadata(
            stage_directory,
            digest=digest,
            size=size,
            artifact=artifact,
        )
        _fsync_directory(stage_directory)
        # Re-validate immediately before publishing: the copy above is the longest window in
        # which a symbolic link could have replaced one of the path components.
        entry_directory = _cache_entry_directory(root_directory, digest)
        try:
            os.rename(stage_directory, entry_directory)
        except OSError as exc:
            adopted = _adoptable_cache_entry(root_directory, digest, expected_size=expected_size)
            if adopted is None:
                raise CacheRootError(
                    f"could not publish the cache entry for {digest}: {_safe_text(exc)}"
                ) from exc
            return {"path": entry_directory, "size_bytes": str(adopted[1]), "sha256": digest}
        stage_directory = ""
        _fsync_directory(os.path.dirname(entry_directory))
        _write_cache_marker(root_directory)
        return {"path": entry_directory, "size_bytes": str(size), "sha256": digest}
    finally:
        if stage_directory:
            _remove_tree_no_follow(stage_directory)


def cache_stats(root: str) -> dict[str, str]:
    """Report the cache's recorded contents without hashing a byte.

    Entries are counted from their metadata documents, which is why this is fast even for a
    full volume; the bytes are re-verified whenever an entry is actually used. An entry whose
    metadata is missing or unreadable is counted separately instead of being reported as
    usable.
    """

    root_directory = _require_cache_root(root)
    entries = 0
    cached_bytes = 0
    unverified = 0
    artifacts_root = _cache_artifacts_root(root_directory)
    for entry_directory in _cache_entry_directories(artifacts_root):
        recorded = _recorded_cache_entry_size(entry_directory)
        if recorded is None:
            unverified += 1
            continue
        entries += 1
        cached_bytes += recorded
    return {
        "root": root_directory,
        "entries": str(entries),
        "cached_bytes": str(cached_bytes),
        "staging_bytes": str(_staging_bytes(os.path.join(root_directory, CACHE_STAGING_RELATIVE))),
        "unverified_entries": str(unverified),
        "marker_schema_version": _cache_marker_version(root_directory) or "absent",
    }


def _open_upload_source(origin: str, allowed_root: str | None):
    """Open a job output through directory fds, rejecting every symlink component."""

    if allowed_root is None:
        if not os.path.isfile(origin):
            raise TransferInputError(f"upload source is not a regular file: {origin!r}")
        return open(origin, "rb")
    root = validate_worker_path(allowed_root, label="upload allowed root")
    relative = os.path.relpath(origin, root)
    parts = relative.split(os.sep)
    if relative in {"", "."} or any(part in {"", ".", ".."} for part in parts):
        raise TransferInputError("upload source must stay inside the allowed job workspace")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            os.close(directory_fd)
            directory_fd = next_fd
        source_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            os.close(source_fd)
            raise TransferInputError("upload source is not a regular file")
        return os.fdopen(source_fd, "rb")
    finally:
        os.close(directory_fd)


def main(
    argv: list[str] | None = None,
    *,
    url: str | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run one transfer operation using the controller-supplied URL."""

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = _argument_parser()
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    operation = options.operation
    try:
        if operation == VERIFY_OPERATION:
            fields = verify_existing(
                options.destination,
                expected_size=options.expected_size,
                expected_sha256=options.expected_sha256,
            )
            text = _render(operation, fields)
        elif operation == CACHE_STATS_OPERATION:
            text = _render_cache_facts(operation, cache_stats(options.root))
        elif operation == CACHE_MATERIALIZE_OPERATION:
            fields = cache_materialize(
                options.root,
                options.destination,
                expected_sha256=options.expected_sha256,
                expected_size=options.expected_size,
                overwrite=options.overwrite,
            )
            text = _render(operation, fields)
        elif operation == CACHE_POPULATE_OPERATION:
            fields = cache_populate(
                options.root,
                options.source,
                expected_sha256=options.expected_sha256,
                expected_size=options.expected_size,
                artifact=options.artifact,
            )
            text = _render(operation, fields)
        else:
            presigned = validate_presigned_url(url if url is not None else WAVCSE_PRESIGNED_URL)
            if operation == DOWNLOAD_OPERATION:
                fields = download(
                    presigned,
                    options.destination,
                    expected_size=options.expected_size,
                    expected_sha256=options.expected_sha256,
                    overwrite=options.overwrite,
                    concurrency=options.concurrency,
                )
            else:
                fields = upload(
                    presigned,
                    options.source,
                    if_none_match=WAVCSE_IF_NONE_MATCH,
                    allowed_root=options.allowed_root,
                )
            text = _render(operation, fields)
    except (CacheMissError, CacheIntegrityFailure) as exc:
        # A miss and a quarantined entry are both ordinary outcomes that a caller resolves by
        # rebuilding from canonical storage, so they never look like a failure.
        err.write(f"{ERROR_KEY}\t{_safe_text(exc)}\n")
        return CACHE_MISS_EXIT_CODE
    except TransferError as exc:
        err.write(f"{ERROR_KEY}\t{_safe_text(exc)}\n")
        return 1
    except Exception as exc:
        # Defensive: an unexpected failure must still exit through the sanitized protocol
        # instead of printing a traceback that could echo the bearer URL. It is reported as
        # transient because an unclassified failure is not evidence about the artifact: the
        # controller must decide from a fresh observation rather than record a failure.
        err.write(
            f"{ERROR_KEY}\t{TRANSIENT_FAILURE_MARKER}: unexpected transfer failure: "
            f"{_safe_text(exc)}\n"
        )
        return 1
    out.write(text)
    return 0


def _stream_download(
    url: str,
    descriptor: int,
    timeout: float,
    expected_size: int | None,
) -> tuple[int, str]:
    """Copy the response body into the open staging descriptor and hash it as it streams."""

    digest = hashlib.sha256()
    written = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            _require_success(response, "download")
            announced = _content_length(response)
            if expected_size is not None and announced is not None and announced != expected_size:
                raise TransferVerificationError(
                    f"the object announces {announced} bytes, but {expected_size} bytes were "
                    "expected"
                )
            # Rewrite the whole staging file from offset zero, exactly like "wb" would.
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            with open(descriptor, "wb", closefd=False) as sink:
                while True:
                    chunk = response.read(CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk)
                    written += len(chunk)
                    digest.update(chunk)
                sink.flush()
                os.fsync(descriptor)
    except urllib.error.HTTPError as exc:
        raise TransferError(
            f"the storage endpoint rejected the download with HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TransferError(
            f"{TRANSIENT_FAILURE_MARKER}: the download request failed: {_safe_text(exc.reason)}"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise TransferError(
            f"{TRANSIENT_FAILURE_MARKER}: the download failed: {_safe_text(exc)}"
        ) from exc
    return written, digest.hexdigest()


def _required_sha256(value: str | None, *, operation: str) -> str:
    """Return the canonical digest a cache operation requires, or fail the request."""

    if value is None:
        raise TransferInputError(
            f"{operation} requires an expected SHA-256: a content-addressed cache cannot "
            "identify an artifact without one"
        )
    return _optional_sha256(value) or ""


def _require_cache_root(root: str) -> str:
    """Return the cache root when it is a real directory, and never a symlink."""

    validated = validate_worker_path(root, label="cache root")
    if os.path.islink(validated) or not os.path.isdir(validated):
        raise CacheRootError(f"{CACHE_ROOT_MISSING_MARKER}: {validated!r}")
    return validated


def _cache_entry_directory(root: str, digest: str) -> str:
    """Build the digest-addressed entry path, refusing any path that could escape the root.

    The digest is validated as 64 hexadecimal characters and every intermediate component
    comes from a constant, so traversal is impossible by construction. The remaining risk is
    a symlink substituted for one of those components, which is refused explicitly and then
    double-checked against the resolved root.
    """

    _cache_artifacts_root(root)
    current = _reject_symlink_components(
        root, (*CACHE_ARTIFACTS_RELATIVE.split("/"), digest[:2], digest)
    )
    real_root = os.path.realpath(root)
    real_entry = os.path.realpath(current)
    if real_entry != os.path.join(real_root, CACHE_ARTIFACTS_RELATIVE, digest[:2], digest):
        raise CacheRootError(
            f"cache entry path for {digest} resolves outside the cache root {root!r}"
        )
    return current


def _verified_cache_entry(
    root: str,
    digest: str,
    *,
    expected_size: int | None,
) -> tuple[str, int]:
    """Return one entry's verified content path and size, or raise a classified failure.

    Every part of the claim is checked against the bytes: the metadata must parse, name a
    supported schema, and agree with the requested digest and size, and the content must be a
    regular file whose actual size and actual SHA-256 match. Anything else is quarantined and
    reported, because the only safe responses to a cache that contradicts itself are to
    rebuild it or to fail explicitly.
    """

    entry_directory = _cache_entry_directory(root, digest)
    if not os.path.isdir(entry_directory) or os.path.islink(entry_directory):
        raise CacheMissError(f"{CACHE_MISS_MARKER}: {digest}")
    content_path = os.path.join(entry_directory, CACHE_ENTRY_CONTENT_NAME)
    try:
        descriptor = os.open(content_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        _quarantine_cache_entry(root, entry_directory, digest)
        raise CacheIntegrityFailure(
            f"{CACHE_CORRUPT_MARKER}: {digest} has no readable content file ({_safe_text(exc)})"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            _quarantine_cache_entry(root, entry_directory, digest)
            raise CacheIntegrityFailure(
                f"{CACHE_CORRUPT_MARKER}: {digest} content is not a regular file"
            )
        recorded = _recorded_cache_entry(entry_directory, digest)
        if recorded is None:
            _quarantine_cache_entry(root, entry_directory, digest)
            raise CacheIntegrityFailure(
                f"{CACHE_CORRUPT_MARKER}: {digest} has no usable metadata document"
            )
        recorded_size = recorded.get("size_bytes")
        if (
            expected_size is not None
            and recorded_size is not None
            and recorded_size != expected_size
        ):
            _quarantine_cache_entry(root, entry_directory, digest)
            raise CacheIntegrityFailure(
                f"{CACHE_CORRUPT_MARKER}: {digest} records {recorded_size} bytes, but "
                f"{expected_size} bytes were expected"
            )
        size, actual_digest = sha256_descriptor(descriptor)
    finally:
        os.close(descriptor)
    if actual_digest != digest or (expected_size is not None and size != expected_size):
        _quarantine_cache_entry(root, entry_directory, digest)
        raise CacheIntegrityFailure(
            f"{CACHE_CORRUPT_MARKER}: {digest} holds {size} bytes hashing to {actual_digest}"
        )
    if recorded_size is not None and recorded_size != size:
        _quarantine_cache_entry(root, entry_directory, digest)
        raise CacheIntegrityFailure(
            f"{CACHE_CORRUPT_MARKER}: {digest} records {recorded_size} bytes but holds {size}"
        )
    return content_path, size


def _adoptable_cache_entry(
    root: str,
    digest: str,
    *,
    expected_size: int | None,
) -> tuple[str, int] | None:
    """Return an entry that already verifies, or `None` when there is nothing to adopt."""

    try:
        return _verified_cache_entry(root, digest, expected_size=expected_size)
    except (CacheMissError, CacheIntegrityFailure):
        return None


def _recorded_cache_entry(entry_directory: str, digest: str) -> dict[str, int] | None:
    """Read one entry's metadata document and return its recorded size, when it is coherent."""

    metadata_path = os.path.join(entry_directory, CACHE_ENTRY_METADATA_NAME)
    try:
        descriptor = os.open(metadata_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as metadata_file:
            payload = json.load(metadata_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != CACHE_ENTRY_METADATA_SCHEMA_VERSION:
        return None
    if payload.get("sha256") != digest:
        return None
    size = payload.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    return {"size_bytes": size}


def _recorded_cache_entry_size(entry_directory: str) -> int | None:
    """Return the recorded size of one entry directory, or `None` when it is unusable."""

    digest = os.path.basename(entry_directory.rstrip(os.sep))
    recorded = _recorded_cache_entry(entry_directory, digest)
    return None if recorded is None else recorded["size_bytes"]


def _write_cache_entry_metadata(
    stage_directory: str,
    *,
    digest: str,
    size: int,
    artifact: str | None,
) -> None:
    """Describe one staged artifact inside the staging directory, before it is published.

    The document holds only non-secret provenance: the identity that was satisfied, the size,
    the artifact key it came from, and when it was cached. No URL, credential, or other
    bearer material is ever written to persistent storage.
    """

    payload = {
        "schema_version": CACHE_ENTRY_METADATA_SCHEMA_VERSION,
        "sha256": digest,
        "size_bytes": size,
        "artifact": artifact,
        "cached_at": _utc_timestamp(),
    }
    path = os.path.join(stage_directory, CACHE_ENTRY_METADATA_NAME)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as metadata_file:
        json.dump(payload, metadata_file, sort_keys=True)
        metadata_file.write("\n")
        metadata_file.flush()
        os.fsync(metadata_file.fileno())


def _write_cache_marker(root: str) -> None:
    """Create the cache root's marker document once, describing what the directory is."""

    path = os.path.join(root, CACHE_MARKER_NAME)
    if os.path.lexists(path):
        return
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "purpose": CACHE_MARKER_PURPOSE,
        "cache_root": root,
        "created_at": _utc_timestamp(),
    }
    staged = f"{path}.staged-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        with open(staged, "x", encoding="utf-8") as marker_file:
            json.dump(payload, marker_file, sort_keys=True)
            marker_file.write("\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
        os.rename(staged, path)
    except OSError:
        _remove_quietly(staged)
        return
    _fsync_directory(root)


def _cache_marker_version(root: str) -> str | None:
    """Return the marker's schema version, or `None` when there is no readable marker."""

    path = os.path.join(root, CACHE_MARKER_NAME)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as marker_file:
            payload = json.load(marker_file)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        return None
    version = payload.get("schema_version")
    return version if isinstance(version, str) else None


def _quarantine_cache_entry(root: str, entry_directory: str, digest: str) -> None:
    """Move one unusable entry aside so it can be inspected and cannot block a rebuild.

    Moving rather than deleting keeps the failing bytes available for diagnosis, and puts
    them under `staging/`, which is documented as safe to delete. If even the move fails,
    the failure is still reported: a cache problem must never become silent.
    """

    try:
        staging_root = _cache_staging_root(root)
        target = os.path.join(staging_root, f"quarantine-{digest}-{secrets.token_hex(6)}")
        os.rename(entry_directory, target)
    except (OSError, TransferError):
        return


def _cache_entry_directories(artifacts_root: str) -> list[str]:
    """List entry directories two levels below the artifacts root, without following links."""

    entries: list[str] = []
    for prefix in _safe_listdir(artifacts_root):
        prefix_path = os.path.join(artifacts_root, prefix)
        if os.path.islink(prefix_path) or not os.path.isdir(prefix_path):
            continue
        for digest in _safe_listdir(prefix_path):
            entry = os.path.join(prefix_path, digest)
            if os.path.islink(entry) or not os.path.isdir(entry):
                continue
            entries.append(entry)
    return entries


def _safe_listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def _staging_bytes(staging_root: str) -> int:
    """Total bytes staged but not yet published, counted without following any link.

    The top of the walk is checked explicitly: `os.walk` follows its own argument when that
    argument is a symbolic link, so without this a link planted at `staging` would make a
    read-only statistics command walk an arbitrary tree (including the filesystem root).
    All other cache paths refuse a symlinked component; this keeps the diagnostic consistent
    with them, and a path the cache would never stage into holds no staged bytes.
    """

    if os.path.islink(staging_root) or not os.path.isdir(staging_root):
        return 0
    total = 0
    for directory, directory_names, file_names in os.walk(staging_root, followlinks=False):
        directory_names[:] = [
            name for name in directory_names if not os.path.islink(os.path.join(directory, name))
        ]
        for name in file_names:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def _copy_verified(source_path: str, sink_descriptor: int) -> tuple[int, str]:
    """Copy a cache entry's already-open content into a staging descriptor, hashing it."""

    try:
        source_descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise CacheMissError(
            f"{CACHE_MISS_MARKER}: cached content became unreadable ({_safe_text(exc)})"
        ) from exc
    try:
        return _copy_descriptor(source_descriptor, sink_descriptor)
    finally:
        os.close(source_descriptor)


def _copy_source_into(source_path: str, destination_path: str) -> tuple[int, str]:
    """Copy one regular file into a new staging file, returning its size and SHA-256."""

    try:
        source_descriptor = os.open(source_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise TransferInputError(
            f"populate source {source_path!r} could not be opened: {_safe_text(exc)}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(source_descriptor).st_mode):
            raise TransferInputError(f"populate source {source_path!r} is not a regular file")
        sink_descriptor = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
        )
        try:
            return _copy_descriptor(source_descriptor, sink_descriptor)
        finally:
            os.close(sink_descriptor)
    finally:
        os.close(source_descriptor)


def _copy_descriptor(source_descriptor: int, sink_descriptor: int) -> tuple[int, str]:
    """Stream one descriptor into another, returning the copied size and its SHA-256."""

    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        os.ftruncate(sink_descriptor, 0)
        os.lseek(sink_descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(source_descriptor, CHUNK_SIZE_BYTES)
            if not chunk:
                break
            written = 0
            while written < len(chunk):
                written += os.write(sink_descriptor, chunk[written:])
            digest.update(chunk)
            total += len(chunk)
        os.fsync(sink_descriptor)
    except OSError as exc:
        raise _LocalWriteError(f"could not copy an artifact locally: {_safe_text(exc)}") from exc
    return total, digest.hexdigest()


def _remove_tree_no_follow(path: str) -> None:
    """Remove a directory tree this module created, refusing to follow any symbolic link."""

    if not os.path.isdir(path) or os.path.islink(path):
        return
    for directory, directory_names, file_names in os.walk(path, topdown=False, followlinks=False):
        for name in directory_names:
            child = os.path.join(directory, name)
            if os.path.islink(child):
                _remove_quietly(child)
                continue
            try:
                os.rmdir(child)
            except OSError:
                continue
        for name in file_names:
            _remove_quietly(os.path.join(directory, name))
    try:
        os.rmdir(path)
    except OSError:
        return


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _render_cache_facts(operation: str, fields: dict[str, str]) -> str:
    lines = [f"{SCHEMA_KEY}\t{SCHEMA_VERSION}", f"operation\t{operation}", "status\tok"]
    lines.extend(f"{key}\t{value}" for key, value in fields.items())
    return "\n".join(lines) + "\n"


def _reject_symlink_components(root: str, parts: tuple[str, ...]) -> str:
    """Walk one cache-relative path and refuse any component that is a symbolic link."""

    current = root
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise CacheRootError(
                f"cache path {current!r} is a symbolic link; the cache never follows one"
            )
    return current


def _cache_artifacts_root(root: str) -> str:
    """Return the artifacts root, refusing a symbolic link anywhere along it."""

    path = _reject_symlink_components(root, tuple(CACHE_ARTIFACTS_RELATIVE.split("/")))
    if os.path.exists(path) and not os.path.isdir(path):
        raise CacheRootError(f"cache path {path!r} is not a directory")
    return path


def _cache_staging_root(root: str) -> str:
    """Return the staging directory, creating it without ever following a symbolic link.

    `os.makedirs(..., exist_ok=True)` is not usable here: its existence check follows a
    symbolic link, so a `staging` link planted on the volume would be accepted and every
    staged write - and every quarantined entry - would land outside the cache root.
    """

    return _make_cache_directory(root, CACHE_STAGING_RELATIVE)


def _make_cache_directory(parent: str, name: str) -> str:
    """Create one cache subdirectory without following or accepting a symbolic link.

    `os.makedirs(..., exist_ok=True)` and `os.mkdir` on a path with missing parents are both
    unusable here: the first accepts a symbolic link because its existence check follows one,
    and the second cannot create a chain. Each component is therefore created and validated
    on its own, so a link planted anywhere along the path is refused rather than followed.
    """

    path = os.path.join(parent, name)
    if os.path.islink(path):
        raise CacheRootError(f"cache path {path!r} is a symbolic link; the cache never follows one")
    try:
        os.mkdir(path, 0o755)
    except FileExistsError:
        if os.path.islink(path) or not os.path.isdir(path):
            raise CacheRootError(
                f"cache path {path!r} is not a directory; the cache will not use it"
            ) from None
    except OSError as exc:
        raise CacheRootError(
            f"could not create the cache directory {path!r}: {_safe_text(exc)}"
        ) from exc
    return path


def _cache_prefix_directory(root: str, digest: str) -> str:
    """Create one digest shard and re-validate the whole entry path around it.

    The chain is built component by component, then the full entry path is re-validated
    immediately afterwards, so a symbolic link can never be introduced between creation and
    use.
    """

    current = _make_cache_directory(root, CACHE_ARTIFACTS_RELATIVE.split("/")[0])
    for part in CACHE_ARTIFACTS_RELATIVE.split("/")[1:]:
        current = _make_cache_directory(current, part)
    _make_cache_directory(current, digest[:2])
    return _cache_entry_directory(root, digest)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wavcse-artifact-transfer",
        description="Download or upload one artifact with a controller-supplied presigned URL.",
    )
    subcommands = parser.add_subparsers(dest="operation", required=True)
    fetch = subcommands.add_parser(DOWNLOAD_OPERATION, help="Materialize one artifact.")
    _add_destination_arguments(fetch)
    fetch.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_DOWNLOAD_CONCURRENCY,
        help=f"Parallel byte-range streams (1-{MAX_DOWNLOAD_CONCURRENCY}).",
    )
    fetch.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing destination file deliberately.",
    )
    inspect_existing = subcommands.add_parser(
        VERIFY_OPERATION,
        help="Report one already-placed artifact without downloading anything.",
    )
    _add_destination_arguments(inspect_existing)
    send = subcommands.add_parser(UPLOAD_OPERATION, help="Upload one local artifact.")
    send.add_argument("--source", required=True, help="Absolute source file path.")
    send.add_argument("--allowed-root", help="Confine a job output to this workspace.")
    materialize = subcommands.add_parser(
        CACHE_MATERIALIZE_OPERATION,
        help="Place one artifact from the rebuildable cache, if it is cached and verified.",
    )
    materialize.add_argument("--root", required=True, help="Absolute cache root directory.")
    _add_destination_arguments(materialize)
    materialize.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing destination file deliberately.",
    )
    populate = subcommands.add_parser(
        CACHE_POPULATE_OPERATION,
        help="Publish one already-verified artifact into the rebuildable cache.",
    )
    populate.add_argument("--root", required=True, help="Absolute cache root directory.")
    populate.add_argument("--source", required=True, help="Absolute source file path.")
    populate.add_argument("--expected-sha256", required=True, help="Required SHA-256 digest.")
    populate.add_argument("--expected-size", type=int, default=None, help="Expected byte size.")
    populate.add_argument("--artifact", default=None, help="Non-secret artifact key for metadata.")
    statistics = subcommands.add_parser(
        CACHE_STATS_OPERATION,
        help="Report the cache's recorded contents without hashing any artifact.",
    )
    statistics.add_argument("--root", required=True, help="Absolute cache root directory.")
    return parser


def _add_destination_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--destination", required=True, help="Absolute destination file path.")
    parser.add_argument("--expected-size", type=int, default=None, help="Expected byte size.")
    parser.add_argument("--expected-sha256", default=None, help="Expected SHA-256 digest.")


def _render(operation: str, fields: dict[str, str]) -> str:
    lines = [
        f"{SCHEMA_KEY}\t{SCHEMA_VERSION}",
        f"operation\t{operation}",
        "status\tok",
        f"path\t{fields['path']}",
        f"size_bytes\t{fields['size_bytes']}",
        f"sha256\t{fields['sha256']}",
    ]
    return "\n".join(lines) + "\n"


def _require_success(response: object, action: str) -> None:
    status = getattr(response, "status", None)
    if isinstance(status, int) and not 200 <= status < 300:
        raise TransferError(f"the storage endpoint returned HTTP {status} for the {action}")


def _header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    return None if value is None else str(value)


def _content_length(response: object) -> int | None:
    value = _header(response, "Content-Length")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise TransferInputError("expected SHA-256 must be 64 hexadecimal characters")
    return normalized


def _fsync_directory(path: str) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        os.close(descriptor)


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        return


def _safe_text(value: object) -> str:
    """Return diagnostic text with any URL removed so a signature cannot be printed."""

    text = _URL_PATTERN.sub("<redacted-url>", str(value))
    text = _SIGNED_PARAMETER_PATTERN.sub(r"\1\2<redacted>", text)
    return " ".join(text.split())[:500]


if __name__ == "__main__":
    raise SystemExit(main())
