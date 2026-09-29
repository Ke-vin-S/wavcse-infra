import fcntl
import hashlib
import io
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wavcse_infra.storage import worker_transfer
from wavcse_infra.storage.worker_transfer import (
    ERROR_KEY,
    SCHEMA_KEY,
    SCHEMA_VERSION,
    TransferError,
    TransferInputError,
    TransferVerificationError,
    download,
    main,
    sha256_file,
    upload,
    validate_presigned_url,
    validate_worker_path,
)

URL = (
    "https://private-wavcse.s3.ap-south-1.amazonaws.com/wavcse/embeddings/v1/a.tar"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeefcafebabe"
)
PAYLOAD = b"wavcse-artifact-payload" * 3
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
LOCK_SUFFIX = ".wavcse-transfer.lock"


def _staged_entries(directory: Path) -> list[str]:
    """Directory entries other than the destination lock every download keeps."""

    return sorted(
        entry.name for entry in directory.iterdir() if not entry.name.endswith(LOCK_SUFFIX)
    )


class FakeResponse:
    """Minimal urlopen response double with scripted bodies and failures."""

    def __init__(
        self,
        payload: bytes = PAYLOAD,
        *,
        status: int = 200,
        content_length: int | None = len(PAYLOAD),
        body=None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.headers: dict[str, str] = (
            {} if content_length is None else {"Content-Length": str(content_length)}
        )
        self.body = body
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self.body is not None:
            return self.body(self, size)
        if size is None or size < 0:
            chunk = self.payload[self._offset :]
        else:
            chunk = self.payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeTransport:
    """Records urlopen targets and serves one scripted response or failure."""

    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response if response is not None else FakeResponse()
        self.error = error
        self.calls: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []
        self.uploaded: list[bytes] = []

    def __call__(self, target, timeout=None):  # type: ignore[no-untyped-def]
        if isinstance(target, urllib.request.Request):
            self.calls.append(target)
            body = target.data
            if hasattr(body, "read"):
                self.uploaded.append(body.read())
        else:
            self.calls.append(target)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.response


def _install(monkeypatch: pytest.MonkeyPatch, transport: FakeTransport) -> FakeTransport:
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    return transport


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, f"synthetic {code}", {}, None)


def _write_source(directory: Path, payload: bytes = PAYLOAD) -> Path:
    source = directory / "artifact.tar"
    source.write_bytes(payload)
    return source


def test_sha256_file_matches_hashlib_for_a_streamed_read(tmp_path: Path) -> None:
    source = _write_source(tmp_path)

    size, digest = sha256_file(str(source), chunk_size=7)

    assert size == len(PAYLOAD)
    assert digest == DIGEST


def test_sha256_file_reports_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(TransferError) as error:
        sha256_file(str(tmp_path / "missing.tar"))

    assert "could not read" in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "  " + URL,
        URL + " ",
        "http://bucket.s3.amazonaws.com/wavcse/a.tar",
        "file:///etc/passwd",
        "ftp://bucket/wavcse/a.tar",
        "https:///wavcse/a.tar",
        URL + "\n",
        URL.replace("a.tar", "a b.tar"),
    ],
)
def test_unsafe_transfer_targets_are_rejected(url: str) -> None:
    with pytest.raises(TransferInputError):
        validate_presigned_url(url)


def test_https_presigned_url_is_accepted_unchanged() -> None:
    assert validate_presigned_url(URL) == URL


def test_worker_paths_must_be_absolute_files() -> None:
    assert validate_worker_path("/workspace/a.tar", label="destination") == "/workspace/a.tar"

    for path in ("", "relative/a.tar", "  /workspace/a.tar", "/workspace/dir/", " /x"):
        with pytest.raises(TransferInputError):
            validate_worker_path(path, label="destination")


def test_download_materializes_the_verified_artifact_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"

    result = download(URL, str(destination))

    assert destination.read_bytes() == PAYLOAD
    assert result == {"path": str(destination), "size_bytes": str(len(PAYLOAD)), "sha256": DIGEST}
    assert _staged_entries(tmp_path) == ["voxceleb.tar"]
    assert transport.timeouts == [worker_transfer.DEFAULT_TIMEOUT_SECONDS]


