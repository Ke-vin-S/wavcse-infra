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

Usage on the worker:

    python3 - download --destination <absolute-path> [--expected-size N]
                       [--expected-sha256 HEX] [--overwrite]
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
import hashlib
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
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
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_URL_PATTERN = re.compile(r"https?://[^\s]*", re.IGNORECASE)
_SIGNED_PARAMETER_PATTERN = re.compile(
    r"(?i)\b(x-amz-(?:signature|credential|security-token)|awsaccesskeyid)"
    r"(\s*[:=]\s*)[^\s&;,]+"
)
_PARTIAL_SUFFIX = ".wavcse-partial"
_DELETE_CHARACTER = 127
_PRINTABLE_ASCII_START = 32


class TransferError(Exception):
    """Base class for worker-side transfer failures with user-facing messages."""


class TransferInputError(TransferError):
    """Raised when the request itself is incomplete or unsafe."""


class TransferVerificationError(TransferError):
    """Raised when a transferred artifact fails its size or checksum requirement."""


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


def download(
    url: str,
    destination: str,
    *,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
    overwrite: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Download one object into a temporary sibling file, verify it, then rename it."""

    validate_presigned_url(url)
    target = validate_worker_path(destination, label="download destination")
    expected_digest = _optional_sha256(expected_sha256)
    if expected_size is not None and expected_size < 0:
        raise TransferInputError("expected size must not be negative")
    if os.path.exists(target) and not overwrite:
        raise TransferVerificationError(
            f"{target!r} already exists; pass --overwrite to replace it deliberately"
        )
    parent = os.path.dirname(target) or os.sep
    if not os.path.isdir(parent):
        raise TransferInputError(f"download directory does not exist: {parent!r}")

    partial = f"{target}{_PARTIAL_SUFFIX}-{os.getpid()}-{secrets.token_hex(6)}"
    complete = False
    try:
        written, observed = _stream_download(url, partial, timeout, expected_size)
        if expected_size is not None and written != expected_size:
            raise TransferVerificationError(
                f"downloaded {written} bytes, but {expected_size} bytes were expected"
            )
        if expected_digest is not None and observed != expected_digest:
            raise TransferVerificationError(
                f"downloaded artifact SHA-256 is {observed}, but {expected_digest} was expected"
            )
        if overwrite:
            os.replace(partial, target)
        else:
            # A sibling hard link atomically fails if the final path now exists.
            # An exists() check followed by replace() would overwrite a racing file.
            try:
                os.link(partial, target)
            except FileExistsError as exc:
                raise TransferVerificationError(
                    f"{target!r} appeared during the download; refusing to replace it"
                ) from exc
            except OSError as exc:
                raise TransferError(f"could not materialize {target!r}: {_safe_text(exc)}") from exc
            _remove_quietly(partial)
        complete = True
        _fsync_directory(parent)
    finally:
        if not complete:
            _remove_quietly(partial)
    return {"path": target, "size_bytes": str(written), "sha256": observed}


def upload(
    url: str,
    source: str,
    *,
    if_none_match: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, str]:
    """Stream one local artifact to a presigned PUT URL and report its digest."""

    validate_presigned_url(url)
    origin = validate_worker_path(source, label="upload source")
    if not os.path.isfile(origin):
        raise TransferInputError(f"upload source is not a regular file: {origin!r}")
    try:
        with open(origin, "rb") as body:
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
            )
        else:
            fields = upload(presigned, options.source, if_none_match=WAVCSE_IF_NONE_MATCH)
    except TransferError as exc:
        err.write(f"{ERROR_KEY}\t{_safe_text(exc)}\n")
        return 1
    out.write(_render(operation, fields))
    return 0


def _stream_download(
    url: str,
    partial: str,
    timeout: float,
    expected_size: int | None,
) -> tuple[int, str]:
    """Copy the response body into the temporary file and return its size and digest."""

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
            with open(partial, "wb") as sink:
                while True:
                    chunk = response.read(CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk)
                    written += len(chunk)
                    digest.update(chunk)
                sink.flush()
                os.fsync(sink.fileno())
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
        "--overwrite",
        action="store_true",
        help="Replace an existing destination file deliberately.",
    )
    send = subcommands.add_parser(UPLOAD_OPERATION, help="Upload one local artifact.")
    send.add_argument("--source", required=True, help="Absolute source file path.")
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


def _content_length(response: object) -> int | None:
    headers = getattr(response, "headers", None)
    value = headers.get("Content-Length") if headers is not None else None
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
