import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.models import Worker, WorkerConnectionInfo, WorkerState
from wavcse_infra.storage.manifests import ArtifactManifest, manifest_json
from wavcse_infra.storage.s3 import S3Storage
from wavcse_infra.workers.ssh import SshCommandResult, SshWaitResult

runner = CliRunner()

BUCKET = "private-wavcse"
KEY = "embeddings/v1/voxceleb-minpooling.tar"
RESOLVED_KEY = f"wavcse/{KEY}"
SHA256 = "e" * 64
CREATED_AT = datetime(2026, 9, 28, 9, 30, tzinfo=UTC)
PRESIGNED_URL = (
    "https://private-wavcse.s3.ap-south-1.amazonaws.com/wavcse/embeddings/v1/"
    "voxceleb-minpooling.tar?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef"
)
CONFIGURED_ENV = {
    "WAVCSE_INFRA_S3_BUCKET": BUCKET,
    "WAVCSE_INFRA_AWS_REGION": "ap-south-1",
}


def _head(size: int = 3072) -> dict[str, object]:
    return {
        "ContentLength": size,
        "ETag": '"5d41402abc4b2a76b9719d911017c592"',
        "LastModified": CREATED_AT,
        "StorageClass": "STANDARD",
    }


def _protocol(operation: str, *, path: str, size: int = 3072) -> str:
    return "\n".join(
        (
            "wavcse_transfer_schema\t1",
            f"operation\t{operation}",
            "status\tok",
            f"path\t{path}",
            f"size_bytes\t{size}",
            f"sha256\t{SHA256}",
            "",
        )
    )


class FakeClient:
    """Minimal Boto3 S3 client double for CLI-level tests."""

    def __init__(
        self,
        *,
        head: dict[str, object] | None = None,
        pages: list[dict[str, object]] | None = None,
        body: bytes = b"{}",
        error: Exception | None = None,
    ) -> None:
        self.head = head
        self.pages = pages if pages is not None else [{}]
        self.body = body
        self.error = error
        self.presign_calls: list[tuple[str, dict[str, object], int]] = []
        self.list_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        index = len(self.list_calls) - 1
        return self.pages[index] if index < len(self.pages) else {}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        if self.head is None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
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
        return PRESIGNED_URL


class FakePodClient:
    def __enter__(self) -> "FakePodClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


class FakeWaiter:
    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> SshWaitResult:
        connection = WorkerConnectionInfo(
            provider_worker_id=worker_id,
            kind="direct",
            host="203.0.113.9",
            port=30222,
            username="root",
        )
        worker = Worker(id=worker_id, state=WorkerState.RUNNING, ssh_direct=connection)
        return SshWaitResult(worker=worker, connection=connection)