def test_download_never_materializes_an_incomplete_transfer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = {"reads": 0}

    def body(response: FakeResponse, size: int) -> bytes:
        state["reads"] += 1
        if state["reads"] > 1:
            raise OSError("connection reset")
        return PAYLOAD[:8]

    transport = _install(monkeypatch, FakeTransport(response=FakeResponse(body=body)))
    destination = tmp_path / "voxceleb.tar"

    with pytest.raises(TransferError) as error:
        download(URL, str(destination))

    assert "connection reset" in str(error.value)
    assert not destination.exists()
    assert _staged_entries(tmp_path) == []
    assert len(transport.calls) == 1


def test_download_rejects_a_size_mismatch_and_removes_the_temporary_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport(response=FakeResponse(content_length=None)))
    destination = tmp_path / "voxceleb.tar"

    with pytest.raises(TransferVerificationError) as error:
        download(URL, str(destination), expected_size=len(PAYLOAD) + 1)

    assert "downloaded" in str(error.value)
    assert not destination.exists()
    assert _staged_entries(tmp_path) == []


def test_download_fails_fast_when_the_announced_size_is_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport(response=FakeResponse(content_length=99)))
    destination = tmp_path / "voxceleb.tar"

    with pytest.raises(TransferVerificationError) as error:
        download(URL, str(destination), expected_size=len(PAYLOAD))

    assert "announces 99 bytes" in str(error.value)
    assert _staged_entries(tmp_path) == []


def test_download_rejects_a_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"

    with pytest.raises(TransferVerificationError) as error:
        download(URL, str(destination), expected_sha256="0" * 64)

    assert DIGEST in str(error.value)
    assert not destination.exists()
    assert _staged_entries(tmp_path) == []


def test_download_accepts_a_matching_expectation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"

    result = download(
        URL,
        str(destination),
        expected_size=len(PAYLOAD),
        expected_sha256=DIGEST.upper(),
    )

    assert result["sha256"] == DIGEST


def test_download_reports_http_failure_without_materializing_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport(error=_http_error(403)))
    destination = tmp_path / "voxceleb.tar"

    with pytest.raises(TransferError) as error:
        download(URL, str(destination))

    assert "HTTP 403" in str(error.value)
    assert "X-Amz-Signature" not in str(error.value)
    assert _staged_entries(tmp_path) == []


def test_download_reports_transport_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport(error=urllib.error.URLError("connection refused")))

    with pytest.raises(TransferError) as error:
        download(URL, str(tmp_path / "voxceleb.tar"))

    assert "the download request failed" in str(error.value)


def test_download_refuses_to_replace_a_file_without_explicit_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"
    destination.write_bytes(b"existing")

    with pytest.raises(TransferVerificationError) as error:
        download(URL, str(destination))

    assert "already exists" in str(error.value)
    assert destination.read_bytes() == b"existing"
    assert transport.calls == []


def test_download_replaces_a_file_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"
    destination.write_bytes(b"existing")

    download(URL, str(destination), overwrite=True)

    assert destination.read_bytes() == PAYLOAD


def test_download_refuses_a_file_that_appears_during_the_transfer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination = tmp_path / "voxceleb.tar"
    response = FakeResponse()
    original_read = response.read
    raced = False

    def racing_read(size: int = -1) -> bytes:
        nonlocal raced
        if not raced:
            raced = True
            destination.write_bytes(b"raced")
        return original_read(size)

    response.read = racing_read  # type: ignore[method-assign]
    transport = _install(monkeypatch, FakeTransport(response=response))

    with pytest.raises(TransferVerificationError) as error:
        download(URL, str(destination))

    assert "appeared during the download" in str(error.value)
    assert raced is True
    assert destination.read_bytes() == b"raced"
    assert len(transport.calls) == 1
    assert _staged_entries(tmp_path) == [destination.name]


