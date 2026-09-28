"""Offline coverage for resumable parallel ranged worker downloads.

Every test drives the real transfer module against a scripted `urlopen` double, so no
socket, no AWS, and no RunPod resource is involved. The double serves inclusive HTTP byte
ranges from one payload and can inject transient failures, truncated bodies, protocol
violations, and slow ranges to exercise concurrency and resume behaviour deterministically.
"""

from __future__ import annotations

import ast
import collections
import hashlib
import http.client
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from wavcse_infra.storage import worker_transfer
from wavcse_infra.storage.transfer import load_worker_transfer_source
from wavcse_infra.storage.worker_transfer import (
    DEFAULT_DOWNLOAD_CONCURRENCY,
    ERROR_KEY,
    MAX_DOWNLOAD_CONCURRENCY,
    RESUME_SCHEMA_VERSION,
    TransferError,
    TransferInputError,
    TransferVerificationError,
    download,
    main,
)

URL = (
    "https://private-wavcse.s3.ap-south-1.amazonaws.com/wavcse/embeddings/v1/a.tar"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeefcafebabe"
)
URL_2 = (
    "https://private-wavcse.s3.ap-south-1.amazonaws.com/wavcse/embeddings/v1/a.tar"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=0123456789abcdef"
)
PAYLOAD = bytes(range(251)) * 3
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
RANGE_SIZE = 100
THRESHOLD = 32
PARTIAL_SUFFIX = ".wavcse-partial"
METADATA_SUFFIX = ".wavcse-partial.json"
LOCK_SUFFIX = ".wavcse-transfer.lock"