class FakeExecutor:
    """Captures the streamed worker module and the generated prologue line."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        self.calls.append((remote_argv, input_text, timeout_seconds))
        return SshCommandResult(exit_code=0, stdout=self.output, stderr="")


def _install_storage(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> S3Storage:
    instance = S3Storage(bucket=BUCKET, prefix="wavcse", client=client)
    monkeypatch.setattr(cli.S3Storage, "from_settings", classmethod(lambda cls, settings: instance))
    return instance


def _install_worker(monkeypatch: pytest.MonkeyPatch, output: str) -> FakeExecutor:
    executor = FakeExecutor(output)
    monkeypatch.setattr(cli.RunPodClient, "from_settings", lambda settings: FakePodClient())
    monkeypatch.setattr(cli, "_ssh_access", lambda client, settings: (executor, FakeWaiter()))
    return executor


def _manifest(**overrides: object) -> ArtifactManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "artifact_name": "voxceleb-minpooling",
        "artifact_type": "embeddings-archive",
        "dataset": "voxceleb",
        "object_key": KEY,
        "size_bytes": 3072,
        "sha256": SHA256,
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return ArtifactManifest.model_validate(values)


def test_storage_group_documents_every_command() -> None:
    result = runner.invoke(app, ["storage", "--help"])

    assert result.exit_code == 0
    for command in (
        "list",
        "presign-download",
        "presign-upload",
        "verify",
        "read",
        "download",
        "upload",
    ):
        assert command in result.stdout


def test_storage_commands_require_a_configured_bucket() -> None:
    result = runner.invoke(app, ["storage", "list"], env={})

    assert result.exit_code == 2
    assert "storage.bucket" in result.stderr


def test_storage_list_renders_objects_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    page = {
        "Contents": [
            {
                "Key": RESOLVED_KEY,
                "Size": 3072,
                "ETag": '"abc"',
                "LastModified": CREATED_AT,
                "StorageClass": "STANDARD",
            }
        ],
        "IsTruncated": False,
    }
    client = FakeClient(pages=[page, page])
    _install_storage(monkeypatch, client)

    result = runner.invoke(app, ["storage", "list", "--prefix", "embeddings/"], env=CONFIGURED_ENV)

    assert result.exit_code == 0
    assert f"3072\t2026-09-28T09:30:00+00:00\ts3://{BUCKET}/{RESOLVED_KEY}" in result.stdout
    assert client.list_calls[0]["Prefix"] == "wavcse/embeddings/"

    json_result = runner.invoke(app, ["storage", "list", "--json"], env=CONFIGURED_ENV)

    payload = json.loads(json_result.stdout)
    assert payload[0]["key"] == RESOLVED_KEY


def test_storage_list_reports_an_empty_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_storage(monkeypatch, FakeClient())

    result = runner.invoke(app, ["storage", "list"], env=CONFIGURED_ENV)

    assert result.exit_code == 0
    assert f"No objects found under s3://{BUCKET}/wavcse/" in result.stdout


def test_presign_download_prints_only_the_requested_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeClient(head=_head())
    _install_storage(monkeypatch, client)

    result = runner.invoke(
        app, ["storage", "presign-download", KEY, "--expires-in", "900"], env=CONFIGURED_ENV
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == PRESIGNED_URL
    assert result.stderr == ""
    assert client.presign_calls == [("get_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY}, 900)]
    assert not (tmp_path / ".local").exists()
    assert not (tmp_path / ".config").exists()


def test_presign_download_refuses_a_missing_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_storage(monkeypatch, FakeClient(head=None))

    result = runner.invoke(app, ["storage", "presign-download", KEY], env=CONFIGURED_ENV)

    assert result.exit_code == 1
    assert "does not exist" in result.stderr
    assert "X-Amz-Signature" not in result.stdout + result.stderr


def test_presign_download_rejects_an_unsafe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=_head())
    _install_storage(monkeypatch, client)

    result = runner.invoke(
        app, ["storage", "presign-download", "../escape.tar"], env=CONFIGURED_ENV
    )

    assert result.exit_code == 1
    assert "artifact key" in result.stderr
    assert client.presign_calls == []


def test_presign_download_rejects_an_unbounded_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=_head())
    _install_storage(monkeypatch, client)

    result = runner.invoke(
        app, ["storage", "presign-download", KEY, "--expires-in", "99999999"], env=CONFIGURED_ENV
    )

    assert result.exit_code == 2
    assert client.presign_calls == []


def test_presign_upload_requires_explicit_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=_head())
    _install_storage(monkeypatch, client)

    result = runner.invoke(app, ["storage", "presign-upload", KEY], env=CONFIGURED_ENV)

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert "--overwrite" in result.stderr
    assert client.presign_calls == []

    overwrite_result = runner.invoke(
        app, ["storage", "presign-upload", KEY, "--overwrite"], env=CONFIGURED_ENV
    )

    assert overwrite_result.exit_code == 0
    assert overwrite_result.stdout.strip() == PRESIGNED_URL
    assert client.presign_calls == [("put_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY}, 3600)]


def test_presign_upload_allows_a_new_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=None)
    _install_storage(monkeypatch, client)

    result = runner.invoke(app, ["storage", "presign-upload", KEY], env=CONFIGURED_ENV)

    assert result.exit_code == 0
    assert result.stdout.strip() == PRESIGNED_URL
    assert "If-None-Match: *" in result.stderr
    assert client.presign_calls == [
        ("put_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY, "IfNoneMatch": "*"}, 3600)
    ]


def test_verify_reports_metadata_and_documents_the_limitations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))

    result = runner.invoke(
        app, ["storage", "verify", KEY, "--expected-size", "3072"], env=CONFIGURED_ENV
    )

    assert result.exit_code == 0
    assert f"Object: s3://{BUCKET}/{RESOLVED_KEY}" in result.stdout
    assert (
        "ETag: 5d41402abc4b2a76b9719d911017c592 (S3 ETag is not a SHA-256 digest)" in result.stdout
    )
    assert "Expected size: matched" in result.stdout
    assert "Manifest: not provided" in result.stdout
    assert "Content checksum: NOT verified" in result.stdout
    assert "note: Object content was not downloaded" in result.stdout


def test_verify_fails_when_the_expected_size_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))

    result = runner.invoke(
        app, ["storage", "verify", KEY, "--expected-size", "99"], env=CONFIGURED_ENV
    )

    assert result.exit_code == 1
    assert "99 bytes were expected" in result.stderr


def test_verify_reports_a_missing_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_storage(monkeypatch, FakeClient(head=None))

    result = runner.invoke(app, ["storage", "verify", KEY], env=CONFIGURED_ENV)

    assert result.exit_code == 1
    assert "does not exist" in result.stderr


def test_verify_accepts_a_local_manifest_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(manifest_json(_manifest()), encoding="utf-8")

    result = runner.invoke(
        app,
        ["storage", "verify", KEY, "--manifest-file", str(manifest_file)],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 0
    assert f"Manifest: consistent; recorded SHA-256 {SHA256}" in result.stdout


def test_verify_reads_a_manifest_from_the_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=_head(), body=manifest_json(_manifest()).encode("utf-8"))
    _install_storage(monkeypatch, client)

    result = runner.invoke(
        app,
        ["storage", "verify", KEY, "--manifest", "embeddings/v1/manifest.json"],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 0
    assert client.get_calls == [{"Bucket": BUCKET, "Key": "wavcse/embeddings/v1/manifest.json"}]
    assert "Manifest: consistent" in result.stdout


def test_verify_rejects_inconsistent_or_invalid_manifests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))

    missing_argument = runner.invoke(
        app, ["storage", "verify", KEY, "--manifest-file"], env=CONFIGURED_ENV
    )
    both_arguments = runner.invoke(
        app,
        [
            "storage",
            "verify",
            KEY,
            "--manifest",
            "embeddings/v1/manifest.json",
            "--manifest-file",
            str(tmp_path / "manifest.json"),
        ],
        env=CONFIGURED_ENV,
    )
    invalid_file = tmp_path / "manifest.json"
    invalid_file.write_text("{not json", encoding="utf-8")
    invalid = runner.invoke(
        app,
        ["storage", "verify", KEY, "--manifest-file", str(invalid_file)],
        env=CONFIGURED_ENV,
    )

    assert missing_argument.exit_code == 2
    assert both_arguments.exit_code == 2
    assert "mutually exclusive" in both_arguments.stderr
    assert invalid.exit_code == 1
    assert "not valid JSON" in invalid.stderr


def test_verify_rejects_a_manifest_with_the_wrong_size(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=_head(), body=manifest_json(_manifest(size_bytes=99)).encode("utf-8"))
    _install_storage(monkeypatch, client)

    result = runner.invoke(
        app,
        ["storage", "verify", KEY, "--manifest", "embeddings/v1/manifest.json"],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 1
    assert "records 99 bytes" in result.stderr


def test_verify_supports_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))

    result = runner.invoke(app, ["storage", "verify", KEY, "--json"], env=CONFIGURED_ENV)

    payload = json.loads(result.stdout)
    assert payload["key"] == RESOLVED_KEY
    assert payload["content_checksum_verified"] is False


def test_read_returns_the_bytes_and_the_digest_the_caller_computes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller inspecting evidence hashes the bytes itself, not a worker's report."""

    body = b'{"study": "TR-0007", "schema_version": 1}\n'
    digest = hashlib.sha256(body).hexdigest()
    client = FakeClient(body=body)
    _install_storage(monkeypatch, client)

    result = runner.invoke(
        app,
        ["storage", "read", KEY, "--expected-sha256", digest, "--json"],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["artifact"] == RESOLVED_KEY
    assert payload["sha256"] == digest
    assert payload["size_bytes"] == len(body)
    assert payload["text"] == body.decode()
    # One bounded body read, and no presign/list call at all.
    assert client.get_calls[0]["Key"] == RESOLVED_KEY
    assert client.presign_calls == []
    assert client.list_calls == []


def test_read_refuses_bytes_that_do_not_match_the_expected_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(body=b"a different run"))

    result = runner.invoke(
        app, ["storage", "read", KEY, "--expected-sha256", "a" * 64], env=CONFIGURED_ENV
    )

    assert result.exit_code == 1
    assert "does not contain the expected bytes" in result.stderr
    assert "a different run" not in result.stdout


