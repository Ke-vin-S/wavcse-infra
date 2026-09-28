import inspect

import pytest
from pydantic import ValidationError

from wavcse_infra.config import Settings
from wavcse_infra.errors import StorageKeyError
from wavcse_infra.storage.keys import (
    MAX_KEY_BYTES,
    normalize_prefix,
    resolve_key_prefix,
    resolve_object_key,
    validate_key_prefix,
    validate_object_key,
)
from wavcse_infra.storage.s3 import S3Storage

PREFIX = "wavcse"


def test_key_resolves_beneath_the_configured_prefix() -> None:
    assert resolve_object_key(PREFIX, "embeddings/v1/voxceleb.tar") == (
        "wavcse/embeddings/v1/voxceleb.tar"
    )


def test_prefix_boundary_separators_are_canonicalized_once() -> None:
    assert normalize_prefix("/wavcse/") == "wavcse"
    assert resolve_object_key("/wavcse/", "embeddings/a.tar") == "wavcse/embeddings/a.tar"
    assert resolve_object_key("wavcse", "embeddings/a.tar") == "wavcse/embeddings/a.tar"


def test_multi_segment_prefix_is_supported() -> None:
    assert resolve_object_key("wavcse/data", "embeddings/a.tar") == ("wavcse/data/embeddings/a.tar")


@pytest.mark.parametrize(
    "key",
    [
        "../foo",
        "../../foo",
        "embeddings/../foo",
        "./foo",
        "embeddings/./foo",
        "/foo",
        "//foo",
        "embeddings//foo.tar",
        "embeddings/",
        "embeddings///foo",
    ],
)
def test_unsafe_or_ambiguous_keys_are_rejected(key: str) -> None:
    with pytest.raises(StorageKeyError) as error:
        resolve_object_key(PREFIX, key)

    message = str(error.value)
    assert "must" in message
    assert "relative" in message or "segment" in message or "separator" in message


def test_key_never_silently_rewrites_what_the_caller_asked_for() -> None:
    with pytest.raises(StorageKeyError):
        validate_object_key("embeddings//v1/foo.tar")


@pytest.mark.parametrize(
    "key",
    [
        "s3://other-bucket/foo.tar",
        "https://example.invalid/foo.tar",
        "wavcse",
    ],
)
def test_bucket_urls_and_repeated_prefixes_are_rejected(key: str) -> None:
    with pytest.raises(StorageKeyError):
        resolve_object_key(PREFIX, key)


def test_repeated_multi_segment_prefix_is_rejected() -> None:
    with pytest.raises(StorageKeyError) as error:
        resolve_object_key("wavcse/data", "wavcse/data/embeddings/a.tar")

    assert "configured prefix" in str(error.value)


def test_a_key_that_merely_shares_a_textual_prefix_is_accepted() -> None:
    assert resolve_object_key(PREFIX, "wavcse-notes/a.txt") == "wavcse/wavcse-notes/a.txt"


@pytest.mark.parametrize(
    "key",
    [
        "embeddings/foo bar.tar",
        "embeddings/foo\tbar.tar",
        "embeddings/foo\nbar.tar",
        "embeddings/foo\x00bar.tar",
        " embeddings/foo.tar",
        "embeddings/foo.tar ",
    ],
)
def test_whitespace_and_control_characters_are_rejected(key: str) -> None:
    with pytest.raises(StorageKeyError):
        validate_object_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "embeddings/foo?versionId=1",
        "embeddings/foo#fragment",
        "embeddings\\foo",
    ],
)
def test_url_syntax_inside_a_key_is_rejected(key: str) -> None:
    with pytest.raises(StorageKeyError):
        validate_object_key(key)


def test_oversized_resolved_key_is_rejected() -> None:
    with pytest.raises(StorageKeyError) as error:
        resolve_object_key(PREFIX, "a" * MAX_KEY_BYTES)

    assert "1024 bytes" in str(error.value)


def test_listing_prefix_may_end_at_a_segment_boundary() -> None:
    assert resolve_key_prefix(PREFIX, "") == "wavcse/"
    assert resolve_key_prefix(PREFIX, "embeddings/") == "wavcse/embeddings/"
    assert resolve_key_prefix(PREFIX, "embeddings") == "wavcse/embeddings"
    assert validate_key_prefix("") == ""


@pytest.mark.parametrize("prefix", ["", "   ", "/", "..", "wavcse/../escape", "wavcse//data"])
def test_invalid_namespace_prefix_is_rejected(prefix: str) -> None:
    with pytest.raises(StorageKeyError):
        normalize_prefix(prefix)


@pytest.mark.parametrize("prefix", ["../escape", "wavcse//data", ""])
def test_configuration_validation_rejects_an_unsafe_prefix(prefix: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"storage": {"prefix": prefix}})


def test_configuration_stores_the_canonical_prefix() -> None:
    settings = Settings.model_validate(
        {"storage": {"bucket": "private-wavcse", "prefix": "/wavcse/"}}
    )

    assert settings.storage.prefix == "wavcse"


def test_storage_operations_cannot_select_a_bucket_or_absolute_key() -> None:
    """The storage surface exposes no per-operation bucket or absolute-key escape."""

    for name in ("object_metadata", "presign_download", "presign_upload", "verify_object"):
        parameters = inspect.signature(getattr(S3Storage, name)).parameters
        assert "bucket" not in parameters
        assert "absolute" not in parameters

    assert "bucket" in inspect.signature(S3Storage.__init__).parameters
