from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from wavcse_infra.config import RunPodConfig
from wavcse_infra.errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderNotFoundError,
    ProviderResponseError,
    ProviderUnavailableError,
)
from wavcse_infra.models import WorkerState
from wavcse_infra.providers.runpod import RunPodClient


def _config(
    *,
    token: str = "fake-runpod-token",
    attempts: int = 3,
    backoff: float = 0.25,
) -> RunPodConfig:
    return RunPodConfig(
        api_url="https://rest.runpod.test/v1",
        api_key=SecretStr(token),
        request_timeout_seconds=2,
        max_read_attempts=attempts,
        retry_backoff_seconds=backoff,
    )


def _pod_payload(**overrides):
    payload = {
        "id": "pod-123",
        "name": "training-worker",
        "adjustedCostPerHr": 0.69,
        "costPerHr": "0.74",
        "desiredStatus": "RUNNING",
        "gpu": {"id": "gpu-id", "count": 1, "displayName": "NVIDIA RTX 4090"},
        "machine": {
            "dataCenterId": "EU-RO-1",
            "location": "Romania",
            "gpuTypeId": "fallback-gpu-id",
        },
        "portMappings": {"22": 10341},
        "publicIp": "203.0.113.10",
        "image": "runpod/pytorch:example",
        "interruptible": False,
        "lastStartedAt": "2026-09-27T10:00:00Z",
        "providerFieldAddedLater": "ignored",
    }
    payload.update(overrides)
    return payload


def test_list_workers_uses_documented_endpoint_and_normalizes_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url == httpx.URL("https://rest.runpod.test/v1/pods?includeMachine=true")
        assert request.headers["Authorization"] == "Bearer fake-runpod-token"
        return httpx.Response(200, json=[_pod_payload()])

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        workers = client.list_workers()

    worker = workers[0]
    assert worker.provider == "runpod"
    assert worker.id == "pod-123"
    assert worker.state is WorkerState.RUNNING
    assert worker.native_status == "RUNNING"
    assert worker.gpu_type == "NVIDIA RTX 4090"
    assert worker.gpu_count == 1
    assert worker.hourly_cost == Decimal("0.69")
    assert worker.base_hourly_cost == Decimal("0.74")
    assert worker.public_ip == "203.0.113.10"
    assert worker.ssh_port == 10341
    assert worker.datacenter == "EU-RO-1"
    assert worker.last_started_at is not None
    assert worker.last_started_at.isoformat() == "2026-09-27T10:00:00+00:00"


def test_show_worker_percent_encodes_provider_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == (b"/v1/pods/pod%2Fwith%20spaces?includeMachine=true")
        return httpx.Response(200, json=_pod_payload(id="pod/with spaces"))

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        worker = client.get_worker("pod/with spaces")

    assert worker.id == "pod/with spaces"


@pytest.mark.parametrize(
    ("native_status", "expected_state"),
    [
        ("EXITED", WorkerState.STOPPED),
        ("TERMINATED", WorkerState.DESTROYED),
        ("provider-added-state", WorkerState.UNKNOWN),
        (None, WorkerState.UNKNOWN),
    ],
)
def test_status_normalization_preserves_unknown_provider_state(
    native_status: str | None, expected_state: WorkerState
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_pod_payload(desiredStatus=native_status),
            request=request,
        )
    )

    with RunPodClient(_config(), transport=transport) as client:
        worker = client.get_worker("pod-123")

    assert worker.state is expected_state
    assert worker.native_status == native_status


def test_safe_read_retries_transient_response_with_exponential_backoff() -> None:
    statuses = iter((503, 200))
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(
            status,
            json=[_pod_payload()] if status == 200 else {},
            request=request,
        )

    client = RunPodClient(
        _config(),
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    )
    with client:
        workers = client.list_workers()

    assert len(workers) == 1
    assert delays == [0.25]


def test_retry_backoff_is_capped() -> None:
    statuses = iter((503, 503, 200))
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(
            status,
            json=[_pod_payload()] if status == 200 else {},
            request=request,
        )

    with RunPodClient(
        _config(backoff=20),
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    ) as client:
        client.list_workers()

    assert delays == [20, 30]


def test_authentication_failure_is_not_retried_or_leaked() -> None:
    calls = 0
    secret = "a-credential-that-must-not-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"echo": secret}, request=request)

    with (
        RunPodClient(
            _config(token=secret),
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(ProviderAuthenticationError) as captured,
    ):
        client.list_workers()

    assert calls == 1
    assert secret not in str(captured.value)


def test_retry_exhaustion_is_bounded() -> None:
    delays: list[float] = []
    transport = httpx.MockTransport(lambda request: httpx.Response(429, json={}, request=request))

    with (
        RunPodClient(
            _config(attempts=3),
            transport=transport,
            sleep=delays.append,
        ) as client,
        pytest.raises(ProviderUnavailableError, match=r"3 attempt\(s\).+HTTP 429"),
    ):
        client.list_workers()

    assert delays == [0.25, 0.5]


def test_transport_failure_is_retried_and_sanitized() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("could not connect", request=request)

    with (
        RunPodClient(
            _config(attempts=2),
            transport=httpx.MockTransport(handler),
            sleep=lambda delay: None,
        ) as client,
        pytest.raises(ProviderUnavailableError, match=r"after 2 attempt\(s\)"),
    ):
        client.list_workers()

    assert calls == 2


def test_show_worker_reports_not_found_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={}, request=request)

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderNotFoundError, match="missing-pod"),
    ):
        client.get_worker("missing-pod")

    assert calls == 1


def test_redirect_is_a_non_retryable_provider_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://example.test"}, request=request)

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderError, match="HTTP 302"),
    ):
        client.list_workers()

    assert calls == 1


def test_invalid_json_and_schema_are_actionable_provider_errors() -> None:
    responses = iter(
        (
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json=[{"name": "missing-id"}]),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        response = next(responses)
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers=response.headers,
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderResponseError, match="invalid JSON"):
            client.list_workers()
        with pytest.raises(ProviderResponseError, match=r"0\.id: Field required"):
            client.list_workers()


def test_normalization_validation_failure_is_a_provider_response_error() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=[_pod_payload(portMappings={"22": 70000})],
            request=request,
        )
    )

    with (
        RunPodClient(_config(), transport=transport) as client,
        pytest.raises(ProviderResponseError, match=r"ssh_port"),
    ):
        client.list_workers()