@pytest.fixture(autouse=True)
def small_ranged_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the production thresholds so tiny payloads exercise the ranged path."""

    monkeypatch.setattr(worker_transfer, "PARALLEL_DOWNLOAD_THRESHOLD_BYTES", THRESHOLD)
    monkeypatch.setattr(worker_transfer, "DOWNLOAD_RANGE_SIZE_BYTES", RANGE_SIZE)
    monkeypatch.setattr(worker_transfer, "RETRY_BACKOFF_SECONDS", 0.0)


class ScriptedResponse:
    """Minimal `urlopen` response double with scripted status, headers, and body."""

    def __init__(
        self,
        *,
        status: int,
        headers: dict[str, str],
        body: Any,
        on_close: Any = None,
    ) -> None:
        self.status = status
        self.headers = headers
        self._body = body
        self._on_close = on_close

    def read(self, size: int = -1) -> bytes:
        return self._body(self, size)

    def __enter__(self) -> ScriptedResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        callback = self._on_close
        if callback is not None:
            self._on_close = None
            callback()
        return False


class RangeServer:
    """Scripted range-capable endpoint double; safe to call from worker threads."""

    def __init__(
        self,
        payload: bytes = PAYLOAD,
        *,
        ranges_supported: bool = True,
    ) -> None:
        self.payload = payload
        self.ranges_supported = ranges_supported
        self.lock = threading.Lock()
        self.requests: list[tuple[str, int | None, int | None]] = []
        self.attempts: collections.Counter[int] = collections.Counter()
        self.completions: list[int] = []
        self.read_sizes: list[int] = []
        self.failures: dict[int, list[str]] = {}
        self.delays: dict[int, float] = {}
        self.fail_all: str | None = None
        self.on_range_complete: Any = None
        self.active = 0
        self.max_active = 0
        self.full_requests = 0

    def fail(self, start: int, *behaviors: str) -> None:
        self.failures.setdefault(start, []).extend(behaviors)

    def delay(self, start: int, seconds: float) -> None:
        self.delays[start] = seconds

    @property
    def starts(self) -> list[int]:
        return [start for _, start, _ in self.requests if start is not None]

    def __call__(self, target: Any, timeout: float | None = None) -> ScriptedResponse:
        request = target if isinstance(target, urllib.request.Request) else None
        url = request.full_url if request is not None else str(target)
        header = request.get_header("Range") if request is not None else None
        if header is None:
            with self.lock:
                self.requests.append((url, None, None))
                self.full_requests += 1
            return self._full_response()
        start_text, end_text = header.removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text)
        with self.lock:
            self.requests.append((url, start, end))
            self.attempts[start] += 1
            if not self.ranges_supported:
                return self._full_response()
            if self.fail_all is not None:
                behavior = self.fail_all
            else:
                behaviors = self.failures.get(start)
                behavior = behaviors.pop(0) if behaviors else "ok"
            delay = self.delays.get(start, 0.0)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        if behavior in {"url_error", "http_503", "http_403"}:
            with self.lock:
                self.active -= 1
            if behavior == "url_error":
                raise urllib.error.URLError(f"tunnel failed for {url}")
            code = 503 if behavior == "http_503" else 403
            raise urllib.error.HTTPError(url, code, "synthetic", {}, None)
        if delay:
            time.sleep(delay)
        return self._range_response(start, end, behavior)

    def _slice(
        self,
        data: bytes,
        *,
        stop_after: int | None = None,
        error: Any = None,
        corrupt: bool = False,
    ) -> Any:
        state = {"offset": 0}

        def body(response: ScriptedResponse, size: int = -1) -> bytes:
            with self.lock:
                if size is not None and size >= 0:
                    self.read_sizes.append(size)
            if error is not None and (stop_after is None or state["offset"] >= stop_after):
                raise error
            if size is None or size < 0:
                chunk = data[state["offset"] :]
            else:
                chunk = data[state["offset"] : state["offset"] + size]
            if stop_after is not None:
                chunk = chunk[: max(0, stop_after - state["offset"])]
            state["offset"] += len(chunk)
            if corrupt:
                chunk = bytes(len(chunk))
            return chunk

        return body

    def _closed(self, start: int | None) -> Any:
        def callback() -> None:
            with self.lock:
                self.active -= 1
                if start is not None:
                    self.completions.append(start)
            if start is not None and self.on_range_complete is not None:
                self.on_range_complete(start)

        return callback

    def _range_response(self, start: int, end: int, behavior: str) -> ScriptedResponse:
        data = self.payload[start : end + 1]
        body_data = data
        body_error: Any = None
        stop_after: int | None = None
        corrupt = False
        headers = {"Content-Range": f"bytes {start}-{end}/{len(self.payload)}"}
        headers["Content-Length"] = str(len(data))
        half = max(1, len(data) // 2)
        if behavior == "truncate":
            body_data = data[:half]
        elif behavior == "extra_bytes":
            body_data = data + b"!"
        elif behavior == "wrong_range":
            headers["Content-Range"] = f"bytes {start + 1}-{end}/{len(self.payload)}"
        elif behavior == "total_mismatch":
            headers["Content-Range"] = f"bytes {start}-{end}/{len(self.payload) + 1}"
        elif behavior == "no_content_range":
            headers.pop("Content-Range")
        elif behavior == "wrong_length":
            headers["Content-Length"] = str(len(data) + 5)
        elif behavior == "reset_after_bytes":
            stop_after = half
            body_error = ConnectionResetError("connection reset by peer")
        elif behavior == "timeout_after_bytes":
            stop_after = half
            body_error = TimeoutError("timed out reading the body")
        elif behavior == "incomplete_read":
            body_error = http.client.IncompleteRead(b"", len(data))
        elif behavior == "corrupt_then_reset":
            stop_after = half
            corrupt = True
            body_error = ConnectionResetError("connection reset by peer")
        elif behavior == "url_in_error":
            body_error = OSError(f"read failed for {URL}")
        return ScriptedResponse(
            status=206,
            headers=headers,
            body=self._slice(body_data, stop_after=stop_after, error=body_error, corrupt=corrupt),
            on_close=self._closed(start),
        )

    def _full_response(self) -> ScriptedResponse:
        return ScriptedResponse(
            status=200,
            headers={"Content-Length": str(len(self.payload))},
            body=self._slice(self.payload),
            on_close=self._closed(None),
        )


def _install(monkeypatch: pytest.MonkeyPatch, server: RangeServer) -> RangeServer:
    monkeypatch.setattr(urllib.request, "urlopen", server)
    return server


def _download(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    server: RangeServer,
    *,
    name: str = "artifact.tar",
    url: str = URL,
    expected_size: int | None = None,
    expected_sha256: str | None = DIGEST,
    overwrite: bool = False,
    concurrency: int = DEFAULT_DOWNLOAD_CONCURRENCY,
) -> Path:
    _install(monkeypatch, server)
    destination = tmp_path / name
    result = download(
        url,
        str(destination),
        expected_size=len(server.payload) if expected_size is None else expected_size,
        expected_sha256=expected_sha256,
        overwrite=overwrite,
        concurrency=concurrency,
    )
    assert result["path"] == str(destination)
    return destination


def _metadata(tmp_path: Path, name: str = "artifact.tar") -> dict[str, Any]:
    payload = (tmp_path / f"{name}{METADATA_SUFFIX}").read_text(encoding="utf-8")
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def _range_starts(server: RangeServer, url: str) -> set[int]:
    return {start for used, start, _ in server.requests if used == url and start is not None}


def _visible(tmp_path: Path, name: str = "artifact.tar") -> list[str]:
    """Directory entries other than the destination lock every download keeps."""

    return sorted(
        entry.name for entry in tmp_path.iterdir() if not entry.name.endswith(LOCK_SUFFIX)
    )


def _residue(tmp_path: Path, name: str = "artifact.tar") -> list[str]:
    return sorted(
        entry.name
        for entry in tmp_path.iterdir()
        if entry.name.startswith(f"{name}{PARTIAL_SUFFIX}")
    )


def test_a_small_artifact_keeps_the_single_connection_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer(b"small-artifact")

    destination = _download(monkeypatch, tmp_path, server, expected_sha256=None)

    assert server.full_requests == 1
    assert server.starts == []
    assert destination.read_bytes() == b"small-artifact"
    assert _visible(tmp_path) == [destination.name]


def test_a_small_artifact_discards_stale_ranged_state_for_its_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer(b"small-artifact")
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(b"stale-ranged-partial")
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text("{}", encoding="utf-8")

    _install(monkeypatch, server)
    download(URL, str(destination), expected_size=len(server.payload), expected_sha256=None)

    assert destination.read_bytes() == b"small-artifact"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "artifact.tar",
        "artifact.tar.wavcse-transfer.lock",
    ]


def test_every_range_is_fetched_once_with_inclusive_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()

    destination = _download(monkeypatch, tmp_path, server)

    assert sorted({(start, end) for _, start, end in server.requests if start is not None}) == [
        (0, 99),
        (100, 199),
        (200, 299),
        (300, 399),
        (400, 499),
        (500, 599),
        (600, 699),
        (700, 752),
    ]
    assert destination.read_bytes() == PAYLOAD
    assert destination.stat().st_size == len(PAYLOAD)
    assert _residue(tmp_path) == []


def test_an_exactly_divisible_artifact_has_no_short_final_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = bytes(range(200)) * 4  # 800 bytes, exactly eight 100-byte ranges.
    server = RangeServer(payload)

    destination = _download(
        monkeypatch, tmp_path, server, expected_sha256=hashlib.sha256(payload).hexdigest()
    )

    ranges = sorted((start, end) for _, start, end in server.requests if start is not None)
    assert len(ranges) == 8
    assert ranges[-1] == (700, 799)
    assert destination.read_bytes() == payload


def test_ranges_that_finish_out_of_order_still_assemble_correctly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.delay(0, 0.05)

    destination = _download(monkeypatch, tmp_path, server, concurrency=2)

    assert server.completions[0] != 0
    assert destination.read_bytes() == PAYLOAD


def test_a_transient_range_failure_is_retried_and_recovered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(100, "http_503")

    destination = _download(monkeypatch, tmp_path, server)

    assert server.attempts[100] == 2
    assert destination.read_bytes() == PAYLOAD


def test_range_retries_are_bounded_and_keep_resumable_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(200, *["url_error"] * 10)

    with pytest.raises(TransferError) as error:
        _download(monkeypatch, tmp_path, server, concurrency=1)

    assert "failed after 4 attempts" in str(error.value)
    assert server.attempts[200] == 4
    assert not (tmp_path / "artifact.tar").exists()
    partial = tmp_path / "artifact.tar.wavcse-partial"
    assert partial.exists()
    completed = set(_metadata(tmp_path)["completed_ranges"])
    assert {0, 1} <= completed
    assert 2 not in completed


def test_a_truncated_range_body_is_detected_and_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "truncate")

    destination = _download(monkeypatch, tmp_path, server)

    assert server.attempts[0] == 2
    assert destination.read_bytes() == PAYLOAD


def test_a_permanently_truncated_range_fails_without_materializing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, *["truncate"] * 10)

    with pytest.raises(TransferError) as error:
        _download(monkeypatch, tmp_path, server, concurrency=1)

    assert "byte range 0-99 failed" in str(error.value)
    assert not (tmp_path / "artifact.tar").exists()


def test_a_wrong_content_range_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "wrong_range")

    with pytest.raises(TransferError) as error:
        _download(monkeypatch, tmp_path, server)

    assert "requested range 0-99" in str(error.value)
    assert server.attempts[0] == 1
    assert not (tmp_path / "artifact.tar").exists()


def test_a_ranged_response_must_include_content_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "no_content_range")

    with pytest.raises(TransferError, match="Content-Range"):
        _download(monkeypatch, tmp_path, server)

    assert server.attempts[0] == 1


def test_a_ranged_response_must_announce_the_requested_length(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "wrong_length")

    with pytest.raises(TransferError, match="bytes were requested"):
        _download(monkeypatch, tmp_path, server)

    assert server.attempts[0] == 1


def test_extra_bytes_beyond_the_requested_range_are_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "extra_bytes")

    with pytest.raises(TransferError, match="more bytes than the requested byte range"):
        _download(monkeypatch, tmp_path, server)

    assert server.attempts[0] == 1


def test_an_ignored_range_header_falls_back_to_one_sequential_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer(ranges_supported=False)

    destination = _download(monkeypatch, tmp_path, server)

    assert server.full_requests == 1
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_an_expired_presigned_url_fails_actionably_without_retrying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "http_403")

    with pytest.raises(TransferError) as error:
        _download(monkeypatch, tmp_path, server)

    message = str(error.value)
    assert "HTTP 403" in message
    assert "may have expired" in message
    assert "X-Amz-Signature" not in message
    assert URL not in message
    assert server.attempts[0] == 1


def test_a_content_range_total_mismatch_discards_the_partial_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "total_mismatch")

    with pytest.raises(TransferVerificationError, match="announces 754 bytes"):
        _download(monkeypatch, tmp_path, server)

    assert not (tmp_path / "artifact.tar").exists()
    assert _residue(tmp_path) == []


def test_the_complete_artifact_digest_is_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = (bytes(range(251)) * 3)[:-1] + b"\xff"  # same length, different content.
    assert len(other) == len(PAYLOAD) and other != PAYLOAD
    server = RangeServer(other)

    with pytest.raises(TransferVerificationError, match="SHA-256") as error:
        _download(monkeypatch, tmp_path, server)

    message = str(error.value)
    assert hashlib.sha256(other).hexdigest() in message
    assert DIGEST in message
    assert not (tmp_path / "artifact.tar").exists()
    assert _residue(tmp_path) == []


def test_a_forged_complete_resume_state_is_rejected_by_the_final_size_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(PAYLOAD[:400])
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text(
        json.dumps(
            {
                "schema_version": RESUME_SCHEMA_VERSION,
                "artifact": {
                    "destination": str(destination),
                    "size_bytes": len(PAYLOAD),
                    "sha256": DIGEST,
                    "range_size_bytes": RANGE_SIZE,
                },
                "completed_ranges": list(range(8)),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TransferVerificationError, match="753 bytes were expected"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert server.requests == []
    assert _residue(tmp_path) == []


def test_resume_state_records_ranges_but_never_the_presigned_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(200, *["url_error"] * 10)

    with pytest.raises(TransferError):
        _download(monkeypatch, tmp_path, server, concurrency=1)

    text = (tmp_path / "artifact.tar.wavcse-partial.json").read_text(encoding="utf-8")
    assert "X-Amz-Signature" not in text
    assert "deadbeef" not in text
    assert URL not in text
    state = _metadata(tmp_path)
    assert state["artifact"] == {
        "destination": str(tmp_path / "artifact.tar"),
        "size_bytes": len(PAYLOAD),
        "sha256": DIGEST,
        "range_size_bytes": RANGE_SIZE,
    }


def test_a_second_invocation_resumes_only_the_missing_ranges_with_a_new_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(200, *["url_error"] * 10)

    with pytest.raises(TransferError):
        _download(monkeypatch, tmp_path, server, concurrency=1)

    completed = set(_metadata(tmp_path)["completed_ranges"])
    assert {0, 1} <= completed
    assert 2 not in completed

    server.failures.clear()
    destination = _download(monkeypatch, tmp_path, server, url=URL_2)

    all_starts = {index * RANGE_SIZE for index in range(8)}
    completed_starts = {index * RANGE_SIZE for index in completed}
    assert _range_starts(server, URL_2) == all_starts - completed_starts
    assert server.attempts[0] == 1
    assert server.attempts[100] == 1
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_incompatible_resume_metadata_is_discarded_and_restarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(b"stale-bytes")
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text(
        json.dumps(
            {
                "schema_version": RESUME_SCHEMA_VERSION,
                "artifact": {
                    "destination": str(destination),
                    "size_bytes": len(PAYLOAD),
                    "sha256": "f" * 64,
                    "range_size_bytes": RANGE_SIZE,
                },
                "completed_ranges": list(range(8)),
            }
        ),
        encoding="utf-8",
    )

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_unreadable_resume_metadata_is_discarded_and_restarted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(b"stale-bytes")
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text("{not json", encoding="utf-8")

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert destination.read_bytes() == PAYLOAD


def test_a_stale_partial_without_metadata_is_reinitialized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(PAYLOAD[:50])

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert destination.read_bytes() == PAYLOAD


def test_two_artifacts_with_the_same_destination_never_share_partial_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first = RangeServer()
    first.fail(200, *["url_error"] * 10)
    with pytest.raises(TransferError):
        _download(monkeypatch, tmp_path, first, concurrency=1)

    other = bytes([1]) + PAYLOAD[1:]
    second = RangeServer(other)
    destination = _download(
        monkeypatch,
        tmp_path,
        second,
        expected_sha256=hashlib.sha256(other).hexdigest(),
    )

    assert sorted(second.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert destination.read_bytes() == other


def test_a_valid_existing_destination_is_preserved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"already-materialized")
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(b"stale-partial")
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text("{}", encoding="utf-8")

    with pytest.raises(TransferVerificationError, match="already exists"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert destination.read_bytes() == b"already-materialized"
    assert server.requests == []
    assert (tmp_path / "artifact.tar.wavcse-partial").read_bytes() == b"stale-partial"


def _hold_destination_lock(destination: Path) -> int:
    """Hold one destination's transfer lock from an independent file descriptor."""

    descriptor = os.open(
        f"{destination}{LOCK_SUFFIX}",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    worker_transfer.fcntl.flock(
        descriptor, worker_transfer.fcntl.LOCK_EX | worker_transfer.fcntl.LOCK_NB
    )
    return descriptor


def test_a_second_transfer_for_one_destination_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    held = _hold_destination_lock(destination)
    try:
        with pytest.raises(TransferError, match="already in progress"):
            download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)
    finally:
        os.close(held)

    assert server.requests == []
    assert not destination.exists()
    assert _residue(tmp_path) == []


