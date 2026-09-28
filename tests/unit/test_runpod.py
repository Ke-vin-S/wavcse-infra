import json
import logging
from decimal import Decimal

import httpx
import pytest
from botocore.session import Session as BotocoreSession
from botocore.stub import Stubber
from pydantic import SecretStr

from wavcse_infra.config import RunPodConfig, Settings
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ProviderAuthenticationError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderPermissionError,
    ProviderResponseError,
    ProviderUnavailableError,
    ProviderValidationError,
)
from wavcse_infra.models import Availability, CloudType, WorkerSpec, WorkerState
from wavcse_infra.providers.runpod import RunPodClient

PARAMETER_NAME = "/wavcse-infra/runpod/api-key"


def _config(
    *,
    token: str = "fake-runpod-token",
    attempts: int = 3,
    backoff: float = 0.25,
    reconcile_attempts: int = 3,
) -> RunPodConfig:
    return RunPodConfig(
        api_url="https://api.runpod.test/v2",
        api_key=SecretStr(token),
        request_timeout_seconds=2,
        max_read_attempts=attempts,
        retry_backoff_seconds=backoff,
        create_reconcile_attempts=reconcile_attempts,
    )


def _pod_payload(**overrides):
    payload = {
        "id": "pod-123",
        "name": "wavcse-training-abc123",
        "status": "RUNNING",
        "gpu": {"id": "NVIDIA RTX A5000", "count": 1, "vcpuCount": 8, "memory": 32},
        "cloud": "COMMUNITY",
        "cost": 0.16,
        "dataCenterId": "EU-RO-1",
        "image": "runpod/pytorch:example",
        "template": None,
        "disk": 30,
        "ports": ["22/tcp", "8888/http"],
        "mounts": {"persistent": {"size": 20, "path": "/workspace"}},
        "ssh": {
            "proxy": {
                "host": "ssh.runpod.io",
                "port": 22,
                "username": "pod-route",
            },
            "direct": {
                "host": "203.0.113.10",
                "port": 10341,
                "username": "root",
            },
        },
        "createdAt": "2026-09-28T09:00:00Z",
        "startedAt": "2026-09-28T09:01:00Z",
        "providerFieldAddedLater": "ignored",
    }
    payload.update(overrides)
    return payload


def _pod_list(*pods, next_cursor=None, has_next_page=False):
    return {
        "pods": list(pods),
        "pagination": {"nextCursor": next_cursor, "hasNextPage": has_next_page},
    }


def _gpu_payload(**overrides):
    payload = {
        "id": "NVIDIA RTX A5000",
        "name": "RTX A5000",
        "memory": 24,
        "secure": True,
        "community": True,
        "price": {"secure": 0.27, "community": "0.16"},
        "maxCount": {"secure": 4, "community": 2},
        "availability": "HIGH",
        "dataCenters": [
            {"id": "EU-RO-1", "name": "Romania", "availability": "HIGH"},
            {"id": "US-KS-2", "name": "Kansas", "availability": "LOW"},
        ],
    }
    payload.update(overrides)
    return payload


def _spec(**overrides) -> WorkerSpec:
    values = {
        "name": "wavcse-training-abc123",
        "gpu_type": "NVIDIA RTX A5000",
        "gpu_count": 1,
        "cloud_type": CloudType.COMMUNITY,
        "image": "runpod/pytorch:example",
        "container_disk_gb": 30,
        "volume_gb": 20,
        "volume_mount_path": "/workspace",
        "data_center_ids": ("EU-RO-1",),
        "start_ssh": True,
    }
    values.update(overrides)
    return WorkerSpec.model_validate(values)


