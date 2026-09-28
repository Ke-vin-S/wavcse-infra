"""Versioned artifact manifests for canonical S3 storage.

An artifact manifest is the reviewable record of how one reusable object was produced.
It carries only facts that were actually known when the artifact was created; unknown
values stay absent instead of being guessed. It never contains bearer material such as a
presigned URL or any cloud credential.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from wavcse_infra.errors import StorageKeyError, StorageVerificationError
from wavcse_infra.redaction import contains_bearer_material
from wavcse_infra.storage.keys import validate_object_key

MANIFEST_SCHEMA_VERSION: Final = 1
MANIFEST_SUFFIX: Final = ".manifest.json"
EMBEDDINGS_DIRECTORY: Final = "embeddings"
EMBEDDINGS_ARTIFACT_TYPE: Final = "embeddings-archive"
DEFAULT_POOLING_STRATEGY: Final = "minpooling"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


class ArtifactManifest(BaseModel):
    """Schema version 1 manifest describing one reusable artifact object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    artifact_name: str = Field(min_length=1, max_length=200)
    artifact_type: str = Field(min_length=1, max_length=80)
    dataset: str | None = Field(default=None, min_length=1, max_length=200)
    object_key: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: datetime
    generator_git_commit: str | None = None
    extracted_destination: str | None = Field(default=None, min_length=1, max_length=1024)
    notes: str | None = Field(default=None, max_length=4096)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("object_key")
    @classmethod
    def key_stays_relative(cls, value: str) -> str:
        """Store the key relative to the configured bucket prefix."""

        try:
            return validate_object_key(value)
        except StorageKeyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("sha256")
    @classmethod
    def sha256_is_canonical(cls, value: str) -> str:
        """Require a canonical lowercase hexadecimal SHA-256 digest."""

        normalized = value.lower()
        if not _SHA256_PATTERN.fullmatch(normalized):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return normalized

    @field_validator("generator_git_commit")
    @classmethod
    def git_commit_is_full_object_id(cls, value: str | None) -> str | None:
        """Accept only a full SHA-1 or SHA-256 Git object ID, never a branch name."""

        if value is None:
            return None
        normalized = value.strip().lower()
        if not _GIT_COMMIT_PATTERN.fullmatch(normalized):
            raise ValueError("generator_git_commit must be a full 40 or 64 character commit ID")
        return normalized

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        """Reject naive timestamps, which cannot identify when an artifact was made."""

        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone offset")
        return value.astimezone(UTC)

    @field_validator("metadata")
    @classmethod
    def metadata_is_flat_text(cls, value: dict[str, str]) -> dict[str, str]:
        """Keep manifest metadata small, flat, and free of empty entries."""

        for key, entry in value.items():
            if not key or not isinstance(entry, str) or not entry:
                raise ValueError("metadata must map non-empty string keys to non-empty strings")
        return value

    @model_validator(mode="after")
    def contains_no_bearer_material(self) -> ArtifactManifest:
        """Refuse to persist presigned URL or credential material in any text field."""

        for location, text in _text_values(self.model_dump(mode="json")):
            if contains_bearer_material(text):
                raise ValueError(
                    f"{location} contains presigned URL or credential material; manifests "
                    "must never store bearer values"
                )
        return self


def manifest_json(manifest: ArtifactManifest) -> str:
    """Serialize a manifest deterministically for version-controlled artifact metadata."""

    payload = manifest.model_dump(mode="json", exclude_none=True)
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_manifest_json(text: str) -> ArtifactManifest:
    """Parse and validate a manifest document, failing with an actionable message."""

    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StorageVerificationError(f"Artifact manifest is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StorageVerificationError("Artifact manifest must be a JSON object")
    try:
        return ArtifactManifest.model_validate(payload)
    except ValidationError as exc:
        raise StorageVerificationError(
            "Artifact manifest is invalid: " + _format_validation_error(exc)
        ) from exc


def embedding_version_directory(embedding_version: str) -> str:
    """Return the relative directory holding one embedding version."""

    return validate_object_key(f"{EMBEDDINGS_DIRECTORY}/{_segment(embedding_version)}")


def embedding_manifest_key(
    embedding_version: str,
    dataset: str,
    *,
    pooling: str = DEFAULT_POOLING_STRATEGY,
) -> str:
    """Return the sidecar manifest key for one dataset archive."""

    archive = embedding_archive_key(embedding_version, dataset, pooling=pooling)
    return validate_object_key(f"{archive.removesuffix('.tar')}{MANIFEST_SUFFIX}")


def embedding_archive_key(
    embedding_version: str,
    dataset: str,
    *,
    pooling: str = DEFAULT_POOLING_STRATEGY,
) -> str:
    """Return the relative dataset archive key for one embedding version.

    The archive format is a plain TAR so the extracted layout stays identical to what
    wavCSE already expects; compression is a later, benchmark-driven decision.
    """

    directory = embedding_version_directory(embedding_version)
    return validate_object_key(f"{directory}/{_segment(dataset)}-{_segment(pooling)}.tar")


def _segment(value: str) -> str:
    """Validate one path segment used to build an embedding key."""

    segment = validate_object_key(value)
    if "/" in segment:
        raise StorageKeyError(
            f"{value!r} must be a single path segment, not a nested key; embedding names "
            "cannot change the canonical layout"
        )
    return segment


def _text_values(value: object, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, dict):
        entries: list[tuple[str, str]] = []
        for key, item in value.items():
            location = f"{prefix}.{key}" if prefix else str(key)
            entries.append((location, str(key)))
            entries.extend(_text_values(item, location))
        return entries
    if isinstance(value, list):
        return [
            entry
            for index, item in enumerate(value)
            for entry in _text_values(item, f"{prefix}[{index}]")
        ]
    return []


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
    return "; ".join(details)
