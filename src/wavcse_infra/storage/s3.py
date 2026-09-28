"""Boto3-backed canonical S3 storage constrained to one bucket and prefix.

Ordinary storage operations never accept a bucket or an absolute key. The controller
resolves every caller-supplied key beneath the configured prefix, so a typo or a
malicious input cannot read or write an unrelated part of the account. Credentials come
from Boto3's normal provider chain; on the controller this is the attached EC2 instance
profile. No credential value is ever read, serialized, or handed to a worker.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Literal, Protocol

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import BaseModel, ConfigDict, Field

from wavcse_infra.config import Settings
from wavcse_infra.errors import (
    ConfigurationError,
    StorageError,
    StorageKeyError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StoragePermissionError,
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
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 1000
MAX_MANIFEST_BYTES = 1024 * 1024
_LIST_PAGE_SIZE = 1000

DEFAULT_VERIFICATION_LIMITATIONS: tuple[str, ...] = (
    "Object content was not downloaded, so a recorded SHA-256 is not cryptographically "
    "confirmed by this command.",
    "An S3 ETag is not a SHA-256 checksum and is never compared against the recorded digest.",
    "Durability is inferred from object metadata and storage class, not from reading the body.",
)

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})
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


class StorageVerification(BaseModel):
    """Result of a metadata-level verification of one stored artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    etag: str | None = None
    last_modified: datetime | None = None
    expected_size_checked: bool = False
    manifest_checked: bool = False
    manifest_sha256: str | None = None
    content_checksum_verified: Literal[False] = False
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
    ) -> None:
        self._bucket = bucket
        self._prefix = normalize_prefix(prefix)
        self._client = client
        self._presign_expiry_seconds = presign_expiry_seconds

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
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

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
        except (ClientError, BotoCoreError, ValueError) as exc:
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
        return expiry

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
    return StoredObject(
        key=key,
        size_bytes=size,
        etag=_etag(item.get("ETag")),
        last_modified=_timestamp(item.get("LastModified")),
        storage_class=storage_class if isinstance(storage_class, str) else None,
    )


def _etag(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.strip('"')


def _timestamp(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


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
    return StorageError(f"Could not {description} s3://{key} (HTTP {status or 'unknown'}, {code})")


def _credentials_failure() -> StorageError:
    return ConfigurationError(
        "AWS credentials are not available for S3; attach the controller EC2 instance "
        "profile instead of installing static access keys"
    )


def _transport_failure(description: str, key: str, exc: BotoCoreError) -> StorageError:
    return StorageError(f"Could not {description} s3://{key}: {_sanitized(exc)}")


def _sanitized(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:500]


def _botocore_config(timeout_seconds: float) -> BotocoreConfig:
    return BotocoreConfig(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"mode": "standard", "max_attempts": 3},
        signature_version="v4",
    )