def test_client_resolves_ssm_credential_once_for_multiple_reads() -> None:
    secret = "ssm-resolved-runpod-token"
    settings = Settings.model_validate(
        {
            "aws": {"region": "us-east-1"},
            "runpod": {
                "api_url": "https://api.runpod.test/v2",
                "api_key_parameter": PARAMETER_NAME,
            },
        }
    )
    aws_session = BotocoreSession()
    aws_session.set_credentials("unit-test-access", "unit-test-secret", "unit-test-session")
    ssm_client = aws_session.create_client("ssm", region_name="us-east-1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        if request.url.path.endswith("/pods"):
            return httpx.Response(200, json=_pod_list(_pod_payload()), request=request)
        return httpx.Response(200, json=_pod_payload(), request=request)

    with Stubber(ssm_client) as stubber:
        stubber.add_response(
            "get_parameter",
            {
                "Parameter": {
                    "Name": PARAMETER_NAME,
                    "Type": "SecureString",
                    "Value": secret,
                }
            },
            {"Name": PARAMETER_NAME, "WithDecryption": True},
        )
        with RunPodClient.from_settings(
            settings,
            ssm_client=ssm_client,
            transport=httpx.MockTransport(handler),
        ) as client:
            assert len(client.list_workers()) == 1
            assert client.get_worker("pod-123").id == "pod-123"
        stubber.assert_no_pending_responses()


def test_list_workers_paginates_and_normalizes_v2_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/pods"
        assert request.url.params["limit"] == "1000"
        if "cursor" not in request.url.params:
            return httpx.Response(
                200,
                json=_pod_list(_pod_payload(), next_cursor="next-page", has_next_page=True),
                request=request,
            )
        assert request.url.params["cursor"] == "next-page"
        return httpx.Response(
            200,
            json=_pod_list(_pod_payload(id="pod-456", name="second")),
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        workers = client.list_workers()

    assert [worker.id for worker in workers] == ["pod-123", "pod-456"]
    worker = workers[0]
    assert worker.state is WorkerState.RUNNING
    assert worker.native_status == "RUNNING"
    assert worker.gpu_type == "NVIDIA RTX A5000"
    assert worker.gpu_count == 1
    assert worker.cloud_type is CloudType.COMMUNITY
    assert worker.hourly_cost == Decimal("0.16")
    assert worker.public_ip == "203.0.113.10"
    assert worker.ssh_port == 10341
    assert worker.exposed_ports == ("22/tcp", "8888/http")
    assert worker.ssh_proxy is not None
    assert worker.ssh_proxy.provider_worker_id == "pod-123"
    assert worker.ssh_proxy.host == "ssh.runpod.io"
    assert worker.ssh_direct is not None
    assert worker.ssh_direct.provider_worker_id == "pod-123"
    assert worker.ssh_direct.port == 10341
    assert worker.datacenter == "EU-RO-1"
    assert worker.container_disk_gb == 30
    assert worker.volume_gb == 20
    assert worker.volume_mount_path == "/workspace"
    assert worker.created_at is not None


def test_worker_with_no_published_ssh_endpoint_normalizes_cleanly() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json=_pod_payload(ssh=None),
            request=request,
        )
    )
    with RunPodClient(_config(), transport=transport) as client:
        worker = client.get_worker("pod-123")

    assert worker.ssh_proxy is None
    assert worker.ssh_direct is None
    assert worker.public_ip is None
    assert worker.ssh_port is None


def test_malformed_ssh_endpoint_is_rejected_as_provider_response_error() -> None:
    malformed = _pod_payload(
        ssh={"direct": {"host": "bad host", "port": 30222, "username": "root"}}
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=malformed, request=request)
    )
    with (
        RunPodClient(_config(), transport=transport) as client,
        pytest.raises(ProviderResponseError, match="unexpected response"),
    ):
        client.get_worker("pod-123")