def test_read_refuses_an_object_larger_than_the_requested_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(body=b"x" * 64))

    result = runner.invoke(app, ["storage", "read", KEY, "--max-bytes", "16"], env=CONFIGURED_ENV)

    assert result.exit_code == 1
    assert "exceeds the 16 byte limit" in result.stderr


def test_read_rejects_a_malformed_expected_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(body=b"{}"))

    result = runner.invoke(
        app, ["storage", "read", KEY, "--expected-sha256", "not-a-digest"], env=CONFIGURED_ENV
    )

    assert result.exit_code == 2
    assert "64 lowercase hex" in result.stderr


def test_read_requires_a_configured_bucket() -> None:
    result = runner.invoke(app, ["storage", "read", KEY], env={})

    assert result.exit_code == 2
    assert "storage.bucket" in result.stderr


def test_download_command_presigns_and_streams_the_worker_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient(head=_head())
    _install_storage(monkeypatch, client)
    executor = _install_worker(monkeypatch, _protocol("download", path="/workspace/embeddings.tar"))

    result = runner.invoke(
        app,
        [
            "storage",
            "download",
            KEY,
            "/workspace/embeddings.tar",
            "--worker",
            "pod-123",
            "--expected-size",
            "3072",
            "--expected-sha256",
            SHA256,
        ],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 0
    assert "Worker pod-123 downloaded the artifact." in result.stdout
    assert f"SHA-256: {SHA256}" in result.stdout
    assert "X-Amz-Signature" not in result.stdout + result.stderr
    assert client.presign_calls == [("get_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY}, 3600)]

    remote_argv, input_text, timeout = executor.calls[0]
    assert remote_argv == (
        "python3",
        "-",
        "download",
        "--destination",
        "/workspace/embeddings.tar",
        "--expected-size",
        "3072",
        "--expected-sha256",
        SHA256,
    )
    assert input_text is not None
    assert input_text.startswith(f"WAVCSE_PRESIGNED_URL = {PRESIGNED_URL!r}\n")
    assert "wavcse_transfer_schema" in input_text
    # One attempt may never outlive its bearer URL: 3600 seconds of URL lifetime minus the
    # 30-second safety margin caps the 3600-second default command bound.
    assert timeout == 3570.0


def test_download_command_passes_an_explicit_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))
    executor = _install_worker(monkeypatch, _protocol("download", path="/workspace/embeddings.tar"))

    result = runner.invoke(
        app,
        [
            "storage",
            "download",
            KEY,
            "/workspace/embeddings.tar",
            "--worker",
            "pod-123",
            "--expected-size",
            "3072",
            "--concurrency",
            "4",
        ],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 0
    remote_argv, _, _ = executor.calls[0]
    assert remote_argv == (
        "python3",
        "-",
        "download",
        "--destination",
        "/workspace/embeddings.tar",
        "--expected-size",
        "3072",
        "--concurrency",
        "4",
    )


def test_download_command_rejects_a_relative_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))
    executor = _install_worker(monkeypatch, _protocol("download", path="/workspace/embeddings.tar"))

    result = runner.invoke(
        app,
        ["storage", "download", KEY, "relative.tar", "--worker", "pod-123"],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 1
    assert "absolute path" in result.stderr
    assert executor.calls == []


def test_download_command_reports_a_worker_protocol_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))
    _install_worker(monkeypatch, "wavcse_transfer_error\tthe storage endpoint returned HTTP 403\n")

    result = runner.invoke(
        app,
        ["storage", "download", KEY, "/workspace/embeddings.tar", "--worker", "pod-123"],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 1
    assert "without schema version 1" in result.stderr
    assert "X-Amz-Signature" not in result.stdout + result.stderr


def test_upload_command_requires_explicit_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head()))
    executor = _install_worker(monkeypatch, _protocol("upload", path="/workspace/embeddings.tar"))

    result = runner.invoke(
        app,
        ["storage", "upload", KEY, "/workspace/embeddings.tar", "--worker", "pod-123"],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert executor.calls == []


def test_upload_command_verifies_the_stored_object(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(head=_head())
    _install_storage(monkeypatch, client)
    _install_worker(monkeypatch, _protocol("upload", path="/workspace/embeddings.tar"))

    result = runner.invoke(
        app,
        [
            "storage",
            "upload",
            KEY,
            "/workspace/embeddings.tar",
            "--worker",
            "pod-123",
            "--overwrite",
            "--json",
        ],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["operation"] == "upload"
    assert payload["result"]["sha256"] == SHA256
    assert payload["verification"]["key"] == RESOLVED_KEY
    assert payload["verification"]["expected_size_checked"] is True
    assert payload["verification"]["content_checksum_verified"] is False
    assert client.presign_calls == [("put_object", {"Bucket": BUCKET, "Key": RESOLVED_KEY}, 3600)]


def test_upload_command_reports_a_size_mismatch_after_the_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_storage(monkeypatch, FakeClient(head=_head(size=99)))
    _install_worker(monkeypatch, _protocol("upload", path="/workspace/embeddings.tar"))

    result = runner.invoke(
        app,
        [
            "storage",
            "upload",
            KEY,
            "/workspace/embeddings.tar",
            "--worker",
            "pod-123",
            "--overwrite",
        ],
        env=CONFIGURED_ENV,
    )

    assert result.exit_code == 1
    assert "99 bytes" in result.stderr
