"""Offline coverage for RunPod network volume, data-center, and billing wire handling."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from wavcse_infra.config import RunPodConfig
from wavcse_infra.errors import (
    AmbiguousCreateError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ProviderResponseError,
    ProviderValidationError,
)
from wavcse_infra.models import NetworkVolumeSpec, VolumeType
from wavcse_infra.providers.runpod import (
    RunPodClient,
    network_volume_create_payload,
)


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


def _volume_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "vol-abc123",
        "name": "wavcse-vol-cache-abc123",
        "size": 200,
        "dataCenter": "EU-RO-1",
        "type": "STANDARD",
        "providerFieldAddedLater": "ignored",
    }
    payload.update(overrides)
    return payload


def _spec(**overrides: object) -> NetworkVolumeSpec:
    values: dict[str, object] = {
        "name": "wavcse-vol-cache-abc123",
        "size_gb": 200,
        "datacenter": "EU-RO-1",
        "volume_type": VolumeType.STANDARD,
    }
    values.update(overrides)
    return NetworkVolumeSpec.model_validate(values)


def _data_center_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "EU-RO-1",
        "name": "Romania 1",
        "region": "EUROPE",
        "networkVolumeTypes": ["STANDARD"],
        "compliance": ["GDPR"],
        "globalNetwork": False,
        "gpuAvailability": [
            {"id": "NVIDIA L4", "name": "L4", "availability": "LOW"},
        ],
    }
    payload.update(overrides)
    return payload


def test_list_network_volumes_normalizes_the_v2_document() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/network-volumes"
        return httpx.Response(
            200,
            json={"networkVolumes": [_volume_payload()]},
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        volumes = client.list_network_volumes()

    assert len(volumes) == 1
    volume = volumes[0]
    assert volume.id == "vol-abc123"
    assert volume.size_gb == 200
    assert volume.datacenter == "EU-RO-1"
    assert volume.volume_type is VolumeType.STANDARD


def test_list_network_volumes_tolerates_absent_optional_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"networkVolumes": [_volume_payload(name=None, type=None)]},
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        volume = client.list_network_volumes()[0]

    assert volume.name is None
    assert volume.volume_type is None


def test_unknown_native_type_does_not_invent_a_tier() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"networkVolumes": [_volume_payload(type="SOMETHING_NEW")]},
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        assert client.list_network_volumes()[0].volume_type is None


def test_list_network_volumes_rejects_a_response_without_the_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []}, request=request)

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderResponseError, match="list network volumes"),
    ):
        client.list_network_volumes()


def test_get_network_volume_uses_the_exact_encoded_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path == b"/v2/network-volumes/vol%2Fwith%20spaces"
        return httpx.Response(200, json=_volume_payload(id="vol/with spaces"), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        volume = client.get_network_volume("vol/with spaces")

    assert volume.id == "vol/with spaces"


def test_get_network_volume_missing_is_reported_as_absent() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={}, request=request))
    with (
        RunPodClient(_config(), transport=transport) as client,
        pytest.raises(ProviderNotFoundError, match="network volume vol-gone was not found"),
    ):
        client.get_network_volume("vol-gone")


def test_create_posts_the_documented_request_and_parses_201() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v2/network-volumes"
        assert json.loads(request.content) == {
            "name": "wavcse-vol-cache-abc123",
            "size": 200,
            "dataCenter": "EU-RO-1",
            "type": "STANDARD",
        }
        return httpx.Response(201, json=_volume_payload(), request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        volume = client.create_network_volume(_spec())

    assert volume.id == "vol-abc123"


def test_create_omits_the_tier_when_the_data_center_should_choose() -> None:
    assert network_volume_create_payload(_spec(volume_type=None)) == {
        "name": "wavcse-vol-cache-abc123",
        "size": 200,
        "dataCenter": "EU-RO-1",
    }


def test_create_is_issued_once_and_reconciled_by_exact_identity() -> None:
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "POST":
            post_calls += 1
            raise httpx.ReadError("lost the create response", request=request)
        assert request.url.path == "/v2/network-volumes"
        return httpx.Response(
            200,
            json={"networkVolumes": [_volume_payload()]},
            request=request,
        )

    with RunPodClient(
        _config(),
        transport=httpx.MockTransport(handler),
        sleep=lambda delay: None,
    ) as client:
        volume = client.create_network_volume(_spec())

    assert volume.id == "vol-abc123"
    assert post_calls == 1, "the paid POST must never be retried"


def test_create_reconciles_a_duplicate_identity_as_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, json={}, request=request)
        return httpx.Response(
            200,
            json={
                "networkVolumes": [
                    _volume_payload(id="vol-one"),
                    _volume_payload(id="vol-two"),
                ]
            },
            request=request,
        )

    with (
        RunPodClient(
            _config(),
            transport=httpx.MockTransport(handler),
            sleep=lambda delay: None,
        ) as client,
        pytest.raises(AmbiguousCreateError, match="2 volumes have exact infra identity"),
    ):
        client.create_network_volume(_spec())


def test_create_with_no_reconcilable_match_fails_without_retrying() -> None:
    post_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_calls
        if request.method == "POST":
            post_calls += 1
            return httpx.Response(503, json={}, request=request)
        return httpx.Response(200, json={"networkVolumes": []}, request=request)

    with (
        RunPodClient(
            _config(reconcile_attempts=2),
            transport=httpx.MockTransport(handler),
            sleep=lambda delay: None,
        ) as client,
        pytest.raises(AmbiguousCreateError, match="infra volume list"),
    ):
        client.create_network_volume(_spec())

    assert post_calls == 1


def test_create_api_rejection_is_actionable_and_not_reconciled() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            422,
            json={"detail": "cannot create network volume in this data center"},
            request=request,
        )

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderValidationError, match="cannot create network volume"),
    ):
        client.create_network_volume(_spec())

    assert calls == ["POST"]


def test_destroy_uses_one_exact_id_delete() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(204, request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        client.destroy_network_volume("vol-abc123")

    assert requests == [("DELETE", "/v2/network-volumes/vol-abc123")]


def test_destroy_missing_volume_is_reported_as_absent() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(404, json={}, request=request))
    with (
        RunPodClient(_config(), transport=transport) as client,
        pytest.raises(ProviderNotFoundError, match="network volume vol-gone"),
    ):
        client.destroy_network_volume("vol-gone")


def test_destroy_with_a_lost_response_is_reconcilable_and_never_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={}, request=request)

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderOperationAmbiguousError, match="must be reconciled"),
    ):
        client.destroy_network_volume("vol-abc123")

    assert calls == 1


def test_list_data_centers_reads_the_storage_tiers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/catalog/datacenters"
        assert "include" not in request.url.params
        return httpx.Response(
            200,
            json={
                "dataCenters": [
                    _data_center_payload(),
                    _data_center_payload(
                        id="CA-MTL-4",
                        region="NORTH_AMERICA",
                        networkVolumeTypes=["HIGH_PERFORMANCE", "STANDARD"],
                    ),
                    _data_center_payload(id="US-TX-3", networkVolumeTypes=[]),
                ]
            },
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        data_centers = client.list_data_centers()

    by_id = {entry.id: entry for entry in data_centers}
    assert by_id["EU-RO-1"].supports_network_volume(VolumeType.STANDARD)
    assert not by_id["EU-RO-1"].supports_network_volume(VolumeType.HIGH_PERFORMANCE)
    assert by_id["CA-MTL-4"].supports_network_volume(VolumeType.HIGH_PERFORMANCE)
    assert not by_id["US-TX-3"].supports_network_volume()


def test_list_data_centers_can_request_gpu_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["include"] == "GPU_AVAILABILITY"
        return httpx.Response(200, json={"dataCenters": [_data_center_payload()]}, request=request)

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        data_centers = client.list_data_centers(include_gpu_availability=True)

    assert data_centers[0].gpu_availability[0].id == "NVIDIA L4"
    assert data_centers[0].gpu_availability[0].availability.value == "LOW"


def test_network_volume_billing_normalizes_totals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/billing/network-volumes"
        assert request.url.params["networkVolumeId"] == "vol-abc123"
        assert request.url.params["lastN"] == "24"
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "startTime": "2026-09-01T00:00:00Z",
                        "endTime": "2026-09-02T00:00:00Z",
                        "networkVolumeId": "vol-abc123",
                        "totalAmount": 0.39,
                        "standardAmount": 0.39,
                        "highPerformanceAmount": 0,
                    }
                ],
                "metadata": {
                    "query": {"networkVolumeId": "vol-abc123"},
                    "recordCount": 1,
                    "uniqueNetworkVolumeCount": 1,
                    "totals": {
                        "totalAmount": 0.39,
                        "standardAmount": 0.39,
                        "highPerformanceAmount": 0,
                    },
                },
            },
            request=request,
        )

    with RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client:
        billing = client.list_network_volume_billing(volume_id="vol-abc123", last_n=24)

    assert billing.total_amount_usd == Decimal("0.39")
    assert billing.unique_volume_count == 1
    assert billing.records[0].volume_id == "vol-abc123"
    assert billing.records[0].standard_amount_usd == Decimal("0.39")


def test_volume_reads_retry_transient_failures_boundedly() -> None:
    statuses = iter((503, 200))
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        payload = {"networkVolumes": [_volume_payload()]} if status == 200 else {}
        return httpx.Response(status, json=payload, request=request)

    with RunPodClient(
        _config(),
        transport=httpx.MockTransport(handler),
        sleep=delays.append,
    ) as client:
        volumes = client.list_network_volumes()

    assert len(volumes) == 1
    assert delays == [0.25]


def test_volume_requests_carry_the_resolved_credential() -> None:
    secret = "volume-token-that-must-not-be-logged"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        return httpx.Response(200, json={"networkVolumes": []}, request=request)

    with RunPodClient(_config(token=secret), transport=httpx.MockTransport(handler)) as client:
        assert client.list_network_volumes() == []


def test_create_failure_detail_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": "rejected https://example.test/?X-Amz-Signature=abcdef"},
            request=request,
        )

    with (
        RunPodClient(_config(), transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProviderValidationError) as failure,
    ):
        client.create_network_volume(_spec())

    assert "X-Amz-Signature=abcdef" not in str(failure.value)