def test_show_worker_percent_encodes_exact_provider_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path == b"/v2/pods/pod%2Fwith%20spaces"
        return httpx.Response(200, json=_pod_payload(id="pod/with spaces"), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        worker = client.get_worker("pod/with spaces")

    assert worker.id == "pod/with spaces"


def test_gpu_discovery_normalizes_price_availability_and_datacenter_filter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/catalog/gpus"
        assert dict(request.url.params) == {
            "include": "AVAILABILITY",
            "product": "POD",
            "count": "2",
            "cloud": "COMMUNITY",
        }
        return httpx.Response(200, json={"gpus": [_gpu_payload()]}, request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        offers = client.list_gpu_offers(
            CloudType.COMMUNITY,
            2,
            data_center_ids=("US-KS-2",),
        )

    offer = offers[0]
    assert offer.gpu_type_id == "NVIDIA RTX A5000"
    assert offer.memory_gb == 24
    assert offer.availability is Availability.LOW
    assert offer.maximum_gpu_count == 2
    assert offer.price_per_gpu_hour == Decimal("0.16")
    assert offer.total_price_per_hour == Decimal("0.32")
    assert [item.id for item in offer.data_centers] == ["US-KS-2"]


def test_gpu_discovery_marks_unsupported_count_and_missing_datacenter_unavailable() -> None:
    responses = iter((_gpu_payload(), _gpu_payload()))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        too_many = client.get_gpu_offer("NVIDIA RTX A5000", CloudType.COMMUNITY, 3)
        wrong_dc = client.get_gpu_offer(
            "NVIDIA RTX A5000",
            CloudType.COMMUNITY,
            1,
            data_center_ids=("CA-MTL-1",),
        )

    assert too_many.availability is Availability.NONE
    assert wrong_dc.availability is Availability.NONE


@pytest.mark.parametrize(
    ("native_status", "expected_state"),
    [
        ("PROVISIONING", WorkerState.PROVISIONING),
        ("STARTING", WorkerState.STARTING),
        ("EXITED", WorkerState.STOPPED),
        ("ERROR", WorkerState.ERROR),
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
            json=_pod_payload(status=native_status),
            request=request,
        )
    )
    with RunPodClient(_config(), transport=transport) as client:
        worker = client.get_worker("pod-123")
    assert worker.state is expected_state
    assert worker.native_status == native_status


def test_create_constructs_explicit_gpu_image_storage_and_ssh_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/pods"
        assert json.loads(request.content) == {
            "name": "wavcse-training-abc123",
            "cloud": "COMMUNITY",
            "gpu": {"id": "NVIDIA RTX A5000", "count": 1},
            "disk": 30,
            "image": "runpod/pytorch:example",
            "dataCenterIds": ["EU-RO-1"],
            "mounts": {"persistent": {"size": 20, "path": "/workspace"}},
            "startSsh": True,
            "ports": ["22/tcp"],
        }
        return httpx.Response(201, json=_pod_payload(), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        worker = client.create_worker(_spec())
    assert worker.id == "pod-123"


def test_create_constructs_template_and_network_volume_request() -> None:
    spec = _spec(
        image=None,
        template_id="template-123",
        volume_gb=0,
        network_volume_id="network-volume-123",
        start_ssh=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["templateId"] == "template-123"
        assert "image" not in payload
        assert payload["mounts"] == {
            "network": [{"volumeId": "network-volume-123", "path": "/workspace"}]
        }
        assert "startSsh" not in payload
        return httpx.Response(201, json=_pod_payload(), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        client.create_worker(spec)


def test_create_api_validation_rejection_is_actionable_and_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            422,
            json={"detail": "GPU type is invalid"},
            request=request,
        )

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderValidationError, match="GPU type is invalid"),
    ):
        client.create_worker(_spec())
    assert calls == 1


def test_ambiguous_create_reconciles_exact_unique_name_without_post_retry() -> None:
    post_calls = 0
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls, get_calls
        if request.method == "POST":
            post_calls += 1
            raise httpx.ReadError("lost create response", request=request)
        get_calls += 1
        return httpx.Response(200, json=_pod_list(_pod_payload()), request=request)

    with RunPodClient(
        _config(),
        transport=httpx.MockTransport(handler),
        sleep=lambda delay: None,
    ) as client:
        worker = client.create_worker(_spec())

    assert worker.id == "pod-123"
    assert post_calls == 1
    assert get_calls == 1


def test_ambiguous_create_never_retries_post_when_reconciliation_is_empty() -> None:
    post_calls = 0
    get_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls, get_calls
        if request.method == "POST":
            post_calls += 1
            raise httpx.ConnectError("connection reset", request=request)
        get_calls += 1
        return httpx.Response(200, json=_pod_list(), request=request)

    with (
        RunPodClient(
            _config(reconcile_attempts=3),
            transport=httpx.MockTransport(handler),
            sleep=lambda delay: None,
        ) as client,
        pytest.raises(AmbiguousCreateError, match="was not retried"),
    ):
        client.create_worker(_spec())

    assert post_calls == 1
    assert get_calls == 3


def test_ambiguous_create_detects_duplicate_exact_identities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(
            200,
            json=_pod_list(_pod_payload(), _pod_payload(id="pod-duplicate")),
            request=request,
        )

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(AmbiguousCreateError, match="2 Pods have exact infra identity"),
    ):
        client.create_worker(_spec())


def test_start_stop_and_destroy_use_v2_exact_id_endpoints_once() -> None:
    requests: list[tuple[str, str, object | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(204, request=request)
        status = "STARTING" if body == {"action": "start"} else "EXITED"
        return httpx.Response(200, json=_pod_payload(status=status), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        assert client.start_worker("pod/exact").state is WorkerState.STARTING
        assert client.stop_worker("pod/exact").state is WorkerState.STOPPED
        client.destroy_worker("pod/exact")

    assert requests == [
        ("POST", "/v2/pods/pod/exact/action", {"action": "start"}),
        ("POST", "/v2/pods/pod/exact/action", {"action": "stop"}),
        ("DELETE", "/v2/pods/pod/exact", None),
    ]


def test_mutation_transport_failure_is_not_automatically_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("lost response", request=request)

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderOperationAmbiguousError),
    ):
        client.start_worker("pod-123")
    assert calls == 1


def test_interruptible_create_is_rejected_before_http_request() -> None:
    transport = httpx.MockTransport(
        lambda request: pytest.fail(f"unexpected request: {request.method} {request.url}")
    )
    with (
        RunPodClient(_config(), transport=transport) as client,
        pytest.raises(ProviderValidationError, match="does not expose interruptible"),
    ):
        client.create_worker(_spec(interruptible=True))


def test_safe_read_retries_transient_response_with_exponential_backoff() -> None:
    statuses = iter((503, 200))
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        payload = _pod_list(_pod_payload()) if status == 200 else {}
        return httpx.Response(status, json=payload, request=request)

    with RunPodClient(
        _config(),
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    ) as client:
        workers = client.list_workers()

    assert len(workers) == 1
    assert delays == [0.25]


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


def test_authentication_and_permission_failures_do_not_leak_secrets() -> None:
    secret = "a-credential-that-must-not-appear"
    responses = iter(
        (
            (401, {"detail": secret}),
            (403, {"detail": f"denied Authorization: Bearer {secret}"}),
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        status, payload = next(responses)
        return httpx.Response(status, json=payload, request=request)

    with RunPodClient(
        _config(token=secret),
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ProviderAuthenticationError) as authentication:
            client.list_workers()
        with pytest.raises(ProviderPermissionError) as permission:
            client.list_workers()
    assert secret not in str(authentication.value)
    assert secret not in str(permission.value)


def test_http_debug_logging_does_not_expose_authorization(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "authorization-value-that-must-not-appear"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_pod_list(), request=request)
    )
    caplog.set_level(logging.DEBUG)
    with RunPodClient(_config(token=secret), transport=transport) as client:
        client.list_workers()
    assert secret not in caplog.text


def test_show_worker_reports_not_found_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, json={"detail": "not found"}, request=request)

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderNotFoundError, match="missing-pod"),
    ):
        client.get_worker("missing-pod")
    assert calls == 1


def test_invalid_json_and_schema_are_actionable_provider_errors() -> None:
    responses = iter(
        (
            httpx.Response(200, text="not-json"),
            httpx.Response(200, json={"pods": [{}], "pagination": {}}),
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
        with pytest.raises(ProviderResponseError, match="unexpected response"):
            client.list_workers()
