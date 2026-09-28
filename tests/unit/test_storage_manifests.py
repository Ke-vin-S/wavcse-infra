import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from wavcse_infra.errors import StorageKeyError, StorageVerificationError
from wavcse_infra.storage.manifests import (
    MANIFEST_SCHEMA_VERSION,
    ArtifactManifest,
    embedding_archive_key,
    embedding_manifest_key,
    embedding_version_directory,
    load_manifest_json,
    manifest_json,
)

SHA256 = "a" * 64
COMMIT = "b" * 40
CREATED_AT = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _manifest(**overrides: object) -> ArtifactManifest:
    values: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact_name": "wavcse-base-v1-minpool-voxceleb",
        "artifact_type": "embeddings-archive",
        "dataset": "voxceleb",
        "object_key": "embeddings/wavcse-base-v1-minpool/voxceleb-minpooling.tar",
        "size_bytes": 21474836480,
        "sha256": SHA256,
        "created_at": CREATED_AT,
        "generator_git_commit": COMMIT,
        "extracted_destination": "datasets/voxceleb/minpooling",
        "metadata": {"pooling": "minpooling", "sample_rate": "16000"},
        "notes": "Generated from the wavCSE embedding pipeline.",
    }
    values.update(overrides)
    return ArtifactManifest.model_validate(values)


def test_valid_v1_manifest_round_trips_through_json() -> None:
    manifest = _manifest()

    restored = load_manifest_json(manifest_json(manifest))

    assert restored == manifest
    assert restored.schema_version == 1


def test_manifest_serialization_is_deterministic() -> None:
    first = _manifest(metadata={"b": "2", "a": "1"})
    second = _manifest(metadata={"a": "1", "b": "2"})

    assert manifest_json(first) == manifest_json(second)
    assert manifest_json(first).endswith("\n")


def test_manifest_json_is_stable_under_reload() -> None:
    manifest = _manifest()

    assert manifest_json(load_manifest_json(manifest_json(manifest))) == manifest_json(manifest)


def test_manifest_omits_unknown_reproducibility_metadata() -> None:
    manifest = _manifest(generator_git_commit=None, extracted_destination=None, notes=None)

    payload = json.loads(manifest_json(manifest))

    assert "generator_git_commit" not in payload
    assert "extracted_destination" not in payload
    assert "notes" not in payload
    assert "presigned_url" not in payload


def test_created_at_requires_a_timezone_and_normalizes_to_utc() -> None:
    with pytest.raises(ValidationError):
        _manifest(created_at=datetime(2026, 9, 28, 12, 0))

    offset = timezone(timedelta(hours=-5))
    manifest = _manifest(created_at=datetime(2026, 9, 28, 7, 0, tzinfo=offset))

    assert manifest.created_at == CREATED_AT
    assert manifest.created_at.tzinfo is not None


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "abc",
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "a" * 64 + " ",
    ],
)
def test_invalid_sha256_is_rejected(digest: str) -> None:
    with pytest.raises(ValidationError):
        _manifest(sha256=digest)


def test_uppercase_sha256_is_normalized() -> None:
    manifest = _manifest(sha256="A" * 64)

    assert manifest.sha256 == "a" * 64


@pytest.mark.parametrize(
    "key",
    ["/abs/path.tar", "../escape.tar", "embeddings//double.tar", "embeddings/"],
)
def test_invalid_object_key_is_rejected(key: str) -> None:
    with pytest.raises(ValidationError) as error:
        _manifest(object_key=key)

    assert "object_key" in str(error.value)


@pytest.mark.parametrize("commit", ["main", "abc123", "b" * 41, "z" * 40])
def test_branch_names_are_not_accepted_as_generator_commits(commit: str) -> None:
    with pytest.raises(ValidationError):
        _manifest(generator_git_commit=commit)


def test_schema_version_must_be_one() -> None:
    with pytest.raises(ValidationError):
        _manifest(schema_version=2)
    with pytest.raises(StorageVerificationError):
        load_manifest_json(manifest_json(_manifest()).replace('"schema_version": 1,', ""))


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _manifest(presigned_url="https://example.invalid/object?X-Amz-Signature=deadbeef")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("notes", "temporary URL https://bucket.s3.amazonaws.com/k?X-Amz-Signature=deadbeef"),
        ("extracted_destination", "/workspace?X-Amz-Credential=AKIAEXAMPLE"),
    ],
)
def test_bearer_material_is_rejected_in_text_fields(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as error:
        _manifest(**{field: value})

    assert "bearer" in str(error.value)


def test_bearer_material_is_rejected_in_metadata() -> None:
    with pytest.raises(ValidationError):
        _manifest(metadata={"url": "X-Amz-Security-Token=example"})
    with pytest.raises(ValidationError):
        _manifest(metadata={"x-amz-signature=secret": "value"})
    with pytest.raises(ValidationError):
        _manifest(notes="aws_secret_access_key=secret")


def test_metadata_must_map_non_empty_text_to_non_empty_text() -> None:
    with pytest.raises(ValidationError):
        _manifest(metadata={"": "value"})
    with pytest.raises(ValidationError):
        _manifest(metadata={"key": ""})


@pytest.mark.parametrize("text", ["not json", "[]", "{}", '{"schema_version": 9}'])
def test_invalid_manifest_documents_fail_actionably(text: str) -> None:
    with pytest.raises(StorageVerificationError):
        load_manifest_json(text)


def test_embedding_conventions_build_canonical_dataset_archives() -> None:
    version = "wavcse-base-v1-minpool"

    assert embedding_version_directory(version) == f"embeddings/{version}"
    assert embedding_manifest_key(version, "voxceleb") == (
        f"embeddings/{version}/voxceleb-minpooling.manifest.json"
    )
    assert embedding_archive_key(version, "voxceleb") == (
        f"embeddings/{version}/voxceleb-minpooling.tar"
    )
    assert embedding_archive_key(version, "keyword-spotting") == (
        f"embeddings/{version}/keyword-spotting-minpooling.tar"
    )
    assert embedding_archive_key(version, "emotion-recognition", pooling="meanpool") == (
        f"embeddings/{version}/emotion-recognition-meanpool.tar"
    )


@pytest.mark.parametrize("name", ["nested/dataset", "../escape", "", "/abs"])
def test_embedding_names_must_be_single_safe_segments(name: str) -> None:
    with pytest.raises(StorageKeyError):
        embedding_archive_key("wavcse-base-v1-minpool", name)
