import ast
import subprocess
from pathlib import Path

import pytest

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    ArtifactTransferError,
    SshCommandError,
    StorageObjectExistsError,
)
from wavcse_infra.models import Worker, WorkerConnectionInfo, WorkerState
from wavcse_infra.storage.s3 import PresignedUrl, StorageVerification
from wavcse_infra.storage.transfer import (
    WorkerArtifactTransfer,
    load_worker_transfer_source,
    parse_transfer_output,
    presigned_url_assignment,
)
from wavcse_infra.workers.ssh import SshCommandResult, SshExecutor, SshWaitResult

KEY = "embeddings/v1/voxceleb-minpooling.tar"
DESTINATION = "/workspace/embeddings/voxceleb-minpooling.tar"
DIGEST = "d" * 64
PRESIGNED_URL = (
    "https://private-wavcse.s3.ap-south-1.amazonaws.com/wavcse/embeddings/v1/"
    "voxceleb-minpooling.tar?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef"
)


def _connection() -> WorkerConnectionInfo:
    return WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="direct",
        host="203.0.113.9",
        port=30222,
        username="root",
    )


class Waiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> SshWaitResult:
        self.calls.append((worker_id, timeout_seconds))
        worker = Worker(id=worker_id, state=WorkerState.RUNNING, ssh_direct=_connection())
        return SshWaitResult(worker=worker, connection=_connection())


class Executor:
    def __init__(self, stdout: str = "", *, error: SshCommandError | None = None) -> None:
        self.stdout = stdout
        self.error = error
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        assert connection.provider_worker_id == "pod-123"
        self.calls.append((remote_argv, input_text, timeout_seconds))
        if self.error is not None:
            raise self.error
        return SshCommandResult(exit_code=0, stdout=self.stdout, stderr="")


class FakeStorage:
    """Duck-typed S3 storage double recording presign and verification requests."""

    def __init__(self, *, exists: bool = False) -> None:
        self.bucket = "private-wavcse"
        self.exists = exists
        self.presign_download_calls: list[tuple[str, int | None]] = []
        self.presign_upload_calls: list[tuple[str, int | None, bool]] = []
        self.writable_calls: list[tuple[str, bool]] = []
        self.verify_calls: list[tuple[str, int | None]] = []

    def object_key(self, key: str) -> str:
        return f"wavcse/{key}"

    def presign_download(self, key: str, *, expires_in_seconds: int | None = None):  # type: ignore[no-untyped-def]
        self.presign_download_calls.append((key, expires_in_seconds))
        return self._presigned("get", key, expires_in_seconds)

    def presign_upload(
        self, key: str, *, expires_in_seconds: int | None = None, overwrite: bool = False
    ):  # type: ignore[no-untyped-def]
        self.presign_upload_calls.append((key, expires_in_seconds, overwrite))
        return self._presigned("put", key, expires_in_seconds, if_none_match=not overwrite)

    def require_writable(self, key: str, *, overwrite: bool) -> None:
        self.writable_calls.append((key, overwrite))
        if not overwrite and self.exists:
            raise StorageObjectExistsError(
                f"s3://{self.bucket}/{self.object_key(key)} already exists; pass --overwrite "
                "to replace a persisted artifact deliberately"
            )

    def verify_object(
        self,
        key: str,
        *,
        expected_size: int | None = None,
        manifest: object = None,
    ) -> StorageVerification:
        self.verify_calls.append((key, expected_size))
        return StorageVerification(
            key=self.object_key(key),
            size_bytes=expected_size or 0,
            expected_size_checked=expected_size is not None,
        )

    def _presigned(
        self,
        operation: str,
        key: str,
        expires_in_seconds: int | None,
        *,
        if_none_match: bool = False,
    ) -> PresignedUrl:
        return PresignedUrl(
            operation=operation,  # type: ignore[arg-type]
            bucket=self.bucket,
            key=self.object_key(key),
            expires_in_seconds=expires_in_seconds or 3600,
            url=PRESIGNED_URL,
            if_none_match=if_none_match,
        )


