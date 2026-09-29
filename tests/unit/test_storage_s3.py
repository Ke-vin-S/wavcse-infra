import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from pydantic import ValidationError

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
from wavcse_infra.storage import s3 as s3_module
from wavcse_infra.storage.manifests import ArtifactManifest
from wavcse_infra.storage.s3 import (
    MAX_PRESIGN_EXPIRY_SECONDS,
    MIN_PRESIGN_EXPIRY_SECONDS,
    S3Storage,
    StoredObject,
)

BUCKET = "private-wavcse"
PREFIX = "wavcse"
KEY = "embeddings/v1/voxceleb-minpooling.tar"
RESOLVED_KEY = f"{PREFIX}/{KEY}"
PRESIGNED_URL = (
    "https://private-wavcse.s3.ap-south-1.amazonaws.com/wavcse/embeddings/v1/"
    "voxceleb-minpooling.tar?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=EXAMPLE%2F20260928%2Fap-south-1%2Fs3%2Faws4_request"
    "&X-Amz-Signature=deadbeefcafebabe"
)
LAST_MODIFIED = datetime(2026, 9, 28, 9, 30, tzinfo=UTC)


def client_error(code: str, status: int, operation: str = "HeadObject") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": f"synthetic {code}"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class FakeS3Client:
    """Minimal Boto3 S3 client double that records every request."""

    def __init__(
        self,
        *,
        pages: list[dict[str, object]] | None = None,
        head: dict[str, object] | None = None,
        error: Exception | None = None,
        presigned_url: str = PRESIGNED_URL,
        body: bytes = b"{}",
    ) -> None:
        self.pages = pages if pages is not None else [{}]
        self.head = head
        self.error = error
        self.presigned_url = presigned_url
        self.body = body
        self.presign_calls: list[tuple[str, dict[str, object], int]] = []
        self.list_calls: list[dict[str, object]] = []
        self.head_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        index = len(self.list_calls) - 1
        return self.pages[index] if index < len(self.pages) else {}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        self.head_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.head is None:
            raise client_error("404", 404)
        return self.head

    def get_object(self, **kwargs: object) -> dict[str, object]:
        self.get_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"Body": io.BytesIO(self.body)}

    def generate_presigned_url(
        self,
        operation_name: str,
        *,
        Params: dict[str, object],
        ExpiresIn: int,
    ) -> str:
        self.presign_calls.append((operation_name, Params, ExpiresIn))
        if self.error is not None:
            raise self.error
        return self.presigned_url


def storage(
    client: FakeS3Client,
    *,
    prefix: str = PREFIX,
    presign_expiry_seconds: int = 3600,
) -> S3Storage:
    return S3Storage(
        bucket=BUCKET,
        prefix=prefix,
        client=client,
        presign_expiry_seconds=presign_expiry_seconds,
    )


def head_response(size: int = 1024) -> dict[str, object]:
    return {
        "ContentLength": size,
        "ETag": '"5d41402abc4b2a76b9719d911017c592"',
        "LastModified": LAST_MODIFIED,
        "StorageClass": "STANDARD",
    }


def _manifest(**overrides: object) -> ArtifactManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_name": "voxceleb-minpooling",
        "artifact_type": "embeddings-archive",
        "dataset": "voxceleb",
        "object_key": KEY,
        "size_bytes": 1024,
        "sha256": "c" * 64,
        "created_at": LAST_MODIFIED,
    }
    values.update(overrides)
    return ArtifactManifest.model_validate(values)