def test_the_transfer_lock_does_not_depend_on_the_staging_pathname(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A completed placement removes the staging name, so the lock must outlive it."""

    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    metadata = tmp_path / "artifact.tar.wavcse-partial.json"
    partial.write_bytes(b"staged-bytes")
    metadata.write_text("{}", encoding="utf-8")
    held = _hold_destination_lock(destination)
    try:
        # Simulate the completion transition: staging data and record disappear while the
        # first writer still holds its lock.
        partial.unlink()
        metadata.unlink()
        with pytest.raises(TransferError, match="already in progress"):
            download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)
    finally:
        os.close(held)

    assert server.requests == []
    assert not destination.exists()


def test_a_refused_transfer_never_deletes_another_invocations_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    metadata = tmp_path / "artifact.tar.wavcse-partial.json"
    partial.write_bytes(b"first-writer-partial")
    metadata.write_text('{"sentinel": true}', encoding="utf-8")
    held = _hold_destination_lock(destination)
    try:
        with pytest.raises(TransferError, match="already in progress"):
            download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)
    finally:
        os.close(held)

    assert partial.read_bytes() == b"first-writer-partial"
    assert metadata.read_text(encoding="utf-8") == '{"sentinel": true}'
    assert server.requests == []


def test_a_concurrent_transfer_cannot_race_the_completion_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    server.delay(0, 0.2)
    destination = tmp_path / "artifact.tar"
    in_flight = threading.Event()
    server.on_range_complete = lambda _start: in_flight.set()
    outcome: dict[str, Any] = {}

    def first() -> None:
        try:
            outcome["result"] = download(
                URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST
            )
        except BaseException as exc:
            outcome["result"] = exc

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert in_flight.wait(timeout=5)
        with pytest.raises(TransferError, match="already in progress"):
            download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)
    finally:
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcome["result"], dict)
    assert destination.read_bytes() == PAYLOAD


@pytest.mark.parametrize("destination", ["relative.tar", "/workspace/dir/", ""])
def test_unsafe_destinations_are_rejected_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, destination: str
) -> None:
    server = _install(monkeypatch, RangeServer())

    with pytest.raises(TransferInputError):
        download(URL, destination, expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert server.requests == []


def test_range_bodies_are_read_in_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    monkeypatch.setattr(worker_transfer, "CHUNK_SIZE_BYTES", 8)

    destination = _download(monkeypatch, tmp_path, server)

    assert server.read_sizes
    assert max(server.read_sizes) <= 8
    assert destination.read_bytes() == PAYLOAD


@pytest.mark.parametrize("concurrency", [3, MAX_DOWNLOAD_CONCURRENCY])
def test_active_requests_never_exceed_the_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, concurrency: int
) -> None:
    server = RangeServer()
    for start in range(0, len(PAYLOAD), RANGE_SIZE):
        server.delay(start, 0.02)

    destination = _download(monkeypatch, tmp_path, server, concurrency=concurrency)

    assert server.max_active <= concurrency
    if concurrency < len(range(0, len(PAYLOAD), RANGE_SIZE)):
        assert server.max_active >= 2
    assert destination.read_bytes() == PAYLOAD


def test_the_destination_appears_only_after_the_whole_artifact_is_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    destination = tmp_path / "artifact.tar"
    observed: list[bool] = []
    server.on_range_complete = lambda _start: observed.append(destination.exists())

    _download(monkeypatch, tmp_path, server)

    assert observed
    assert not any(observed)
    assert destination.read_bytes() == PAYLOAD


def test_download_concurrency_bounds_are_enforced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())

    for value in (0, -1, MAX_DOWNLOAD_CONCURRENCY + 1):
        with pytest.raises(TransferInputError, match="concurrency"):
            download(
                URL,
                str(tmp_path / "artifact.tar"),
                expected_size=len(PAYLOAD),
                expected_sha256=DIGEST,
                concurrency=value,
            )

    assert server.requests == []


def test_the_worker_cli_accepts_concurrency_and_rejects_a_bad_bound(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"

    exit_code = main(
        [
            "download",
            "--destination",
            str(destination),
            "--expected-size",
            str(len(PAYLOAD)),
            "--expected-sha256",
            DIGEST,
            "--concurrency",
            "3",
        ],
        url=URL,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert server.max_active <= 3
    assert destination.read_bytes() == PAYLOAD

    stderr = io.StringIO()
    exit_code = main(
        ["download", "--destination", str(tmp_path / "second.tar"), "--concurrency", "0"],
        url=URL,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert "concurrency" in stderr.getvalue()


def test_the_streamed_worker_module_still_imports_only_the_standard_library() -> None:
    tree = ast.parse(load_worker_transfer_source())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    assert roots
    assert roots <= set(sys.stdlib_module_names)
    assert "wavcse_infra" not in roots
    assert "boto3" not in roots


def test_a_symlinked_resumable_staging_path_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"victim-content")
    (tmp_path / "artifact.tar.wavcse-partial").symlink_to(victim)

    with pytest.raises(TransferError, match="resumable staging") as error:
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert "victim-content" not in str(error.value)
    assert victim.read_bytes() == b"victim-content"
    assert not destination.exists()
    assert server.requests == []


def test_a_hard_linked_staging_file_is_refused_without_truncating_the_shared_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"victim-content" * 8)
    os.link(victim, tmp_path / "artifact.tar.wavcse-partial")
    # An incompatible record would otherwise authorize a reset that truncates the file.
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text(
        json.dumps(
            {
                "schema_version": RESUME_SCHEMA_VERSION,
                "artifact": {},
                "completed_ranges": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TransferError, match="hard links"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert victim.read_bytes() == b"victim-content" * 8
    assert not destination.exists()
    assert server.requests == []


def test_a_symlinked_resume_record_is_ignored_and_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    victim = tmp_path / "victim.json"
    victim.write_text("victim-content", encoding="utf-8")
    (tmp_path / "artifact.tar.wavcse-partial.json").symlink_to(victim)

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert victim.read_text(encoding="utf-8") == "victim-content"
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert not (tmp_path / "artifact.tar.wavcse-partial.json").exists()


def test_a_hard_linked_resume_record_is_not_trusted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    victim = tmp_path / "victim.json"
    record = json.dumps(
        {
            "schema_version": RESUME_SCHEMA_VERSION,
            "artifact": {
                "destination": str(destination),
                "size_bytes": len(PAYLOAD),
                "sha256": DIGEST,
                "range_size_bytes": RANGE_SIZE,
            },
            "completed_ranges": list(range(8)),
        }
    )
    victim.write_text(record, encoding="utf-8")
    os.link(victim, tmp_path / "artifact.tar.wavcse-partial.json")

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert victim.read_text(encoding="utf-8") == record


def test_a_staging_path_swapped_during_the_download_is_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    decoy = tmp_path / "decoy.bin"
    swapped = threading.Event()

    def swap(_start: int) -> None:
        if swapped.is_set():
            return
        swapped.set()
        os.rename(partial, decoy)
        partial.write_bytes(b"decoy")

    server.on_range_complete = swap
    with pytest.raises(TransferError, match="replaced by another file"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert not destination.exists()
    assert partial.read_bytes() == b"decoy"
    assert decoy.exists()


def test_placement_links_the_open_descriptor_not_a_swapped_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    decoy = tmp_path / "decoy.bin"
    # Close only the window between the placement recheck and the link itself.
    monkeypatch.setattr(worker_transfer, "_recheck_staging_path", lambda *args, **kwargs: None)
    swapped = threading.Event()

    def swap(_start: int) -> None:
        if swapped.is_set():
            return
        swapped.set()
        os.rename(partial, decoy)
        partial.write_bytes(b"decoy")

    server.on_range_complete = swap
    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert decoy.read_bytes() == PAYLOAD
    assert not partial.exists()


def test_a_dangling_symlink_destination_counts_as_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    destination.symlink_to(tmp_path / "missing-target")

    with pytest.raises(TransferVerificationError, match="already exists"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert server.requests == []
    assert destination.is_symlink()


def test_overwrite_replaces_a_destination_symlink_without_following_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"victim-content")
    destination.symlink_to(victim)

    result = download(
        URL,
        str(destination),
        expected_size=len(PAYLOAD),
        expected_sha256=DIGEST,
        overwrite=True,
    )

    assert result["sha256"] == DIGEST
    assert not destination.is_symlink()
    assert destination.read_bytes() == PAYLOAD
    assert victim.read_bytes() == b"victim-content"


def test_a_body_connection_reset_after_some_bytes_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(100, "reset_after_bytes")

    destination = _download(monkeypatch, tmp_path, server)

    assert server.attempts[100] == 2
    assert destination.read_bytes() == PAYLOAD


def test_a_body_read_timeout_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = RangeServer()
    server.fail(0, "timeout_after_bytes")

    destination = _download(monkeypatch, tmp_path, server)

    assert server.attempts[0] == 2
    assert destination.read_bytes() == PAYLOAD


def test_an_incomplete_read_exception_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(0, "incomplete_read")

    destination = _download(monkeypatch, tmp_path, server)

    assert server.attempts[0] == 2
    assert destination.read_bytes() == PAYLOAD


def test_body_read_retries_are_bounded_and_surface_a_controlled_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail_all = "reset_after_bytes"

    with pytest.raises(TransferError) as error:
        _download(monkeypatch, tmp_path, server, concurrency=1)

    message = str(error.value)
    assert "byte range 0-99 failed after 4 attempts" in message
    assert "connection reset by peer" in message
    assert server.attempts[0] == 4
    assert not (tmp_path / "artifact.tar").exists()


def test_a_url_inside_a_body_read_exception_is_never_surfaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail_all = "url_in_error"

    with pytest.raises(TransferError) as error:
        _download(monkeypatch, tmp_path, server, concurrency=1)

    message = str(error.value)
    assert URL not in message
    assert "X-Amz-Signature" not in message
    assert "deadbeefcafebabe" not in message
    assert "<redacted-url>" in message


def test_a_failed_attempt_cannot_corrupt_a_later_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(300, "corrupt_then_reset")

    destination = _download(monkeypatch, tmp_path, server)

    assert server.attempts[300] == 2
    assert destination.read_bytes() == PAYLOAD
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == DIGEST


def test_the_worker_cli_reports_a_body_failure_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    server.fail_all = "url_in_error"
    stderr = io.StringIO()

    exit_code = main(
        [
            "download",
            "--destination",
            str(tmp_path / "artifact.tar"),
            "--expected-size",
            str(len(PAYLOAD)),
            "--expected-sha256",
            DIGEST,
            "--concurrency",
            "1",
        ],
        url=URL,
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    text = stderr.getvalue()
    assert text.startswith(f"{ERROR_KEY}\t")
    assert "X-Amz-Signature" not in text
    assert URL not in text


def test_a_ranged_download_without_a_digest_cannot_reuse_previous_ranges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail(200, *["url_error"] * 10)
    with pytest.raises(TransferError):
        _download(monkeypatch, tmp_path, server, concurrency=1)
    assert _metadata(tmp_path)["completed_ranges"]

    server.failures.clear()
    destination = _download(monkeypatch, tmp_path, server, url=URL_2, expected_sha256=None)

    assert _range_starts(server, URL_2) == {0, 100, 200, 300, 400, 500, 600, 700}
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_a_digest_less_download_never_trusts_a_complete_looking_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = bytes([9]) + PAYLOAD[1:]  # same size, different content
    server = _install(monkeypatch, RangeServer(PAYLOAD))
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(other)
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text(
        json.dumps(
            {
                "schema_version": RESUME_SCHEMA_VERSION,
                "artifact": {
                    "destination": str(destination),
                    "size_bytes": len(PAYLOAD),
                    "sha256": hashlib.sha256(other).hexdigest(),
                    "range_size_bytes": RANGE_SIZE,
                },
                "completed_ranges": list(range(8)),
            }
        ),
        encoding="utf-8",
    )

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=None)

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert _residue(tmp_path) == []


def test_a_version_one_resume_record_is_never_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    (tmp_path / "artifact.tar.wavcse-partial").write_bytes(PAYLOAD)
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact": {
                    "destination": str(destination),
                    "size_bytes": len(PAYLOAD),
                    "sha256": DIGEST,
                    "range_size_bytes": RANGE_SIZE,
                },
                "completed_ranges": list(range(8)),
            }
        ),
        encoding="utf-8",
    )

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]


class RecordingPool:
    """Executor double that records submissions and lifecycle calls."""

    def __init__(
        self,
        max_workers: int,
        *,
        fail_submit_on: int | None = None,
        interrupt_submit_on: int | None = None,
    ) -> None:
        self.max_workers = max_workers
        self.submits = 0
        self.shutdowns = 0
        self._fail_submit_on = fail_submit_on
        self._interrupt_submit_on = interrupt_submit_on
        self._inner = ThreadPoolExecutor(max_workers=max_workers)

    def submit(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        self.submits += 1
        if self._interrupt_submit_on is not None and self.submits == self._interrupt_submit_on:
            raise KeyboardInterrupt("scheduling interrupted")
        if self._fail_submit_on is not None and self.submits == self._fail_submit_on:
            raise RuntimeError("cannot schedule new futures after shutdown")
        return self._inner.submit(function, *args, **kwargs)

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        self.shutdowns += 1
        self._inner.shutdown(wait=wait, cancel_futures=cancel_futures)


def _recording_pool_factory(pools: list[RecordingPool], **options: Any) -> Any:
    def factory(*, max_workers: int) -> RecordingPool:
        pool = RecordingPool(max_workers, **options)
        pools.append(pool)
        return pool

    return factory


def test_the_number_of_scheduled_ranges_is_bounded_by_concurrency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    server.fail_all = "url_error"
    monkeypatch.setattr(worker_transfer, "DOWNLOAD_RANGE_SIZE_BYTES", 1)
    pools: list[RecordingPool] = []
    monkeypatch.setattr(worker_transfer, "ThreadPoolExecutor", _recording_pool_factory(pools))

    with pytest.raises(TransferError):
        download(
            URL,
            str(tmp_path / "artifact.tar"),
            expected_size=4096,
            expected_sha256=DIGEST,
            concurrency=4,
        )

    assert pools and pools[0].submits <= 4
    assert pools[0].shutdowns == 1


def test_a_submission_failure_is_controlled_and_joins_running_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    for start in range(0, len(PAYLOAD), RANGE_SIZE):
        server.delay(start, 0.05)
    pools: list[RecordingPool] = []
    monkeypatch.setattr(
        worker_transfer,
        "ThreadPoolExecutor",
        _recording_pool_factory(pools, fail_submit_on=3),
    )

    with pytest.raises(TransferError, match="the ranged download failed"):
        download(
            URL, str(tmp_path / "artifact.tar"), expected_size=len(PAYLOAD), expected_sha256=DIGEST
        )

    assert pools[0].submits == 3
    assert pools[0].shutdowns == 1
    settled = len(server.completions)
    time.sleep(0.05)
    assert len(server.completions) == settled
    assert not (tmp_path / "artifact.tar").exists()
    assert (tmp_path / "artifact.tar.wavcse-partial").exists()


def test_an_interrupted_scheduling_run_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    for start in range(0, len(PAYLOAD), RANGE_SIZE):
        server.delay(start, 0.05)
    pools: list[RecordingPool] = []
    monkeypatch.setattr(
        worker_transfer,
        "ThreadPoolExecutor",
        _recording_pool_factory(pools, interrupt_submit_on=3),
    )

    with pytest.raises(KeyboardInterrupt):
        download(
            URL, str(tmp_path / "artifact.tar"), expected_size=len(PAYLOAD), expected_sha256=DIGEST
        )

    assert pools[0].shutdowns == 1
    settled = len(server.completions)
    time.sleep(0.05)
    assert len(server.completions) == settled
    assert not (tmp_path / "artifact.tar").exists()


def test_no_range_write_continues_after_a_failure_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    server.fail(0, *["url_error"] * 10)
    server.delay(100, 0.2)

    with pytest.raises(TransferError):
        download(
            URL,
            str(tmp_path / "artifact.tar"),
            expected_size=len(PAYLOAD),
            expected_sha256=DIGEST,
            concurrency=2,
        )

    settled = len(server.completions)
    time.sleep(0.05)
    assert len(server.completions) == settled
    assert not (tmp_path / "artifact.tar").exists()


def test_completed_placement_residue_is_recovered_before_redownload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A crash between placement and cleanup leaves a hard link that must not block."""

    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    destination.write_bytes(PAYLOAD)
    os.link(destination, partial)
    (tmp_path / "artifact.tar.wavcse-partial.json").write_text(
        json.dumps(
            {
                "schema_version": RESUME_SCHEMA_VERSION,
                "artifact": {
                    "destination": str(destination),
                    "size_bytes": len(PAYLOAD),
                    "sha256": DIGEST,
                    "range_size_bytes": RANGE_SIZE,
                },
                "completed_ranges": list(range(8)),
            }
        ),
        encoding="utf-8",
    )

    result = download(
        URL,
        str(destination),
        expected_size=len(PAYLOAD),
        expected_sha256=DIGEST,
        overwrite=True,
    )

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert sorted(server.starts) == [0, 100, 200, 300, 400, 500, 600, 700]
    assert _residue(tmp_path) == []


def test_a_range_with_a_failed_body_read_is_never_recorded_as_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = RangeServer()
    server.fail_all = "reset_after_bytes"

    with pytest.raises(TransferError):
        _download(monkeypatch, tmp_path, server, concurrency=1)

    assert server.attempts[0] == 4
    assert _metadata(tmp_path)["completed_ranges"] == []
    assert not (tmp_path / "artifact.tar").exists()


def test_a_small_download_is_serialized_by_the_destination_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer(b"small-artifact"))
    destination = tmp_path / "artifact.tar"
    held = _hold_destination_lock(destination)
    try:
        with pytest.raises(TransferError, match="already in progress"):
            download(
                URL,
                str(destination),
                expected_size=len(server.payload),
                expected_sha256=None,
            )
    finally:
        os.close(held)

    assert server.requests == []
    assert not destination.exists()