def _ssh_config(tmp_path) -> SshConfig:  # type: ignore[no-untyped-def]
    return SshConfig(
        private_key=tmp_path / "not-used-by-fake",
        known_hosts_file=tmp_path / "known_hosts",
        transfer_timeout_seconds=1800,
    )


def _transfer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    stdout: str = "",
    *,
    error=None,  # type: ignore[no-untyped-def]
) -> tuple[WorkerArtifactTransfer, Waiter, Executor]:  # type: ignore[no-untyped-def]
    waiter = Waiter()
    executor = Executor(stdout, error=error)
    return WorkerArtifactTransfer(waiter, executor, _ssh_config(tmp_path)), waiter, executor  # type: ignore[arg-type]


def _protocol(
    operation: str = "download",
    *,
    status: str = "ok",
    path: str = DESTINATION,
    size: str = "3072",
    sha256: str = DIGEST,
    extra: tuple[str, ...] = (),
) -> str:
    lines = [
        "wavcse_transfer_schema\t1",
        f"operation\t{operation}",
        f"status\t{status}",
        f"path\t{path}",
        f"size_bytes\t{size}",
        f"sha256\t{sha256}",
        *extra,
    ]
    return "\n".join(lines) + "\n"


def test_parse_transfer_output_returns_structured_completion_information() -> None:
    result = parse_transfer_output(_protocol(), expected_operation="download")

    assert result.operation == "download"
    assert result.path == DESTINATION
    assert result.size_bytes == 3072
    assert result.sha256 == DIGEST


def test_parse_transfer_output_normalizes_digest_case() -> None:
    result = parse_transfer_output(_protocol(sha256=DIGEST.upper()), expected_operation="download")

    assert result.sha256 == DIGEST


@pytest.mark.parametrize(
    "output",
    [
        "no protocol here\n",
        "wavcse_transfer_schema\t2\nstatus\tok\n",
        _protocol() + "wavcse_transfer_schema\t1\n",
        _protocol(extra=("path\t/other\n",)),
        _protocol(extra=("unknown\tvalue\n",)),
        "wavcse_transfer_schema\t1\nstatus\tok\npath\t/x\n",
        _protocol(status="failed"),
        _protocol(operation="upload"),
        _protocol(size="not-a-number"),
        _protocol(size="-5"),
        _protocol(sha256="zz"),
        "wavcse_transfer_schema\t1\nstatus\tok\nmalformed line\n",
        "",
    ],
)
def test_malformed_worker_protocol_is_rejected(output: str) -> None:
    with pytest.raises(ArtifactTransferError):
        parse_transfer_output(output, expected_operation="download")


def test_download_presigns_for_the_worker_and_keeps_the_url_off_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, waiter, executor = _transfer(monkeypatch, tmp_path, _protocol())
    storage = FakeStorage()

    result = transfer.download("pod-123", storage=storage, key=KEY, destination=DESTINATION)  # type: ignore[arg-type]

    assert result.size_bytes == 3072
    assert storage.presign_download_calls == [(KEY, None)]
    assert waiter.calls == [("pod-123", None)]
    remote_argv, input_text, timeout = executor.calls[0]
    assert remote_argv == ("python3", "-", "download", "--destination", DESTINATION)
    assert "X-Amz-Signature" not in " ".join(remote_argv)
    assert input_text is not None
    assert input_text.startswith(f"WAVCSE_PRESIGNED_URL = {PRESIGNED_URL!r}\n")
    assert "def download(" in input_text
    assert timeout == 1800