def test_download_cannot_overwrite_a_file_created_at_materialization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"
    original_link = worker_transfer.os.link

    def racing_link(source: str, target: str, **kwargs: object) -> None:
        destination.write_bytes(b"raced")
        original_link(source, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_transfer.os, "link", racing_link)

    with pytest.raises(TransferVerificationError, match="appeared during the download"):
        download(URL, str(destination))

    assert destination.read_bytes() == b"raced"
    assert _staged_entries(tmp_path) == [destination.name]


@pytest.mark.parametrize("destination", ["relative.tar", "/workspace/dir/", "", " /workspace/x"])
def test_download_requires_an_absolute_file_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, destination: str
) -> None:
    transport = _install(monkeypatch, FakeTransport())

    with pytest.raises(TransferInputError):
        download(URL, destination)

    assert transport.calls == []


def test_download_requires_an_existing_parent_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())

    with pytest.raises(TransferInputError) as error:
        download(URL, str(tmp_path / "missing" / "voxceleb.tar"))

    assert "does not exist" in str(error.value)
    assert transport.calls == []


def test_download_rejects_an_invalid_expected_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())

    with pytest.raises(TransferInputError):
        download(URL, str(tmp_path / "voxceleb.tar"), expected_sha256="not-a-digest")

    assert transport.calls == []


def test_upload_streams_the_file_and_reports_its_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())
    source = _write_source(tmp_path)

    result = upload(URL, str(source))

    assert result == {"path": str(source), "size_bytes": str(len(PAYLOAD)), "sha256": DIGEST}
    request = transport.calls[0]
    assert isinstance(request, urllib.request.Request)
    assert request.method == "PUT"
    assert request.full_url == URL
    assert request.get_header("Content-length") == str(len(PAYLOAD))
    assert not isinstance(request.data, bytes)
    assert transport.uploaded == [PAYLOAD]


def test_upload_sends_the_signed_no_replace_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())
    source = _write_source(tmp_path)

    upload(URL, str(source), if_none_match=True)

    request = transport.calls[0]
    assert isinstance(request, urllib.request.Request)
    assert request.get_header("If-none-match") == "*"


def test_upload_rejects_a_file_above_the_single_put_limit_before_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())
    source = _write_source(tmp_path)
    monkeypatch.setattr(worker_transfer, "MAX_SINGLE_PUT_BYTES", len(PAYLOAD) - 1)

    with pytest.raises(TransferInputError, match="5 GB"):
        upload(URL, str(source))

    assert transport.calls == []


def test_upload_digest_describes_the_bytes_actually_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _write_source(tmp_path)

    class ChangingTransport(FakeTransport):
        def __call__(self, target, timeout=None):  # type: ignore[no-untyped-def]
            source.write_bytes(b"changed-source-bytes" + b"x" * (len(PAYLOAD) - 20))
            return super().__call__(target, timeout=timeout)

    transport = _install(monkeypatch, ChangingTransport())

    result = upload(URL, str(source))

    assert result["sha256"] == hashlib.sha256(transport.uploaded[0]).hexdigest()
    assert result["sha256"] != DIGEST


