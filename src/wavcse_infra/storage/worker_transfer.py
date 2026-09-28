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

Result protocol on stdout, one tab-separated key per line:

    wavcse_transfer_schema	1
    operation	download
    status	ok
    path	/workspace/embeddings/voxceleb-minpooling.tar
    size_bytes	21474836480
    sha256	<64 lowercase hexadecimal characters>

Failures write `wavcse_transfer_error	<redacted message>` to stderr and exit nonzero.
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


class TransferError(Exception):
    """Base class for worker-side transfer failures with user-facing messages."""


class TransferInputError(TransferError):
    """Raised when the request itself is incomplete or unsafe."""


class TransferVerificationError(TransferError):
    """Raised when a transferred artifact fails its size or checksum requirement."""


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
        raise TransferVerificationError(
            f"{target!r} already exists; pass --overwrite to replace it deliberately"
        )


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
                f"another transfer for {target!r} is already in progress on this worker"
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
                    f"byte range {start}-{end} failed after {attempt} attempts: {_safe_text(exc)}"
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
        raise TransferError(f"the upload request failed: {_safe_text(exc.reason)}") from exc
    except (TimeoutError, OSError) as exc:
        raise TransferError(f"the upload failed: {_safe_text(exc)}") from exc
    return {"path": origin, "size_bytes": str(size), "sha256": digest}


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
    except TransferError as exc:
        err.write(f"{ERROR_KEY}\t{_safe_text(exc)}\n")
        return 1
    except Exception as exc:
        # Defensive: an unexpected failure must still exit through the sanitized protocol
        # instead of printing a traceback that could echo the bearer URL. It fails the
        # transfer; nothing is retried or ignored.
        err.write(f"{ERROR_KEY}\tunexpected transfer failure: {_safe_text(exc)}\n")
        return 1
    out.write(_render(operation, fields))
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
        raise TransferError(f"the download request failed: {_safe_text(exc.reason)}") from exc
    except (TimeoutError, OSError) as exc:
        raise TransferError(f"the download failed: {_safe_text(exc)}") from exc
    return written, digest.hexdigest()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wavcse-artifact-transfer",
        description="Download or upload one artifact with a controller-supplied presigned URL.",
    )
    subcommands = parser.add_subparsers(dest="operation", required=True)
    fetch = subcommands.add_parser(DOWNLOAD_OPERATION, help="Materialize one artifact.")
    fetch.add_argument("--destination", required=True, help="Absolute destination file path.")
    fetch.add_argument("--expected-size", type=int, default=None, help="Expected byte size.")
    fetch.add_argument("--expected-sha256", default=None, help="Expected SHA-256 digest.")
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
    send = subcommands.add_parser(UPLOAD_OPERATION, help="Upload one local artifact.")
    send.add_argument("--source", required=True, help="Absolute source file path.")
    send.add_argument("--allowed-root", help="Confine a job output to this workspace.")
    return parser


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