def test_download_passes_verification_expectations_and_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, waiter, executor = _transfer(monkeypatch, tmp_path, _protocol())
    storage = FakeStorage()

    transfer.download(  # type: ignore[arg-type]
        "pod-123",
        storage=storage,
        key=KEY,
        destination=DESTINATION,
        expected_size=3072,
        expected_sha256=DIGEST,
        overwrite=True,
        expires_in_seconds=600,
        wait_timeout_seconds=30,
        command_timeout_seconds=900,
    )

    assert storage.presign_download_calls == [(KEY, 600)]
    assert waiter.calls == [("pod-123", 30)]
    assert executor.calls[0][0] == (
        "python3",
        "-",
        "download",
        "--destination",
        DESTINATION,
        "--expected-size",
        "3072",
        "--expected-sha256",
        DIGEST,
        "--overwrite",
    )
    assert executor.calls[0][2] == 900


def test_download_rejects_a_relative_destination_before_presigning(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, waiter, executor = _transfer(monkeypatch, tmp_path, _protocol())
    storage = FakeStorage()

    with pytest.raises(ArtifactTransferError) as error:
        transfer.download("pod-123", storage=storage, key=KEY, destination="relative.tar")  # type: ignore[arg-type]

    assert "absolute path" in str(error.value)
    assert storage.presign_download_calls == []
    assert waiter.calls == []
    assert executor.calls == []


def test_download_wraps_ssh_failures_without_leaking_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, _, _ = _transfer(
        monkeypatch,
        tmp_path,
        error=SshCommandError("Remote command on worker pod-123 exited 1: storage refused"),
    )

    with pytest.raises(ArtifactTransferError) as error:
        transfer.download(  # type: ignore[arg-type]
            "pod-123", storage=FakeStorage(), key=KEY, destination=DESTINATION
        )

    message = str(error.value)
    assert "Download failed on RunPod worker pod-123" in message
    assert "X-Amz-Signature" not in message


def test_download_includes_remote_diagnostics_when_the_protocol_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, _, _ = _transfer(monkeypatch, tmp_path, "partial output only\n")

    with pytest.raises(ArtifactTransferError) as error:
        transfer.download(  # type: ignore[arg-type]
            "pod-123", storage=FakeStorage(), key=KEY, destination=DESTINATION
        )

    assert "remote stdout=" in str(error.value)


def test_worker_cannot_report_a_different_path_or_echo_the_url_as_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, _, _ = _transfer(monkeypatch, tmp_path, _protocol(path=PRESIGNED_URL))

    with pytest.raises(ArtifactTransferError) as error:
        transfer.download(  # type: ignore[arg-type]
            "pod-123", storage=FakeStorage(), key=KEY, destination=DESTINATION
        )

    assert "different transfer path" in str(error.value)
    assert "X-Amz-Signature" not in str(error.value)
    assert "deadbeef" not in str(error.value)


def test_upload_verifies_the_stored_object_after_the_worker_reports_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, _, executor = _transfer(
        monkeypatch,
        tmp_path,
        _protocol("upload", path="/workspace/voxceleb.tar", size="3072"),
    )
    storage = FakeStorage()

    outcome = transfer.upload("pod-123", storage=storage, source="/workspace/voxceleb.tar", key=KEY)  # type: ignore[arg-type]

    assert storage.writable_calls == [(KEY, False)]
    assert storage.presign_upload_calls == [(KEY, None, False)]
    assert storage.verify_calls == [(KEY, 3072)]
    assert outcome.result.operation == "upload"
    assert outcome.verification.size_bytes == 3072
    assert executor.calls[0][0] == ("python3", "-", "upload", "--source", "/workspace/voxceleb.tar")
    assert "WAVCSE_IF_NONE_MATCH = True\n" in executor.calls[0][1]


def test_upload_refuses_to_replace_a_persisted_artifact_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    transfer, waiter, executor = _transfer(monkeypatch, tmp_path, _protocol("upload"))
    storage = FakeStorage(exists=True)

    with pytest.raises(StorageObjectExistsError):
        transfer.upload("pod-123", storage=storage, source="/workspace/voxceleb.tar", key=KEY)  # type: ignore[arg-type]

    assert storage.presign_upload_calls == []
    assert waiter.calls == []
    assert executor.calls == []


def test_upload_allows_a_deliberate_replacement(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    transfer, _, _ = _transfer(
        monkeypatch, tmp_path, _protocol("upload", path="/workspace/voxceleb.tar")
    )
    storage = FakeStorage(exists=True)

    transfer.upload(  # type: ignore[arg-type]
        "pod-123",
        storage=storage,
        source="/workspace/voxceleb.tar",
        key=KEY,
        overwrite=True,
    )

    assert storage.writable_calls == [(KEY, True)]
    assert storage.presign_upload_calls == [(KEY, None, True)]


def test_upload_requires_an_absolute_source(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    transfer, _, executor = _transfer(monkeypatch, tmp_path, _protocol("upload"))
    storage = FakeStorage()

    with pytest.raises(ArtifactTransferError):
        transfer.upload("pod-123", storage=storage, source="voxceleb.tar", key=KEY)  # type: ignore[arg-type]

    assert storage.presign_upload_calls == []
    assert executor.calls == []


@pytest.mark.parametrize(
    "url",
    ["http://bucket.s3.amazonaws.com/key", "file:///etc/passwd", "", "https://bucket/key\nvalue"],
)
def test_presigned_url_assignment_refuses_unsafe_urls(url: str) -> None:
    with pytest.raises(ArtifactTransferError):
        presigned_url_assignment(url)


def test_presigned_url_assignment_is_a_safe_python_literal() -> None:
    quoted = "https://bucket.s3.amazonaws.com/wavcse/a'b?X-Amz-Signature=deadbeef"

    line = presigned_url_assignment(quoted)

    assert line.startswith("WAVCSE_PRESIGNED_URL = ")
    assert ast.literal_eval(line.split("=", 1)[1].strip()) == quoted


def test_streamed_worker_module_stays_worker_safe() -> None:
    """The streamed module is the transfer payload, so its imports are a contract.

    It runs on a worker with Python 3 only, and the controller prepends one statement
    before its first line, so package imports and future imports would both break it.
    """

    source = load_worker_transfer_source()

    assert "import wavcse_infra" not in source
    assert "from wavcse_infra" not in source
    assert "\nfrom __future__ import" not in source
    assert "import boto3" not in source
    assert "\nWAVCSE_PRESIGNED_URL: str = globals()" in source


def test_transfer_never_puts_the_presigned_url_on_any_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the real OpenSSH argv builder and capture the actual subprocess argv."""

    private_key = tmp_path / "worker-key"
    private_key.write_text("unit-test-key", encoding="utf-8")
    private_key.chmod(0o600)
    config = SshConfig(
        private_key=private_key,
        known_hosts_file=tmp_path / "state" / "known_hosts",
        transfer_timeout_seconds=60,
    )
    captured: dict[str, object] = {}

    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        captured["shell"] = kwargs.get("shell")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=_protocol("download", path=DESTINATION),
            stderr="",
        )

    executor = SshExecutor(config, runner=runner)  # type: ignore[arg-type]
    transfer = WorkerArtifactTransfer(Waiter(), executor, config)

    transfer.download("pod-123", storage=FakeStorage(), key=KEY, destination=DESTINATION)  # type: ignore[arg-type]

    argv = captured["argv"]
    assert isinstance(argv, tuple)
    command_line = " ".join(str(part) for part in argv)
    assert "X-Amz-Signature" not in command_line
    assert "wavcse_transfer_schema" not in command_line
    assert argv[0] == "ssh"
    assert "-F" in argv
    assert "python3" in command_line
    assert captured["shell"] is False
    stdin = captured["input"]
    assert isinstance(stdin, str)
    assert stdin.startswith(f"WAVCSE_PRESIGNED_URL = {PRESIGNED_URL!r}\n")
    assert "def download(" in stdin


def test_storage_verification_never_claims_content_verification() -> None:
    verification = StorageVerification(key="wavcse/a.tar", size_bytes=5)

    assert verification.content_checksum_verified is False
    assert verification.limitations