def test_presign_download_is_object_scoped_and_uses_configured_expiry() -> None:
    client = FakeS3Client()
    presigned = storage(client, presign_expiry_seconds=1800).presign_download(KEY)

    assert client.presign_calls == [("get_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY}, 1800)]
    assert presigned.operation == "get"
    assert presigned.bucket == BUCKET
    assert presigned.key == RESOLVED_KEY
    assert presigned.expires_in_seconds == 1800
    assert presigned.reveal() == PRESIGNED_URL


def test_presign_upload_is_object_scoped_and_uses_put() -> None:
    client = FakeS3Client()
    presigned = storage(client).presign_upload(KEY, expires_in_seconds=600)

    assert client.presign_calls == [
        ("put_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY, "IfNoneMatch": "*"}, 600)
    ]
    assert presigned.operation == "put"
    assert presigned.expires_in_seconds == 600
    assert presigned.if_none_match is True


def test_presign_upload_overwrite_omits_the_conditional_header() -> None:
    client = FakeS3Client()

    presigned = storage(client).presign_upload(KEY, overwrite=True)

    assert client.presign_calls == [("put_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY}, 3600)]
    assert presigned.if_none_match is False


def test_real_botocore_signs_the_no_replace_header_without_network() -> None:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="TEST_ONLY_ACCESS_KEY",
        aws_secret_access_key="TEST_ONLY_SECRET_KEY",
        config=Config(signature_version="v4"),
    )
    instance = S3Storage(bucket=BUCKET, prefix=PREFIX, client=client)

    guarded = instance.presign_upload(KEY)
    replacement = instance.presign_upload(KEY, overwrite=True)

    guarded_headers = parse_qs(urlsplit(guarded.reveal()).query)["X-Amz-SignedHeaders"][0]
    replacement_headers = parse_qs(urlsplit(replacement.reveal()).query)["X-Amz-SignedHeaders"][0]
    assert "if-none-match" in guarded_headers.split(";")
    assert "if-none-match" not in replacement_headers.split(";")


@pytest.mark.parametrize(
    "expiry", [0, MIN_PRESIGN_EXPIRY_SECONDS - 1, MAX_PRESIGN_EXPIRY_SECONDS + 1]
)
def test_presign_rejects_unbounded_or_absurd_expiry(expiry: int) -> None:
    client = FakeS3Client()

    with pytest.raises(StorageKeyError):
        storage(client).presign_download(KEY, expires_in_seconds=expiry)

    assert client.presign_calls == []


def test_presign_rejects_an_unsafe_key_before_calling_s3() -> None:
    client = FakeS3Client()

    with pytest.raises(StorageKeyError):
        storage(client).presign_download("../escape.tar")

    assert client.presign_calls == []


def test_presigned_url_representation_is_redacted_but_revealable() -> None:
    presigned = storage(FakeS3Client()).presign_download(KEY)

    for rendered in (repr(presigned), str(presigned), f"{presigned}"):
        assert "X-Amz-Signature" not in rendered
        assert "deadbeefcafebabe" not in rendered
        assert "url=<redacted>" in rendered

    assert presigned.reveal().endswith("X-Amz-Signature=deadbeefcafebabe")


def test_list_objects_resolves_the_configured_namespace() -> None:
    client = FakeS3Client(
        pages=[
            {
                "Contents": [
                    {
                        "Key": RESOLVED_KEY,
                        "Size": 4096,
                        "ETag": '"abc"',
                        "LastModified": LAST_MODIFIED,
                        "StorageClass": "STANDARD",
                    }
                ],
                "IsTruncated": False,
            }
        ]
    )

    objects = storage(client).list_objects("embeddings/", limit=10)

    assert client.list_calls == [
        {"Bucket": BUCKET, "Prefix": f"{PREFIX}/embeddings/", "MaxKeys": 10}
    ]
    assert objects == [
        StoredObject(
            key=RESOLVED_KEY,
            size_bytes=4096,
            etag="abc",
            last_modified=LAST_MODIFIED,
            storage_class="STANDARD",
        )
    ]


def test_list_objects_follows_continuation_tokens_up_to_the_limit() -> None:
    first_page = {
        "Contents": [{"Key": f"{PREFIX}/a", "Size": 1}],
        "IsTruncated": True,
        "NextContinuationToken": "token-2",
    }
    second_page = {
        "Contents": [{"Key": f"{PREFIX}/b", "Size": 2}],
        "IsTruncated": True,
        "NextContinuationToken": "token-3",
    }
    client = FakeS3Client(pages=[first_page, second_page])

    objects = storage(client).list_objects(limit=2)

    assert [item.key for item in objects] == [f"{PREFIX}/a", f"{PREFIX}/b"]
    assert len(client.list_calls) == 2
    assert client.list_calls[1]["ContinuationToken"] == "token-2"
    assert client.list_calls[1]["MaxKeys"] == 1


def test_list_objects_rejects_a_truncated_page_without_a_next_token() -> None:
    client = FakeS3Client(pages=[{"Contents": [], "IsTruncated": True}])

    with pytest.raises(StorageError, match="continuation token"):
        storage(client).list_objects()


def test_head_metadata_without_a_size_is_not_reported_as_an_empty_object() -> None:
    client = FakeS3Client(head={"ETag": '"abc"'})

    with pytest.raises(StorageError, match="valid byte size"):
        storage(client).verify_object(KEY, expected_size=0)


@pytest.mark.parametrize("limit", [0, -1, 100000])
def test_list_objects_rejects_an_unbounded_limit(limit: int) -> None:
    client = FakeS3Client()

    with pytest.raises(StorageError):
        storage(client).list_objects(limit=limit)

    assert client.list_calls == []


def test_object_metadata_reads_an_existing_object() -> None:
    client = FakeS3Client(head=head_response(2048))

    stored = storage(client).object_metadata(KEY)

    assert client.head_calls == [{"Bucket": BUCKET, "Key": RESOLVED_KEY}]
    assert stored is not None
    assert stored.size_bytes == 2048
    assert stored.etag == "5d41402abc4b2a76b9719d911017c592"
    assert stored.last_modified == LAST_MODIFIED


def test_missing_object_is_reported_as_absent() -> None:
    client = FakeS3Client(error=client_error("NoSuchKey", 404))

    storage_instance = storage(client)

    assert storage_instance.object_metadata(KEY) is None
    assert storage_instance.object_exists(KEY) is False


def test_permission_failure_is_sanitized_and_actionable() -> None:
    client = FakeS3Client(error=client_error("AccessDenied", 403))

    with pytest.raises(StoragePermissionError) as error:
        storage(client).object_metadata(KEY)

    message = str(error.value)
    assert "not permitted" in message
    assert "role policy" in message
    assert "synthetic AccessDenied" not in message


def test_transport_failure_is_reported_without_credentials() -> None:
    client = FakeS3Client(error=BotoCoreError())

    with pytest.raises(StorageError) as error:
        storage(client).object_metadata(KEY)

    assert "Could not read metadata for" in str(error.value)


def test_missing_credentials_are_reported_as_configuration() -> None:
    client = FakeS3Client(error=NoCredentialsError())

    with pytest.raises(ConfigurationError) as error:
        storage(client).object_metadata(KEY)

    assert "instance profile" in str(error.value)


def test_read_object_bytes_refuses_an_oversized_body() -> None:
    client = FakeS3Client(body=b"x" * 32)

    with pytest.raises(StorageVerificationError) as error:
        storage(client).read_object_bytes(KEY, max_bytes=16)

    assert "byte limit" in str(error.value)


def test_read_object_bytes_reports_a_missing_object() -> None:
    client = FakeS3Client(error=client_error("NoSuchKey", 404))

    with pytest.raises(StorageObjectNotFoundError):
        storage(client).read_object_bytes(KEY)


def test_read_manifest_parses_a_stored_document() -> None:
    client = FakeS3Client(body=_manifest().model_dump_json().encode("utf-8"))

    manifest = storage(client).read_manifest("embeddings/v1/manifest.json")

    assert manifest.object_key == KEY
    assert client.get_calls == [{"Bucket": BUCKET, "Key": f"{PREFIX}/embeddings/v1/manifest.json"}]


def test_read_manifest_rejects_non_utf8_or_invalid_documents() -> None:
    with pytest.raises(StorageVerificationError):
        storage(FakeS3Client(body=b"\xff\xfe")).read_manifest("embeddings/v1/manifest.json")
    with pytest.raises(StorageVerificationError):
        storage(FakeS3Client(body=b"not json")).read_manifest("embeddings/v1/manifest.json")


def test_verify_object_confirms_existence_and_expected_size() -> None:
    client = FakeS3Client(head=head_response(1024))

    verification = storage(client).verify_object(KEY, expected_size=1024)

    assert verification.key == RESOLVED_KEY
    assert verification.size_bytes == 1024
    assert verification.expected_size_checked is True
    assert verification.manifest_checked is False
    assert verification.content_checksum_verified is False
    assert any("SHA-256" in limitation for limitation in verification.limitations)
    assert any("ETag" in limitation for limitation in verification.limitations)


def test_verify_object_rejects_a_size_mismatch() -> None:
    client = FakeS3Client(head=head_response(1024))

    with pytest.raises(StorageVerificationError) as error:
        storage(client).verify_object(KEY, expected_size=2048)

    assert "1024 bytes" in str(error.value)


def test_verify_object_reports_a_missing_object() -> None:
    client = FakeS3Client(error=client_error("NoSuchKey", 404))

    with pytest.raises(StorageObjectNotFoundError):
        storage(client).verify_object(KEY)


def test_verify_object_accepts_a_consistent_manifest() -> None:
    client = FakeS3Client(head=head_response(1024))

    verification = storage(client).verify_object(KEY, manifest=_manifest())

    assert verification.manifest_checked is True
    assert verification.manifest_sha256 == "c" * 64


def test_verify_object_rejects_a_manifest_for_a_different_object() -> None:
    client = FakeS3Client(head=head_response(1024))

    with pytest.raises(StorageVerificationError) as error:
        storage(client).verify_object(KEY, manifest=_manifest(object_key="embeddings/other.tar"))

    assert "'embeddings/other.tar'" in str(error.value)
    assert RESOLVED_KEY in str(error.value)


def test_verify_object_rejects_a_manifest_with_a_different_size() -> None:
    client = FakeS3Client(head=head_response(1024))

    with pytest.raises(StorageVerificationError) as error:
        storage(client).verify_object(KEY, manifest=_manifest(size_bytes=2048))

    assert "records 2048 bytes" in str(error.value)


def test_require_writable_refuses_an_implicit_replacement() -> None:
    client = FakeS3Client(head=head_response())

    with pytest.raises(StorageObjectExistsError) as error:
        storage(client).require_writable(KEY, overwrite=False)

    assert "already exists" in str(error.value)
    assert "--overwrite" in str(error.value)

    storage(client).require_writable(KEY, overwrite=True)


def test_require_writable_allows_a_new_key() -> None:
    client = FakeS3Client(error=client_error("NoSuchKey", 404))

    storage(client).require_writable(KEY, overwrite=False)


def test_storage_requires_a_configured_bucket_and_region() -> None:
    with pytest.raises(ConfigurationError) as error:
        S3Storage.from_settings(Settings.model_validate({"aws": {"region": "ap-south-1"}}))
    assert "storage.bucket" in str(error.value)

    with pytest.raises(ConfigurationError) as error:
        S3Storage.from_settings(
            Settings.model_validate({"storage": {"bucket": BUCKET, "prefix": PREFIX}})
        )
    assert "aws.region" in str(error.value)


def test_storage_builds_a_client_from_the_normal_credential_chain(monkeypatch) -> None:
    client = FakeS3Client()
    sessions: list[dict[str, object]] = []
    configs: list[object] = []

    class FakeSession:
        def __init__(self, *, region_name: str | None = None) -> None:
            self.region_name = region_name

        def client(self, name: str, *, config: object = None) -> FakeS3Client:
            sessions.append({"name": name, "region": self.region_name})
            configs.append(config)
            return client

    monkeypatch.setattr(s3_module.boto3, "Session", FakeSession)
    settings = Settings.model_validate(
        {
            "aws": {"region": "ap-south-1"},
            "storage": {"bucket": BUCKET, "prefix": PREFIX, "presign_expiry_seconds": 900},
        }
    )

    instance = S3Storage.from_settings(settings)
    presigned = instance.presign_download(KEY)

    assert sessions == [{"name": "s3", "region": "ap-south-1"}]
    assert configs[0] is not None
    assert presigned.expires_in_seconds == 900
    assert instance.bucket == BUCKET
    assert instance.prefix == PREFIX


def test_presign_failure_is_reported_without_the_signature(tmp_path: Path) -> None:
    client = FakeS3Client(error=ValueError(f"bad request for {PRESIGNED_URL}"))

    with pytest.raises(StorageError) as error:
        storage(client).presign_download(KEY)

    message = str(error.value)
    assert "Could not generate a presigned GET URL" in message
    assert "X-Amz-Signature" not in message
    assert "deadbeefcafebabe" not in message


def test_storage_settings_reject_an_out_of_range_presign_expiry() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(
            {"storage": {"bucket": BUCKET, "prefix": PREFIX, "presign_expiry_seconds": 5}}
        )


# --- credential-bounded lifetimes and transient classification ---------------------


def _credential_storage(client: FakeS3Client, *, remaining_seconds: float):
    from datetime import timedelta

    from wavcse_infra.storage.s3 import S3Storage

    expiry = datetime.now(UTC) + timedelta(seconds=remaining_seconds)
    return S3Storage(
        bucket=BUCKET,
        prefix=PREFIX,
        client=client,
        presign_expiry_seconds=3600,
        credential_expiry=lambda: expiry,
    )


def test_a_url_is_never_signed_beyond_the_credential_that_signs_it() -> None:
    """A temporary credential's expiry caps the lifetime, whatever ExpiresIn asked for."""

    client = FakeS3Client()

    presigned = _credential_storage(client, remaining_seconds=300).presign_download(KEY)

    granted = client.presign_calls[0][2]
    assert granted <= 270
    assert presigned.expires_in_seconds == granted


def test_static_credentials_are_not_capped() -> None:
    client = FakeS3Client()

    presigned = storage(client, presign_expiry_seconds=1800).presign_download(KEY)

    assert presigned.expires_in_seconds == 1800


def test_presigning_refuses_when_the_credential_is_about_to_expire() -> None:
    from wavcse_infra.errors import StorageUnavailableError

    client = FakeS3Client()

    with pytest.raises(StorageUnavailableError, match="too soon to sign"):
        _credential_storage(client, remaining_seconds=10).presign_download(KEY)

    assert client.presign_calls == []


def test_a_service_failure_is_reported_as_unavailable_not_as_failure() -> None:
    """A throttled or failed observation says nothing about the object."""

    from botocore.exceptions import ClientError

    from wavcse_infra.errors import StorageUnavailableError

    error = ClientError(
        {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
        "HeadObject",
    )
    client = FakeS3Client(head=head_response(), error=error)

    with pytest.raises(StorageUnavailableError, match="service observation failure"):
        storage(client).object_metadata(KEY)


def test_a_transport_failure_is_reported_as_unavailable() -> None:
    from botocore.exceptions import BotoCoreError

    from wavcse_infra.errors import StorageUnavailableError

    client = FakeS3Client(head=head_response(), error=BotoCoreError())

    with pytest.raises(StorageUnavailableError, match="not evidence about the object"):
        storage(client).object_metadata(KEY)


def test_a_missing_object_stays_a_definitive_absence() -> None:
    from botocore.exceptions import ClientError

    error = ClientError(
        {
            "Error": {"Code": "404", "Message": "Not Found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadObject",
    )
    client = FakeS3Client(head=head_response(), error=error)

    assert storage(client).object_metadata(KEY) is None


# --- object content verification ---------------------------------------------------


def test_verify_object_content_proves_the_stored_bytes() -> None:
    payload = b"embedding-bytes"
    client = FakeS3Client(head=head_response(len(payload)), body=payload)

    verification = storage(client).verify_object_content(
        KEY,
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert verification.content_checksum_verified is True
    assert verification.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert verification.size_bytes == len(payload)
    assert verification.expected_size_checked is True
    assert verification.key == RESOLVED_KEY


def test_verify_object_content_rejects_different_bytes_of_the_same_size() -> None:
    """Metadata that matches is not content: the stored bytes must match too."""

    client = FakeS3Client(head=head_response(4), body=b"good")

    with pytest.raises(StorageVerificationError) as error:
        storage(client).verify_object_content(
            KEY,
            expected_size=4,
            expected_sha256=hashlib.sha256(b"evil").hexdigest(),
        )

    assert "does not contain the expected bytes" in str(error.value)


def test_verify_object_content_rejects_a_size_mismatch() -> None:
    client = FakeS3Client(head=head_response(4), body=b"good")

    with pytest.raises(StorageVerificationError) as error:
        storage(client).verify_object_content(KEY, expected_size=8)

    assert "contains 4 bytes" in str(error.value)
    assert "8 bytes were expected" in str(error.value)


def test_verify_object_content_binds_the_read_to_the_reported_version() -> None:
    """An object replaced after the metadata read must not be read as the verified one."""

    payload = b"versioned"
    client = FakeS3Client(head={**head_response(len(payload)), "VersionId": "v7"}, body=payload)

    verification = storage(client).verify_object_content(
        KEY, expected_sha256=hashlib.sha256(payload).hexdigest()
    )

    assert verification.content_checksum_verified is True
    assert client.get_calls[0]["VersionId"] == "v7"


def test_verify_object_content_reports_a_missing_object() -> None:
    with pytest.raises(StorageObjectNotFoundError):
        storage(FakeS3Client()).verify_object_content(KEY)


# --- HIGH 3: the real expiry of the signing credentials caps a URL ------------------


def _refreshable_credentials(remaining_seconds: float):
    from datetime import timedelta

    client = FakeS3Client()
    expiry = datetime.now(UTC) + timedelta(seconds=remaining_seconds)
    credentials = RefreshableCredentials(
        access_key="AKIAEXAMPLE",
        secret_key="secret",
        token="token",
        expiry_time=expiry,
        # Never refresh inside these tests: the initial expiry is what must be honoured.
        refresh_using=lambda: {
            "access_key": "AKIAEXAMPLE",
            "secret_key": "secret",
            "token": "token",
            "expiry_time": expiry.isoformat(),
        },
        method="test",
        advisory_timeout=0,
        mandatory_timeout=0,
    )
    instance = S3Storage(
        bucket=BUCKET,
        prefix=PREFIX,
        client=client,
        presign_expiry_seconds=3600,
        credential_expiry=s3_module._credential_expiry_reader(credentials),
    )
    return instance, client


def test_a_real_refreshable_credential_caps_the_signed_lifetime() -> None:
    """botocore keeps this expiry on the refreshable object, not on a public attribute."""

    instance, client = _refreshable_credentials(remaining_seconds=300)

    presigned = instance.presign_download(KEY)

    granted = client.presign_calls[0][2]
    assert granted <= 270
    assert presigned.expires_in_seconds == granted


def test_a_real_refreshable_credential_about_to_expire_refuses_to_sign() -> None:
    instance, client = _refreshable_credentials(remaining_seconds=10)

    with pytest.raises(StorageUnavailableError, match="too soon to sign"):
        instance.presign_download(KEY)

    assert client.presign_calls == []


def test_a_real_static_credential_produces_no_cap() -> None:
    from botocore.credentials import Credentials

    assert s3_module._credential_expiry_reader(Credentials("AKIAEXAMPLE", "secret")) is None


# --- MEDIUM 2: an unsigned request is not evidence, a bad configuration is ---------


def test_a_transient_credential_refresh_failure_while_signing_is_unavailable() -> None:
    from botocore.exceptions import CredentialRetrievalError

    client = FakeS3Client(
        error=CredentialRetrievalError(provider="test", error_msg="could not refresh")
    )

    with pytest.raises(StorageUnavailableError):
        storage(client).presign_download(KEY)


def test_missing_credentials_while_signing_stay_a_configuration_error() -> None:
    client = FakeS3Client(error=NoCredentialsError())

    with pytest.raises(ConfigurationError) as error:
        storage(client).presign_download(KEY)

    assert "instance profile" in str(error.value)


def test_an_invalid_signing_request_stays_definitive() -> None:
    from botocore.exceptions import ParamValidationError

    client = FakeS3Client(error=ParamValidationError(report="bad parameters"))

    with pytest.raises(StorageError) as error:
        storage(client).presign_download(KEY)

    assert not isinstance(error.value, StorageUnavailableError)
    assert "Could not generate a presigned GET URL" in str(error.value)


# --- MEDIUM 4: a generic bad request is not a transient condition -------------------


def test_a_generic_bad_request_is_not_transient() -> None:
    """Only named retryable conditions are transient; an invalid request stays definitive."""

    client = FakeS3Client(head=head_response(), error=client_error("InvalidRequest", 400))

    with pytest.raises(StorageError) as error:
        storage(client).object_metadata(KEY)

    assert not isinstance(error.value, StorageUnavailableError)
    assert "HTTP 400" in str(error.value)


def test_a_named_retryable_code_is_still_transient_on_a_400() -> None:
    client = FakeS3Client(head=head_response(), error=client_error("RequestTimeout", 400))

    with pytest.raises(StorageUnavailableError):
        storage(client).object_metadata(KEY)