def test_a_size_unknown_download_is_serialized_by_the_destination_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer(b"small-artifact"))
    destination = tmp_path / "artifact.tar"
    held = _hold_destination_lock(destination)
    try:
        with pytest.raises(TransferError, match="already in progress"):
            download(URL, str(destination))
    finally:
        os.close(held)

    assert server.requests == []
    assert not destination.exists()


def test_a_non_resumable_ranged_download_is_serialized_by_the_destination_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    held = _hold_destination_lock(destination)
    try:
        with pytest.raises(TransferError, match="already in progress"):
            download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=None)
    finally:
        os.close(held)

    assert server.requests == []
    assert not destination.exists()


def test_every_download_keeps_an_empty_destination_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    small = _install(monkeypatch, RangeServer(b"small-artifact"))
    small_destination = tmp_path / "small.tar"
    download(URL, str(small_destination), expected_size=len(small.payload), expected_sha256=None)

    large = RangeServer()
    _download(monkeypatch, tmp_path, large, name="large.tar")

    for name in ("small.tar", "large.tar"):
        lock = tmp_path / f"{name}{LOCK_SUFFIX}"
        assert lock.exists()
        assert lock.stat().st_size == 0


def test_overwrite_placement_uses_the_verified_inode_not_a_swapped_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An adversary that replaces the staging pathname cannot change what is placed."""

    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"previous-artifact")
    partial = tmp_path / "artifact.tar.wavcse-partial"
    decoy = tmp_path / "decoy.bin"
    monkeypatch.setattr(worker_transfer, "_recheck_staging_path", lambda *args, **kwargs: None)
    swapped = threading.Event()

    def swap(_start: int) -> None:
        if swapped.is_set():
            return
        swapped.set()
        os.rename(partial, decoy)
        partial.write_bytes(b"decoy")

    server.on_range_complete = swap
    result = download(
        URL,
        str(destination),
        expected_size=len(PAYLOAD),
        expected_sha256=DIGEST,
        overwrite=True,
    )

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert decoy.read_bytes() == PAYLOAD
    assert not partial.exists()


def test_overwrite_refuses_a_destination_that_appears_during_placement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A racing writer that takes the destination name is never replaced by us."""

    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"previous-artifact")
    attacker = tmp_path / "attacker.bin"
    attacker.write_bytes(b"attacker-bytes")
    real_link = worker_transfer.os.link

    def racing_link(source: str, target: str, **kwargs: Any) -> None:
        directory_fd = kwargs.get("dst_dir_fd")
        real_link(attacker, target, dst_dir_fd=directory_fd)
        real_link(source, target, **kwargs)

    monkeypatch.setattr(worker_transfer.os, "link", racing_link)

    with pytest.raises(TransferVerificationError, match="appeared during the download"):
        download(
            URL,
            str(destination),
            expected_size=len(PAYLOAD),
            expected_sha256=DIGEST,
            overwrite=True,
        )

    assert destination.read_bytes() == b"attacker-bytes"
    assert (tmp_path / "artifact.tar.wavcse-partial").exists()
    assert server.requests


