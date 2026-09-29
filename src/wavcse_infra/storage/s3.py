"""Boto3-backed canonical S3 storage constrained to one bucket and prefix.

Ordinary storage operations never accept a bucket or an absolute key. The controller
resolves every caller-supplied key beneath the configured prefix, so a typo or a
malicious input cannot read or write an unrelated part of the account. Credentials come
from Boto3's normal provider chain; on the controller this is the attached EC2 instance
profile. No credential value is ever read, serialized, or handed to a worker.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Protocol

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ParamValidationError,
)
from pydantic import BaseModel, ConfigDict, Field

from wavcse_infra.config import Settings
from wavcse_infra.errors import (
    ConfigurationError,
    StorageError,
    StorageKeyError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StoragePermissionError,
    StorageUnavailableError,
    StorageVerificationError,
)
from wavcse_infra.redaction import redact
from wavcse_infra.storage.keys import (
    normalize_prefix,
    resolve_key_prefix,
    resolve_object_key,
    validate_object_key,
)
from wavcse_infra.storage.manifests import (
    ArtifactManifest,
    load_manifest_json,
)

DEFAULT_PRESIGN_EXPIRY_SECONDS = 3600
MIN_PRESIGN_EXPIRY_SECONDS = 60
# Signature Version 4 refuses a presigned URL lifetime above seven days.
MAX_PRESIGN_EXPIRY_SECONDS = 604800
# A URL signed with the controller's temporary credentials dies when the session token
# does too, so the signed lifetime is capped by the credential's own remaining validity and
# this margin is kept clear of both boundaries.
PRESIGN_CREDENTIAL_MARGIN_SECONDS = 30
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000
MAX_MANIFEST_BYTES = 1024 * 1024
# An evidence document (a job manifest, a metrics text file) is small by construction, so a
# read-back is capped: a caller inspecting evidence must never be able to pull a training
# artifact through this path by asking for a larger buffer.
MAX_READABLE_EVIDENCE_BYTES = 16 * 1024 * 1024
_LIST_PAGE_SIZE = 1000
# Reading an object body to prove its bytes must not buffer it: a declared output can be
# large, so the body is streamed through one hash in bounded chunks.
CONTENT_HASH_CHUNK_BYTES = 8 * 1024 * 1024

DEFAULT_VERIFICATION_LIMITATIONS: tuple[str, ...] = (
    "Object content was not downloaded, so a recorded SHA-256 is not cryptographically "
    "confirmed by this command.",
    "An S3 ETag is not a SHA-256 checksum and is never compared against the recorded digest.",
    "Durability is inferred from object metadata and storage class, not from reading the body.",
)

CONTENT_VERIFICATION_LIMITATIONS: tuple[str, ...] = (
    "The object body was read once and hashed, and the read was bound to the version the "
    "metadata read reported when the bucket provided one; a replacement after that read is "
    "not detected by this verification.",
    "Durability is inferred from object metadata and storage class, not from reading the body.",
)

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
# Server-side, throttling, and connection codes are observations about the service rather
# than about the object, so they must never be reported as a failed artifact. A generic
# malformed-request 400 is deliberately absent: only a named retryable condition is
# transient, because a request S3 rejects as invalid will be rejected identically forever.
_TRANSIENT_CODES = frozenset(
    {
        "408",
        "429",
        "500",
        "502",
        "503",
        "504",
        "500InternalError",
        "InternalError",
        "RequestTimeout",
        "RequestTimeTooSkewed",
        "ServiceUnavailable",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "TooManyRequests",
    }
)
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_PERMISSION_CODES = frozenset(
    {"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"}
)
_NOT_FOUND_STATUS = 404
_PERMISSION_STATUS = 403


class S3Client(Protocol):
    """Minimal Boto3 S3 client surface used by this module."""

    def list_objects_v2(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def generate_presigned_url(self, operation_name: str, **kwargs: Any) -> str: ...


class StoredObject(BaseModel):
    """Normalized metadata for one stored S3 object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    etag: str | None = None
    last_modified: datetime | None = None
    storage_class: str | None = None
    # Present only when the bucket is versioned; it is what binds a later body read to the
    # exact version a metadata read observed.
    version_id: str | None = None


