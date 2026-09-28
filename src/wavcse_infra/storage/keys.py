"""Artifact key validation for the configured S3 namespace.

Every ordinary storage operation accepts a key that is relative to the configured
bucket prefix. This module is the single place that decides what a safe key is, so the
configuration model, the S3 service, and the manifest model cannot disagree.

The rules are deliberately strict rather than helpful. A key is either unambiguous or
it is rejected with an actionable message; nothing is silently rewritten, because a
normalized-but-different key would address a different object than the caller asked for.
"""

from __future__ import annotations

from wavcse_infra.errors import StorageKeyError

MAX_KEY_BYTES = 1024
_FORBIDDEN_CHARACTERS = frozenset("\\?#")
_DELETE_CHARACTER = 127
_PRINTABLE_ASCII_START = 32
_AMBIGUOUS_SEGMENTS = frozenset({"", ".", ".."})


def normalize_prefix(prefix: str) -> str:
    """Return the canonical namespace prefix without S3 boundary slashes."""

    normalized = prefix.strip().strip("/")
    if not normalized:
        raise StorageKeyError("storage.prefix must not be empty")
    return _validate_relative(normalized, "storage.prefix")


def validate_object_key(key: str) -> str:
    """Return a safe artifact key relative to the configured prefix."""

    return _validate_relative(key, "artifact key")


def validate_key_prefix(key_prefix: str) -> str:
    """Validate a listing prefix; an empty value means the whole configured namespace."""

    if key_prefix == "":
        return ""
    return _validate_relative(key_prefix, "artifact prefix", allow_trailing_slash=True)


def resolve_object_key(prefix: str, key: str) -> str:
    """Resolve one artifact key inside the configured prefix without namespace escape."""

    canonical_prefix = normalize_prefix(prefix)
    relative = validate_object_key(key)
    _reject_repeated_prefix(canonical_prefix, relative)
    return _bounded(f"{canonical_prefix}/{relative}", "artifact key")


def resolve_key_prefix(prefix: str, key_prefix: str) -> str:
    """Resolve a listing prefix inside the configured namespace."""

    canonical_prefix = normalize_prefix(prefix)
    relative = validate_key_prefix(key_prefix)
    if not relative:
        return f"{canonical_prefix}/"
    _reject_repeated_prefix(canonical_prefix, relative)
    return _bounded(f"{canonical_prefix}/{relative}", "artifact prefix")


def _validate_relative(
    value: str,
    label: str,
    *,
    allow_trailing_slash: bool = False,
) -> str:
    if value != value.strip():
        raise StorageKeyError(f"{label} must not start or end with whitespace: {value!r}")
    if not value:
        raise StorageKeyError(f"{label} must not be empty")
    if value.startswith("/"):
        raise StorageKeyError(
            f"{label} must be relative to the configured prefix; remove the leading "
            f"separator from {value!r}"
        )
    if "://" in value:
        raise StorageKeyError(
            f"{label} must not be a bucket or URL; pass the key relative to the configured prefix"
        )
    body = value[:-1] if allow_trailing_slash and value.endswith("/") else value
    for character in body:
        if character in _FORBIDDEN_CHARACTERS:
            raise StorageKeyError(
                f"{label} must not contain {character!r}: {value!r}; keys are plain S3 "
                "object paths, not URLs"
            )
        if character.isspace():
            raise StorageKeyError(
                f"{label} must not contain whitespace: {value!r}; use an unambiguous name"
            )
        if ord(character) < _PRINTABLE_ASCII_START or ord(character) == _DELETE_CHARACTER:
            raise StorageKeyError(f"{label} must not contain control characters: {value!r}")
    for segment in body.split("/"):
        if segment in _AMBIGUOUS_SEGMENTS:
            raise StorageKeyError(
                f"{label} must not contain empty, '.' or '..' path segments: {value!r}; "
                "pass a normalized relative key such as 'embeddings/v1/name.tar'"
            )
    return value


def _reject_repeated_prefix(canonical_prefix: str, relative: str) -> None:
    prefix_segments = canonical_prefix.split("/")
    relative_segments = relative.split("/")
    if relative_segments[: len(prefix_segments)] == prefix_segments:
        raise StorageKeyError(
            f"{relative!r} already begins with the configured prefix {canonical_prefix!r}; "
            "pass the key relative to that prefix instead"
        )


def _bounded(resolved: str, label: str) -> str:
    size = len(resolved.encode("utf-8"))
    if size > MAX_KEY_BYTES:
        raise StorageKeyError(
            f"Resolved {label} is {size} bytes; S3 object keys must not exceed "
            f"{MAX_KEY_BYTES} bytes"
        )
    return resolved