def test_job_upload_rejects_symlinks_and_lexical_escapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "job"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    outside = _write_source(tmp_path)
    (outputs / "linked-file").symlink_to(outside)
    (workspace / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    transport = _install(monkeypatch, FakeTransport())

    for source in (
        outputs / "linked-file",
        workspace / "linked-dir" / "artifact.tar",
        workspace / ".." / "artifact.tar",
        outside,
    ):
        with pytest.raises((TransferInputError, TransferError)):
            upload(URL, str(source), allowed_root=str(workspace))
    assert transport.calls == []


def test_job_upload_accepts_a_regular_file_inside_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "job"
    outputs = workspace / "outputs"
    outputs.mkdir(parents=True)
    source = _write_source(outputs)
    transport = _install(monkeypatch, FakeTransport())

    result = upload(URL, str(source), allowed_root=str(workspace))

    assert result["sha256"] == DIGEST
    assert transport.uploaded == [PAYLOAD]


def test_job_upload_reports_a_missing_source_as_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A declared output the command never produced is terminal, not resumable.

    `FileNotFoundError` is an `OSError`, so classified as a transient transfer failure it
    made the controller retry a permanently absent file and kept the job record out of a
    terminal state.
    """

    workspace = tmp_path / "job"
    (workspace / "outputs").mkdir(parents=True)
    transport = _install(monkeypatch, FakeTransport())

    with pytest.raises(TransferInputError) as failure:
        upload(URL, str(workspace / "outputs" / "missing.json"), allowed_root=str(workspace))

    message = str(failure.value)
    assert "does not exist" in message
    assert worker_transfer.TRANSIENT_FAILURE_MARKER not in message
    assert transport.calls == []


def test_upload_reports_http_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, FakeTransport(error=_http_error(403)))
    source = _write_source(tmp_path)

    with pytest.raises(TransferError) as error:
        upload(URL, str(source))

    assert "HTTP 403" in str(error.value)
    assert "X-Amz-Signature" not in str(error.value)


def test_upload_reports_transport_failure_without_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(
        monkeypatch,
        FakeTransport(error=urllib.error.URLError(f"tunnel failed for {URL}")),
    )
    source = _write_source(tmp_path)

    with pytest.raises(TransferError) as error:
        upload(URL, str(source))

    message = str(error.value)
    assert "the upload request failed" in message
    assert "X-Amz-Signature" not in message
    assert "<redacted-url>" in message


def test_upload_redacts_a_standalone_signed_parameter_in_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport(error=urllib.error.URLError("X-Amz-Signature=secret")))

    with pytest.raises(TransferError) as error:
        upload(URL, str(_write_source(tmp_path)))

    assert "secret" not in str(error.value)


def test_upload_requires_an_absolute_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    transport = _install(monkeypatch, FakeTransport())

    with pytest.raises(TransferInputError):
        upload(URL, "artifact.tar")
    with pytest.raises(TransferInputError):
        upload(URL, str(tmp_path / "missing.tar"))
    with pytest.raises(TransferInputError):
        upload(URL, str(tmp_path))

    assert transport.calls == []


def test_main_download_emits_the_structured_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        ["download", "--destination", str(destination), "--expected-sha256", DIGEST],
        url=URL,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    lines = stdout.getvalue().splitlines()
    assert lines[0] == f"{SCHEMA_KEY}\t{SCHEMA_VERSION}"
    assert lines[1] == "operation\tdownload"
    assert lines[2] == "status\tok"
    assert lines[3] == f"path\t{destination}"
    assert lines[4] == f"size_bytes\t{len(PAYLOAD)}"
    assert lines[5] == f"sha256\t{DIGEST}"


def test_main_upload_emits_the_structured_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport())
    source = _write_source(tmp_path)
    stdout = io.StringIO()

    exit_code = main(["upload", "--source", str(source)], url=URL, stdout=stdout)

    assert exit_code == 0
    assert "operation\tupload" in stdout.getvalue()
    assert f"sha256\t{DIGEST}" in stdout.getvalue()


def test_main_uses_the_injected_module_url_when_no_argument_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_transfer, "WAVCSE_PRESIGNED_URL", URL)
    _install(monkeypatch, FakeTransport())
    destination = tmp_path / "voxceleb.tar"

    exit_code = main(["download", "--destination", str(destination)], stdout=io.StringIO())

    assert exit_code == 0
    assert destination.read_bytes() == PAYLOAD


def test_main_fails_when_the_url_was_not_injected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(worker_transfer, "WAVCSE_PRESIGNED_URL", "")
    transport = _install(monkeypatch, FakeTransport())
    stderr = io.StringIO()

    exit_code = main(
        ["download", "--destination", str(tmp_path / "voxceleb.tar")],
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert stderr.getvalue().startswith(f"{ERROR_KEY}\t")
    assert transport.calls == []


def test_main_reports_failures_without_printing_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, FakeTransport(error=_http_error(500)))
    stderr = io.StringIO()

    exit_code = main(
        ["upload", "--source", str(_write_source(tmp_path))],
        url=URL,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "HTTP 500" in stderr.getvalue()
    assert "X-Amz-Signature" not in stderr.getvalue()
    assert URL not in stderr.getvalue()


def test_download_reads_the_body_in_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_sizes: list[int] = []
    response = FakeResponse(payload=b"x" * 40)
    original_read = response.read

    def recording_read(size: int = -1) -> bytes:
        read_sizes.append(size)
        return original_read(size)

    response.read = recording_read  # type: ignore[method-assign]
    monkeypatch.setattr(worker_transfer, "CHUNK_SIZE_BYTES", 8)
    _install(monkeypatch, FakeTransport(response=response))

    download(URL, str(tmp_path / "artifact.tar"))

    assert set(read_sizes) == {8}
    assert len(read_sizes) == 6


# --- read-only verification of an artifact that may already be placed --------------


def _verify(tmp_path: Path, name: str = "artifact.tar", **kwargs):
    """Run the verify subcommand in-process against a real local file."""

    destination = str(tmp_path / name)
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = worker_transfer.main(
        ["verify", "--destination", destination, *_verify_arguments(**kwargs)],
        url="",
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue(), destination


def _verify_arguments(**kwargs) -> list[str]:
    arguments: list[str] = []
    if kwargs.get("expected_size") is not None:
        arguments += ["--expected-size", str(kwargs["expected_size"])]
    if kwargs.get("expected_sha256") is not None:
        arguments += ["--expected-sha256", kwargs["expected_sha256"]]
    return arguments


def test_verify_reports_a_complete_matching_artifact(tmp_path: Path) -> None:
    (tmp_path / "artifact.tar").write_bytes(PAYLOAD)

    code, stdout, stderr, destination = _verify(
        tmp_path, expected_size=len(PAYLOAD), expected_sha256=DIGEST
    )

    assert code == 0, stderr
    assert f"{SCHEMA_KEY}\t{SCHEMA_VERSION}" in stdout
    assert "operation\tverify" in stdout
    assert f"path\t{destination}" in stdout
    assert f"size_bytes\t{len(PAYLOAD)}" in stdout
    assert f"sha256\t{DIGEST}" in stdout


def test_verify_needs_no_presigned_url(tmp_path: Path) -> None:
    """A read-only probe carries no bearer material at all."""

    (tmp_path / "artifact.tar").write_bytes(PAYLOAD)

    code, stdout, stderr, _ = _verify(tmp_path, expected_sha256=DIGEST)

    assert code == 0, stderr
    assert "X-Amz-Signature" not in stdout + stderr


def test_verify_reports_an_absent_destination(tmp_path: Path) -> None:
    code, _, stderr, _ = _verify(tmp_path, expected_size=len(PAYLOAD))

    assert code == 1
    assert worker_transfer.DESTINATION_ABSENT_MARKER in stderr
    assert URL not in stderr


def test_verify_reports_incomplete_resumable_state(tmp_path: Path) -> None:
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(b"half")

    code, _, stderr, _ = _verify(tmp_path, expected_size=len(PAYLOAD))

    assert code == 1
    assert worker_transfer.DESTINATION_INCOMPLETE_MARKER in stderr


def test_verify_reports_a_size_that_does_not_match(tmp_path: Path) -> None:
    (tmp_path / "artifact.tar").write_bytes(PAYLOAD)

    code, _, stderr, _ = _verify(tmp_path, expected_size=len(PAYLOAD) + 1)

    assert code == 1
    assert worker_transfer.DESTINATION_INCOMPLETE_MARKER in stderr


def test_verify_reports_a_digest_that_does_not_match(tmp_path: Path) -> None:
    (tmp_path / "artifact.tar").write_bytes(PAYLOAD)

    code, _, stderr, _ = _verify(tmp_path, expected_size=len(PAYLOAD), expected_sha256="f" * 64)

    assert code == 1
    assert worker_transfer.DESTINATION_MISMATCH_MARKER in stderr


def test_verify_reports_an_active_transfer_instead_of_racing_it(tmp_path: Path) -> None:
    """A live transfer holds the destination lock, so verify refuses to judge the file."""

    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"partial")
    lock = tmp_path / "artifact.tar.wavcse-transfer.lock"
    lock.touch()

    with lock.open("rb") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _, stderr, _ = _verify(tmp_path, expected_size=len(PAYLOAD))

    assert code == 1
    assert worker_transfer.TRANSFER_IN_PROGRESS_MARKER in stderr


def test_verify_reports_a_non_regular_destination(tmp_path: Path) -> None:
    (tmp_path / "artifact.tar").mkdir()

    code, _, stderr, _ = _verify(tmp_path, expected_size=1)

    assert code == 1
    assert worker_transfer.DESTINATION_MISMATCH_MARKER in stderr


def test_verify_never_writes_to_the_destination(tmp_path: Path) -> None:
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(PAYLOAD)

    before = sorted(entry.name for entry in tmp_path.iterdir())
    _verify(tmp_path, expected_size=len(PAYLOAD), expected_sha256=DIGEST)
    after = sorted(entry.name for entry in tmp_path.iterdir())

    assert before == after
    assert destination.read_bytes() == PAYLOAD


# --- the verified bytes are the inspected inode, not the pathname ------------------


def test_verify_never_accepts_an_entry_replaced_while_it_was_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swap between inspection and hashing must not turn another file into the artifact."""

    destination = tmp_path / "artifact.tar"
    destination.write_bytes(PAYLOAD)
    decoy = tmp_path / "decoy.tar"
    decoy.write_bytes(PAYLOAD)
    original = worker_transfer.sha256_descriptor

    def swapping(descriptor: int, **kwargs: object) -> tuple[int, str]:
        # Exactly the reported window: the destination name is redirected while the
        # descriptor being hashed still refers to the artifact.
        os.unlink(destination)
        os.symlink(decoy, destination)
        return original(descriptor, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_transfer, "sha256_descriptor", swapping)

    with pytest.raises(worker_transfer.TransferError) as failure:
        worker_transfer.verify_existing(
            str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST
        )

    assert not isinstance(failure.value, worker_transfer.TransferVerificationError)
    assert worker_transfer.TRANSIENT_FAILURE_MARKER in str(failure.value)


def test_verify_reports_a_size_mismatch_that_only_the_swap_could_explain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(PAYLOAD)
    decoy = tmp_path / "decoy.tar"
    decoy.write_bytes(b"decoy")
    original = worker_transfer.sha256_descriptor

    def swapping(descriptor: int, **kwargs: object) -> tuple[int, str]:
        os.unlink(destination)
        os.symlink(decoy, destination)
        return original(descriptor, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_transfer, "sha256_descriptor", swapping)

    with pytest.raises(
        worker_transfer.TransferError, match=worker_transfer.TRANSIENT_FAILURE_MARKER
    ):
        worker_transfer.verify_existing(
            str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST
        )


def test_verify_refuses_to_follow_a_symlink_destination(tmp_path: Path) -> None:
    decoy = tmp_path / "decoy.tar"
    decoy.write_bytes(PAYLOAD)
    destination = tmp_path / "artifact.tar"
    os.symlink(decoy, destination)

    with pytest.raises(
        worker_transfer.TransferVerificationError,
        match=worker_transfer.DESTINATION_MISMATCH_MARKER,
    ):
        worker_transfer.verify_existing(
            str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST
        )


def test_exhausted_range_retries_are_marked_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bounded attempt that ran out of retries keeps its resumable state and says so."""

    def always_retryable(*args: object, **kwargs: object) -> None:
        raise worker_transfer._RetryableRangeError("the byte range request failed: reset")

    monkeypatch.setattr(worker_transfer, "_fetch_range", always_retryable)
    monkeypatch.setattr(worker_transfer.time, "sleep", lambda _seconds: None)
    with (tmp_path / "staging.bin").open("wb") as staging:
        stop = worker_transfer.threading.Event()

        with pytest.raises(worker_transfer.TransferError) as failure:
            worker_transfer._fetch_range_with_retries(
                URL, staging.fileno(), 0, 32 * 1024 * 1024, 1.0, stop
            )

    assert worker_transfer.TRANSIENT_FAILURE_MARKER in str(failure.value)
    assert "after 4 attempts" in str(failure.value)