class StorageVerification(BaseModel):
    """Result of a verification of one stored artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    etag: str | None = None
    last_modified: datetime | None = None
    expected_size_checked: bool = False
    manifest_checked: bool = False
    manifest_sha256: str | None = None
    content_checksum_verified: bool = False
    content_sha256: str | None = None
    limitations: tuple[str, ...] = DEFAULT_VERIFICATION_LIMITATIONS


@dataclass(frozen=True)
class PresignedUrl:
    """One object-scoped, operation-scoped, expiring bearer URL.

    The URL is the secret. Its representation is intentionally redacted so that an
    accidental log statement, exception message, or state write cannot leak the
    signature; callers must use `reveal()` to return it as an explicit result.
    """

    operation: Literal["get", "put"]
    bucket: str
    key: str
    expires_in_seconds: int
    url: str
    if_none_match: bool = False

    def reveal(self) -> str:
        """Return the complete URL for the one caller that was asked to return it."""

        return self.url

    def __repr__(self) -> str:
        return (
            f"PresignedUrl(operation={self.operation!r}, bucket={self.bucket!r}, "
            f"key={self.key!r}, expires_in_seconds={self.expires_in_seconds}, "
            f"if_none_match={self.if_none_match}, url=<redacted>)"
        )


class S3Storage:
    """Object-level operations for the configured canonical artifact namespace."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        client: S3Client,
        presign_expiry_seconds: int = DEFAULT_PRESIGN_EXPIRY_SECONDS,
        credential_expiry: Callable[[], datetime | None] | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = normalize_prefix(prefix)
        self._client = client
        self._presign_expiry_seconds = presign_expiry_seconds
        self._credential_expiry = credential_expiry

    @classmethod
    def from_settings(cls, settings: Settings) -> S3Storage:
        """Build a client from Boto3's normal credential chain, without reading secrets."""

        bucket = settings.storage.bucket
        if bucket is None:
            raise ConfigurationError(
                "storage.bucket is not configured; set it in the user configuration file "
                "or set WAVCSE_INFRA_S3_BUCKET"
            )
        region = settings.aws.region
        if region is None:
            raise ConfigurationError(
                "aws.region is not configured; S3 requests must be signed for the bucket "
                "region. Set it in the user configuration file or set WAVCSE_INFRA_AWS_REGION"
            )
        session = boto3.Session(region_name=region)
        client: S3Client = session.client(
            "s3",
            config=_botocore_config(settings.runpod.request_timeout_seconds),
        )
        return cls(
            bucket=bucket,
            prefix=settings.storage.prefix,
            client=client,
            presign_expiry_seconds=settings.storage.presign_expiry_seconds,
            credential_expiry=_credential_expiry_reader(_session_credentials(session)),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    @property
    def presign_expiry_seconds(self) -> int:
        """Return the default lifetime of a URL this storage generates.

        Callers that must outlive a single request (a bounded transfer attempt) derive
        their own bound from this value so a URL can never expire mid-attempt.
        """

        return self._presign_expiry_seconds

    def object_key(self, key: str) -> str:
        """Resolve one artifact key relative to the configured prefix."""

        return resolve_object_key(self._prefix, key)

    def list_objects(
        self,
        key_prefix: str = "",
        *,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[StoredObject]:
        """List stored objects beneath the configured namespace, bounded by `limit`."""

        if not 1 <= limit <= MAX_LIST_LIMIT:
            raise StorageError(f"List limit must be between 1 and {MAX_LIST_LIMIT}")
        resolved_prefix = resolve_key_prefix(self._prefix, key_prefix)
        objects: list[StoredObject] = []
        continuation: str | None = None
        while len(objects) < limit:
            parameters: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": resolved_prefix,
                "MaxKeys": min(limit - len(objects), _LIST_PAGE_SIZE),
            }
            if continuation is not None:
                parameters["ContinuationToken"] = continuation
            response = self._call(
                "list objects",
                resolved_prefix,
                partial(self._client.list_objects_v2, **parameters),
            )
            contents = response.get("Contents") or ()
            objects.extend(_stored_object(item) for item in contents)
            if not response.get("IsTruncated"):
                break
            next_token = response.get("NextContinuationToken")
            if not isinstance(next_token, str) or not next_token:
                raise StorageError("S3 returned a truncated listing without a continuation token")
            if next_token == continuation:
                raise StorageError("S3 repeated a listing continuation token")
            continuation = next_token
        return objects[:limit]

    def object_metadata(self, key: str) -> StoredObject | None:
        """Return stored metadata, or None when the object does not exist."""

        resolved = self.object_key(key)
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=resolved)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise _storage_failure("read metadata for", resolved, exc) from exc
        except NoCredentialsError as exc:
            raise _credentials_failure() from exc
        except BotoCoreError as exc:
            raise _transport_failure("read metadata for", resolved, exc) from exc
        return _stored_object({**response, "Key": resolved})

    def object_exists(self, key: str) -> bool:
        """Return whether the object exists, using one metadata request."""

        return self.object_metadata(key) is not None

    def require_writable(self, key: str, *, overwrite: bool) -> None:
        """Refuse an implicit replacement of an existing persisted object."""

        if overwrite:
            return
        resolved = self.object_key(key)
        if self.object_metadata(key) is not None:
            raise StorageObjectExistsError(
                f"s3://{self._bucket}/{resolved} already exists; pass --overwrite to "
                "replace a persisted artifact deliberately"
            )

    def read_object_bytes(self, key: str, *, max_bytes: int = MAX_MANIFEST_BYTES) -> bytes:
        """Read one small object fully, refusing to buffer an unbounded body."""

        resolved = self.object_key(key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=resolved)
            body = response["Body"]
            try:
                payload = body.read(max_bytes + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except ClientError as exc:
            if _is_not_found(exc):
                raise StorageObjectNotFoundError(
                    f"S3 object s3://{self._bucket}/{resolved} does not exist"
                ) from exc
            raise _storage_failure("read", resolved, exc) from exc
        except NoCredentialsError as exc:
            raise _credentials_failure() from exc
        except BotoCoreError as exc:
            raise _transport_failure("read", resolved, exc) from exc
        if len(payload) > max_bytes:
            raise StorageVerificationError(
                f"S3 object s3://{self._bucket}/{resolved} exceeds the {max_bytes} byte limit "
                "for small metadata objects"
            )
        return payload

    def read_manifest(self, key: str) -> ArtifactManifest:
        """Read and validate a versioned artifact manifest stored in the namespace."""

        payload = self.read_object_bytes(key, max_bytes=MAX_MANIFEST_BYTES)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StorageVerificationError(
                f"Artifact manifest at {self.object_key(key)} is not valid UTF-8"
            ) from exc
        return load_manifest_json(text)

    def presign_download(
        self,
        key: str,
        *,
        expires_in_seconds: int | None = None,
    ) -> PresignedUrl:
        """Generate one time-limited GET URL for an exact object."""

        return self._presign("get", "get_object", key, expires_in_seconds)

    def presign_upload(
        self,
        key: str,
        *,
        expires_in_seconds: int | None = None,
        overwrite: bool = False,
    ) -> PresignedUrl:
        """Generate one time-limited PUT URL for an exact object."""

        return self._presign(
            "put", "put_object", key, expires_in_seconds, if_none_match=not overwrite
        )

    def verify_object(
        self,
        key: str,
        *,
        expected_size: int | None = None,
        manifest: ArtifactManifest | None = None,
    ) -> StorageVerification:
        """Verify stored existence and metadata without downloading the object body."""

        resolved = self.object_key(key)
        relative = validate_object_key(key)
        stored = self.object_metadata(key)
        if stored is None:
            raise StorageObjectNotFoundError(
                f"S3 object s3://{self._bucket}/{resolved} does not exist"
            )
        if expected_size is not None and stored.size_bytes != expected_size:
            raise StorageVerificationError(
                f"s3://{self._bucket}/{resolved} is {stored.size_bytes} bytes, but "
                f"{expected_size} bytes were expected"
            )
        manifest_sha256: str | None = None
        if manifest is not None:
            manifest_sha256 = _verify_manifest(
                bucket=self._bucket,
                relative_key=relative,
                resolved_key=resolved,
                size_bytes=stored.size_bytes,
                expected_size=expected_size,
                manifest=manifest,
            )
        return StorageVerification(
            key=resolved,
            size_bytes=stored.size_bytes,
            etag=stored.etag,
            last_modified=stored.last_modified,
            expected_size_checked=expected_size is not None,
            manifest_checked=manifest is not None,
            manifest_sha256=manifest_sha256,
        )

    def verify_object_content(
        self,
        key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StorageVerification:
        """Read one stored object and prove its bytes, not just its metadata.

        Metadata cannot say what an object's bytes are: an object at the right key with the
        right size may be a different file entirely, so recording a digest from anywhere
        else would misattribute its provenance. This reads the object body once, streaming
        it through one SHA-256, and rejects anything whose bytes do not match. When the
        bucket reports a version id, the body read is bound to the exact version the
        metadata read observed, so an object replaced between the two reads is still
        verified against the bytes that were actually read.

        The body read costs one full transfer of the object; that is the price of a
        cryptographic claim about what canonical storage contains.
        """

        resolved = self.object_key(key)
        stored = self.object_metadata(key)
        if stored is None:
            raise StorageObjectNotFoundError(
                f"S3 object s3://{self._bucket}/{resolved} does not exist"
            )
        parameters: dict[str, Any] = {"Bucket": self._bucket, "Key": resolved}
        if stored.version_id is not None:
            parameters["VersionId"] = stored.version_id
        size, digest = self._hash_object_body(resolved, parameters)
        if expected_size is not None and size != expected_size:
            raise StorageVerificationError(
                f"s3://{self._bucket}/{resolved} contains {size} bytes, but {expected_size} "
                "bytes were expected"
            )
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise StorageVerificationError(
                f"s3://{self._bucket}/{resolved} does not contain the expected bytes: its "
                f"SHA-256 is {digest}, but {expected_sha256.lower()} was expected"
            )
        return StorageVerification(
            key=resolved,
            size_bytes=size,
            etag=stored.etag,
            last_modified=stored.last_modified,
            expected_size_checked=expected_size is not None,
            content_checksum_verified=True,
            content_sha256=digest,
            limitations=CONTENT_VERIFICATION_LIMITATIONS,
        )

    def _presign(
        self,
        operation: Literal["get", "put"],
        client_operation: str,
        key: str,
        expires_in_seconds: int | None,
        *,
        if_none_match: bool = False,
    ) -> PresignedUrl:
        resolved = self.object_key(key)
        expiry = self._validated_expiry(expires_in_seconds)
        parameters = {"Bucket": self._bucket, "Key": resolved}
        if if_none_match:
            parameters["IfNoneMatch"] = "*"
        try:
            url = self._client.generate_presigned_url(
                client_operation,
                Params=parameters,
                ExpiresIn=expiry,
            )
        except NoCredentialsError as exc:
            # No credential at all is a controller configuration problem, not an artifact
            # observation, so it stays definitive.
            raise _credentials_failure() from exc
        except ParamValidationError as exc:
            # Signing refused the request itself; sending it again cannot change that.
            raise StorageError(
                f"Could not generate a presigned {operation.upper()} URL for "
                f"s3://{self._bucket}/{resolved}: {_sanitized(exc)}"
            ) from exc
        except ClientError as exc:
            raise _storage_failure("presign", resolved, exc) from exc
        except BotoCoreError as exc:
            # A credential refresh, a transport error, or a service interruption during
            # signing says nothing about the object, so the caller must be free to retry
            # with fresh credentials rather than record a terminal failure.
            raise _transport_failure("presign", resolved, exc) from exc
        except ValueError as exc:
            raise StorageError(
                f"Could not generate a presigned {operation.upper()} URL for "
                f"s3://{self._bucket}/{resolved}: {_sanitized(exc)}"
            ) from exc
        return PresignedUrl(
            operation=operation,
            bucket=self._bucket,
            key=resolved,
            expires_in_seconds=expiry,
            url=url,
            if_none_match=if_none_match,
        )

    def _validated_expiry(self, expires_in_seconds: int | None) -> int:
        expiry = self._presign_expiry_seconds if expires_in_seconds is None else expires_in_seconds
        if not MIN_PRESIGN_EXPIRY_SECONDS <= expiry <= MAX_PRESIGN_EXPIRY_SECONDS:
            raise StorageKeyError(
                f"Presigned URL lifetime must be between {MIN_PRESIGN_EXPIRY_SECONDS} and "
                f"{MAX_PRESIGN_EXPIRY_SECONDS} seconds"
            )
        return self._credential_bounded_expiry(expiry)

    def _credential_bounded_expiry(self, requested: int) -> int:
        """Cap one requested URL lifetime by the signing credential's own remaining validity.

        A Signature Version 4 URL signed with temporary credentials stops working when the
        session token expires, whatever `ExpiresIn` asked for. A URL must therefore never
        claim more time than the credentials behind it can honour, or a transfer would fail
        with an authorization error partway through an attempt the controller believed was
        fully covered.
        """

        if self._credential_expiry is None:
            return requested
        expiry = self._credential_expiry()
        if expiry is None:
            return requested
        remaining = (expiry - datetime.now(UTC)).total_seconds()
        usable = int(remaining - PRESIGN_CREDENTIAL_MARGIN_SECONDS)
        if usable < MIN_PRESIGN_EXPIRY_SECONDS:
            raise StorageUnavailableError(
                f"Controller AWS credentials expire in {remaining:.0f} seconds, which is too "
                "soon to sign a usable transfer URL. Renew them (an attached instance "
                "profile refreshes automatically) and retry; nothing about the artifact is "
                "concluded from this"
            )
        return min(requested, usable)

    def _call(
        self,
        description: str,
        key: str,
        operation: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        try:
            return operation()
        except ClientError as exc:
            raise _storage_failure(description, key, exc) from exc
        except NoCredentialsError as exc:
            raise _credentials_failure() from exc
        except BotoCoreError as exc:
            raise _transport_failure(description, key, exc) from exc

    def _hash_object_body(
        self,
        resolved: str,
        parameters: Mapping[str, Any],
    ) -> tuple[int, str]:
        """Stream one object body through a SHA-256 and return its byte count and digest."""

        digest = hashlib.sha256()
        total = 0
        try:
            response = self._client.get_object(**parameters)
            body = response["Body"]
            try:
                while True:
                    chunk = body.read(CONTENT_HASH_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
        except ClientError as exc:
            if _is_not_found(exc):
                raise StorageObjectNotFoundError(
                    f"S3 object s3://{self._bucket}/{resolved} does not exist"
                ) from exc
            raise _storage_failure("read", resolved, exc) from exc
        except NoCredentialsError as exc:
            raise _credentials_failure() from exc
        except BotoCoreError as exc:
            raise _transport_failure("read", resolved, exc) from exc
        return total, digest.hexdigest()


def _verify_manifest(
    *,
    bucket: str,
    relative_key: str,
    resolved_key: str,
    size_bytes: int,
    expected_size: int | None,
    manifest: ArtifactManifest,
) -> str:
    """Require the manifest to describe exactly the object being verified.

    A manifest records the key relative to the configured namespace prefix, so the same
    manifest stays valid if the canonical bucket moves and keeps the same layout.
    """

    if manifest.object_key != relative_key:
        raise StorageVerificationError(
            f"Artifact manifest describes {manifest.object_key!r}, but this artifact key "
            f"resolves to s3://{bucket}/{resolved_key}"
        )
    if manifest.size_bytes != size_bytes:
        raise StorageVerificationError(
            f"Artifact manifest records {manifest.size_bytes} bytes for {resolved_key!r}, but "
            f"the stored object is {size_bytes} bytes"
        )
    if expected_size is not None and manifest.size_bytes != expected_size:
        raise StorageVerificationError(
            f"Artifact manifest records {manifest.size_bytes} bytes, but {expected_size} bytes "
            "were expected"
        )
    return manifest.sha256


def _stored_object(item: Mapping[str, Any]) -> StoredObject:
    key = item.get("Key")
    if not isinstance(key, str) or not key:
        raise StorageError("S3 returned an object entry without a key")
    size = item.get("Size", item.get("ContentLength"))
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise StorageError(f"S3 returned object metadata without a valid byte size for {key!r}")
    storage_class = item.get("StorageClass")
    version_id = item.get("VersionId")
    return StoredObject(
        key=key,
        size_bytes=size,
        etag=_etag(item.get("ETag")),
        last_modified=_timestamp(item.get("LastModified")),
        storage_class=storage_class if isinstance(storage_class, str) else None,
        version_id=version_id if isinstance(version_id, str) and version_id else None,
    )


def _etag(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.strip('"')


def _timestamp(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _session_credentials(session: object) -> object | None:
    """Read the session's credential object without assuming a particular boto3 shape."""

    getter = getattr(session, "get_credentials", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


def _credential_expiry_reader(credentials: object) -> Callable[[], datetime | None] | None:
    """Return a reader for the signing credentials' actual expiry, when they expire.

    Boto3's credential chain hides the authoritative expiry of temporary credentials: a
    static `Credentials` object has none at all, and a refreshable one keeps the expiry of
    the credentials it last obtained (after a refresh) on the refreshable object itself
    rather than on any public attribute. Reading a fixed attribute therefore silently
    reports "no expiry" for exactly the credentials that do expire, and a presigned URL
    would be issued with a lifetime the session token cannot honour.

    This adapter is the single place that resolves that expiry. It freezes the credentials
    first - which is also what triggers a refresh when the chain says one is needed, using
    the same path signing uses - and then reads the expiry of the credentials that would
    actually sign. Credentials that carry no expiry at all produce no reader and no cap:
    that covers static keys, and also a session token supplied directly through the
    environment, whose expiry botocore cannot know.
    """

    if credentials is None:
        return None
    read = _expiry_accessor(credentials)
    if read is None:
        return None

    def reader() -> datetime | None:
        freeze = getattr(credentials, "get_frozen_credentials", None)
        if callable(freeze):
            # A refresh failure is left to signing, which reports it with provider context.
            freeze()
        try:
            value = read()
        except Exception:  # pragma: no cover - a broken resolver must not break presigning
            return None
        return value if isinstance(value, datetime) else None

    return reader


def _expiry_accessor(credentials: object) -> Callable[[], object] | None:
    """Return how one credential object's own expiry is read, or None when it has none."""

    if isinstance(credentials, RefreshableCredentials):
        # The post-refresh expiry lives here; botocore exposes no public reader for it.
        return lambda: credentials._expiry_time
    if hasattr(credentials, "expiry_time"):
        return lambda: credentials.expiry_time
    return None


def _is_not_found(exc: ClientError) -> bool:
    error = exc.response.get("Error") or {}
    code = str(error.get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in _NOT_FOUND_CODES or status == _NOT_FOUND_STATUS


def _storage_failure(description: str, key: str, exc: ClientError) -> StorageError:
    error = exc.response.get("Error") or {}
    code = str(error.get("Code") or "unknown")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if code in _PERMISSION_CODES or status == _PERMISSION_STATUS:
        return StoragePermissionError(
            f"AWS identity is not permitted to {description} s3://{key} (HTTP "
            f"{status or 'unknown'}, {code}); verify the controller role policy"
        )
    if code in _TRANSIENT_CODES or status in _TRANSIENT_STATUSES or status is None:
        return StorageUnavailableError(
            f"Could not {description} s3://{key} (HTTP {status or 'unknown'}, {code}); this is "
            "a service observation failure, not evidence about the object"
        )
    return StorageError(f"Could not {description} s3://{key} (HTTP {status or 'unknown'}, {code})")


def _credentials_failure() -> StorageError:
    return ConfigurationError(
        "AWS credentials are not available for S3; attach the controller EC2 instance "
        "profile instead of installing static access keys"
    )


def _transport_failure(description: str, key: str, exc: BotoCoreError) -> StorageUnavailableError:
    return StorageUnavailableError(
        f"Could not {description} s3://{key}: {_sanitized(exc)}. This is a service "
        "observation failure, not evidence about the object"
    )


def _sanitized(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


def _botocore_config(timeout_seconds: float) -> BotocoreConfig:
    return BotocoreConfig(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"mode": "standard", "max_attempts": 3},
        signature_version="v4",
    )