def test_the_no_procfs_fallback_removes_a_substituted_destination_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without descriptor links, a substituted link target is detected and undone."""

    _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    attacker = tmp_path / "attacker.bin"
    attacker.write_bytes(b"attacker-bytes")
    monkeypatch.setattr(worker_transfer, "_PROC_FD_ROOT", str(tmp_path / "absent-procfs"))
    real_link = worker_transfer.os.link

    def substituted_link(source: str, target: str, **kwargs: Any) -> None:
        directory_fd = kwargs.get("dst_dir_fd")
        real_link(attacker, target, dst_dir_fd=directory_fd)

    monkeypatch.setattr(worker_transfer.os, "link", substituted_link)

    with pytest.raises(TransferError, match="did not receive the verified artifact"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert not destination.exists()
    assert attacker.read_bytes() == b"attacker-bytes"


def test_a_crash_before_placement_leaves_resumable_state_without_a_link_wedge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Dying between removing the old entry and linking the verified inode is recoverable."""

    _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    destination.write_bytes(b"previous-artifact")
    original = worker_transfer._link_verified_inode

    def dying_link(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt("simulated crash before placement")

    monkeypatch.setattr(worker_transfer, "_link_verified_inode", dying_link)
    with pytest.raises(KeyboardInterrupt):
        download(
            URL,
            str(destination),
            expected_size=len(PAYLOAD),
            expected_sha256=DIGEST,
            overwrite=True,
        )

    assert os.stat(tmp_path / "artifact.tar.wavcse-partial").st_nlink == 1
    assert _metadata(tmp_path)["artifact"]["sha256"] == DIGEST

    monkeypatch.setattr(worker_transfer, "_link_verified_inode", original)
    result = download(
        URL,
        str(destination),
        expected_size=len(PAYLOAD),
        expected_sha256=DIGEST,
        overwrite=True,
    )

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_a_crash_after_placement_is_recovered_on_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A staging name left hard-linked to the placed artifact is recovered, not wedged."""

    _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    real_unlink = worker_transfer.os.unlink

    def crashing_unlink(path: Any, *, dir_fd: int | None = None, **kwargs: Any) -> None:
        if str(path).endswith(f"{PARTIAL_SUFFIX}") and dir_fd is not None:
            raise KeyboardInterrupt("simulated crash after placement")
        real_unlink(path, dir_fd=dir_fd, **kwargs)

    monkeypatch.setattr(worker_transfer.os, "unlink", crashing_unlink)
    with pytest.raises(KeyboardInterrupt):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert destination.read_bytes() == PAYLOAD
    assert os.stat(tmp_path / "artifact.tar.wavcse-partial").st_nlink == 2

    monkeypatch.setattr(worker_transfer.os, "unlink", real_unlink)
    result = download(
        URL,
        str(destination),
        expected_size=len(PAYLOAD),
        expected_sha256=DIGEST,
        overwrite=True,
    )

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_harvested_ranges_are_persisted_when_scheduling_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ranges that finish while the transfer shuts down remain resumable afterwards."""

    server = _install(monkeypatch, RangeServer())
    for start in range(0, len(PAYLOAD), RANGE_SIZE):
        server.delay(start, 0.05)
    pools: list[RecordingPool] = []
    monkeypatch.setattr(
        worker_transfer,
        "ThreadPoolExecutor",
        _recording_pool_factory(pools, fail_submit_on=3),
    )

    with pytest.raises(TransferError, match="the ranged download failed"):
        download(
            URL,
            str(tmp_path / "artifact.tar"),
            expected_size=len(PAYLOAD),
            expected_sha256=DIGEST,
        )

    assert _metadata(tmp_path)["completed_ranges"] == [0, 1]
    assert pools[0].shutdowns == 1

    monkeypatch.setattr(worker_transfer, "ThreadPoolExecutor", ThreadPoolExecutor)
    server.delays.clear()
    destination = _download(monkeypatch, tmp_path, server, url=URL_2)

    assert _range_starts(server, URL_2) == {200, 300, 400, 500, 600, 700}
    assert destination.read_bytes() == PAYLOAD
    assert _residue(tmp_path) == []


def test_a_stale_placement_link_is_recovered_instead_of_wedging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Crash residue from an earlier build's pre-replace boundary must not wedge state."""

    _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    residue = tmp_path / "artifact.tar.wavcse-stage-4242-deadbeef"
    partial.write_bytes(PAYLOAD[: 2 * RANGE_SIZE])
    os.link(partial, residue)
    assert partial.stat().st_nlink == 2

    result = download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert result["sha256"] == DIGEST
    assert destination.read_bytes() == PAYLOAD
    assert not residue.exists()
    assert _residue(tmp_path) == []


def test_a_foreign_hard_link_to_a_staging_file_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recovering our own placement links must not weaken the shared-file refusal."""

    server = _install(monkeypatch, RangeServer())
    destination = tmp_path / "artifact.tar"
    partial = tmp_path / "artifact.tar.wavcse-partial"
    foreign = tmp_path / "foreign.bin"
    foreign.write_bytes(b"foreign-content")
    os.link(foreign, partial)

    with pytest.raises(TransferError, match="hard links"):
        download(URL, str(destination), expected_size=len(PAYLOAD), expected_sha256=DIGEST)

    assert foreign.read_bytes() == b"foreign-content"
    assert os.stat(foreign).st_nlink == 2
    assert not destination.exists()
    assert server.requests == []
