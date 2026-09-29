"""CLI coverage for network volume operations and volume-constrained Pod creation."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.errors import ProviderNotFoundError, ResourceUnavailableError
from wavcse_infra.models import (
    Availability,
    CloudType,
    DataCenterInfo,
    GpuOffer,
    NetworkVolume,
    NetworkVolumeBilling,
    NetworkVolumeSpec,
    VolumeType,
    Worker,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.state import VolumeStateStore, WorkerStateStore

runner = CliRunner()

VOLUME_ID = "vol-abc123"
VOLUME_NAME = "wavcse-vol-cache-abc123"
POD_ID = "pod-123"


def _volume(**overrides: object) -> NetworkVolume:
    values: dict[str, object] = {
        "id": VOLUME_ID,
        "name": VOLUME_NAME,
        "size_gb": 200,
        "datacenter": "EU-RO-1",
        "volume_type": VolumeType.STANDARD,
    }
    values.update(overrides)
    return NetworkVolume.model_validate(values)


def _data_center(**overrides: object) -> DataCenterInfo:
    values: dict[str, object] = {
        "id": "EU-RO-1",
        "name": "Romania 1",
        "region": "EUROPE",
        "network_volume_types": (VolumeType.STANDARD,),
    }
    values.update(overrides)
    return DataCenterInfo.model_validate(values)


def _worker(**overrides: object) -> Worker:
    values: dict[str, object] = {
        "id": POD_ID,
        "name": "wavcse-cache-abc123",
        "state": WorkerState.RUNNING,
        "cloud_type": CloudType.SECURE,
        "datacenter": "EU-RO-1",
        "network_volume_id": VOLUME_ID,
        "volume_mount_path": "/workspace/cache",
    }
    values.update(overrides)
    return Worker.model_validate(values)


def _offer(**overrides: object) -> GpuOffer:
    values: dict[str, object] = {
        "gpu_type_id": "NVIDIA L4",
        "display_name": "L4",
        "memory_gb": 24,
        "cloud_type": CloudType.SECURE,
        "gpu_count": 1,
        "availability": Availability.HIGH,
        "price_per_gpu_hour": Decimal("0.49"),
        "total_price_per_hour": Decimal("0.49"),
        "public_ip_capable": True,
    }
    values.update(overrides)
    return GpuOffer.model_validate(values)


class FakeClient:
    """Provider double covering the volume surface and the parts of the Pod surface used."""

    def __init__(
        self,
        *,
        volumes: Sequence[NetworkVolume] = (),
        created_volume: NetworkVolume | None = None,
        worker: Worker | None = None,
        offer: GpuOffer | None = None,
        offer_error: Exception | None = None,
    ) -> None:
        self.volumes = list(volumes)
        self.created_volume = created_volume or _volume()
        self.destroyed: set[str] = set()
        self.worker = worker or _worker()
        self.offer = offer or _offer()
        self.offer_error = offer_error
        self.volume_create_calls = 0
        self.volume_create_specs: list[NetworkVolumeSpec] = []
        self.volume_destroy_calls: list[str] = []
        self.worker_create_calls = 0
        self.worker_create_specs: list[WorkerSpec] = []
        self.worker_destroy_calls: list[str] = []

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    # volumes
    def list_network_volumes(self) -> list[NetworkVolume]:
        return list(self.volumes)

    def get_network_volume(self, volume_id: str) -> NetworkVolume:
        for volume in self.volumes:
            if volume.id == volume_id and volume_id not in self.destroyed:
                return volume
        raise ProviderNotFoundError(f"RunPod network volume {volume_id} was not found (HTTP 404)")

    def create_network_volume(self, spec: NetworkVolumeSpec) -> NetworkVolume:
        self.volume_create_calls += 1
        self.volume_create_specs.append(spec)
        return self.created_volume

    def destroy_network_volume(self, volume_id: str) -> None:
        self.volume_destroy_calls.append(volume_id)
        self.destroyed.add(volume_id)

    def list_data_centers(self, *, include_gpu_availability: bool = False) -> list[DataCenterInfo]:
        del include_gpu_availability
        return [_data_center()]

    def list_network_volume_billing(
        self,
        *,
        volume_id: str | None = None,
        last_n: int | None = None,
    ) -> NetworkVolumeBilling:
        del volume_id, last_n
        return NetworkVolumeBilling(total_amount_usd=Decimal("0.39"))

    # pods
    def get_worker(self, worker_id: str) -> Worker:
        assert worker_id == POD_ID
        return self.worker

    def list_workers(self) -> list[Worker]:
        return [self.worker]

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids: Sequence[str] = (),
        require_public_ip: bool = False,
    ) -> GpuOffer:
        del gpu_type, cloud_type, gpu_count, require_public_ip
        if self.offer_error is not None:
            raise self.offer_error
        if data_center_ids and self.offer.availability is Availability.NONE:
            return self.offer
        return self.offer

    def create_worker(self, spec: WorkerSpec) -> Worker:
        self.worker_create_calls += 1
        self.worker_create_specs.append(spec)
        return self.worker.model_copy(update={"name": spec.name})

    def destroy_worker(self, worker_id: str) -> None:
        self.worker_destroy_calls.append(worker_id)


def _install(monkeypatch, tmp_path: Path, client: FakeClient) -> None:
    monkeypatch.setattr(cli.RunPodClient, "from_settings", lambda settings: client)
    monkeypatch.setattr(cli, "_state_store", lambda: WorkerStateStore(tmp_path / "workers.json"))
    monkeypatch.setattr(
        cli,
        "_volume_state_store",
        lambda: VolumeStateStore(tmp_path / "volumes.json"),
    )


def _env() -> dict[str, str]:
    return {"RUNPOD_API_KEY": "fake-token"}


def _volume_state(tmp_path: Path) -> VolumeStateStore:
    return VolumeStateStore(tmp_path / "volumes.json")


# --- listing and inspection ------------------------------------------------------------


def test_volume_list_renders_the_provider_view(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, FakeClient(volumes=[_volume()]))

    result = runner.invoke(app, ["volume", "list"], env=_env())

    assert result.exit_code == 0, result.output
    assert f"{VOLUME_ID}\tEU-RO-1\t200 GB\tSTANDARD\t{VOLUME_NAME}" in result.stdout


def test_volume_list_reconciles_a_tracked_volume(monkeypatch, tmp_path: Path) -> None:
    store = _volume_state(tmp_path)
    intent = store.record_create_intent(
        NetworkVolumeSpec(name=VOLUME_NAME, size_gb=200, datacenter="EU-RO-1")
    )
    assert intent.provider_volume_id is None
    _install(monkeypatch, tmp_path, FakeClient(volumes=[_volume()]))

    result = runner.invoke(app, ["volume", "list"], env=_env())

    assert result.exit_code == 0, result.output
    adopted = store.get_by_identity(VOLUME_NAME)
    assert adopted is not None
    assert adopted.provider_volume_id == VOLUME_ID


def test_volume_list_warns_about_an_unresolved_paid_create(monkeypatch, tmp_path: Path) -> None:
    _volume_state(tmp_path).record_create_intent(
        NetworkVolumeSpec(name=VOLUME_NAME, size_gb=200, datacenter="EU-RO-1")
    )
    _install(monkeypatch, tmp_path, FakeClient(volumes=[]))

    result = runner.invoke(app, ["volume", "list"], env=_env())

    assert result.exit_code == 0, result.output
    assert "unresolved create intent" in result.stderr
    assert "may have created it" in result.stderr


def test_volume_show_prints_provider_and_billed_facts(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, FakeClient(volumes=[_volume()]))

    result = runner.invoke(app, ["volume", "show", VOLUME_ID], env=_env())

    assert result.exit_code == 0, result.output
    assert f"ID: {VOLUME_ID}" in result.stdout
    assert "Data center: EU-RO-1" in result.stdout
    assert "Size: 200 GB" in result.stdout
    assert "Provider-billed storage (last 24 buckets): $0.39" in result.stdout
    assert "not canonical storage" in result.stdout


def test_volume_show_reports_an_absent_volume(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, FakeClient(volumes=[]))

    result = runner.invoke(app, ["volume", "show", "vol-gone"], env=_env())

    assert result.exit_code == 1
    assert "vol-gone was not found" in result.stderr


def test_volume_datacenters_lists_only_capable_locations(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, FakeClient())

    result = runner.invoke(app, ["volume", "datacenters"], env=_env())

    assert result.exit_code == 0, result.output
    assert "EU-RO-1\tEUROPE\tSTANDARD\tRomania 1" in result.stdout


# --- creation -------------------------------------------------------------------------


def test_volume_create_refuses_without_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(
        app,
        ["volume", "create", "--data-center", "EU-RO-1", "--size", "200"],
        input="n\n",
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "creation plan" in result.stdout.lower()
    assert "Exact create request" in result.stdout
    assert "no volume was created" in result.stdout
    assert client.volume_create_calls == 0
    assert _volume_state(tmp_path).list_records() == []


def test_volume_create_shows_the_data_center_and_the_published_rate(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient()
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(
        app,
        ["volume", "create", "--data-center", "EU-RO-1", "--size", "200"],
        input="n\n",
        env=_env(),
    )

    assert "Data center: EU-RO-1" in result.stdout
    assert "Size: 200 GB" in result.stdout
    assert "$0.07/GB/month" in result.stdout
    assert "published rate, not a provider-reported charge" in result.stdout
    assert "Estimated storage cost: $14.00/month" in result.stdout
    assert "storage charges continue until the volume is destroyed" in result.stdout
    assert '"dataCenter": "EU-RO-1"' in result.stdout


def test_volume_create_with_yes_records_and_prints_the_result(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(
        app,
        ["volume", "create", "--data-center", "EU-RO-1", "--size", "200", "--yes"],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert client.volume_create_calls == 1
    assert client.volume_create_specs[0].size_gb == 200
    assert client.volume_create_specs[0].datacenter == "EU-RO-1"
    assert client.volume_create_specs[0].name.startswith("wavcse-vol-cache-")
    assert "RunPod network volume created." in result.stdout
    records = _volume_state(tmp_path).list_records()
    assert [record.provider_volume_id for record in records] == [VOLUME_ID]


def test_volume_create_rejects_an_incapable_data_center_before_any_call(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient()
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(
        app,
        ["volume", "create", "--data-center", "US-NOWHERE-9", "--size", "200", "--yes"],
        env=_env(),
    )

    assert result.exit_code != 0
    assert "does not report a data center" in result.stderr
    assert client.volume_create_calls == 0


def test_volume_create_rejects_a_tier_the_data_center_lacks(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(
        app,
        [
            "volume",
            "create",
            "--data-center",
            "EU-RO-1",
            "--size",
            "200",
            "--tier",
            "HIGH_PERFORMANCE",
            "--yes",
        ],
        env=_env(),
    )

    assert result.exit_code != 0
    assert "cannot host network volume tier" in result.stderr
    assert client.volume_create_calls == 0


# --- destruction ----------------------------------------------------------------------


def test_volume_destroy_refuses_without_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ["volume", "destroy", VOLUME_ID], input="n\n", env=_env())

    assert result.exit_code == 0, result.output
    assert "destroy target" in result.stdout
    assert "permanently lost" in result.stdout
    assert "not changed" in result.stdout
    assert client.volume_destroy_calls == []


def test_volume_destroy_never_resolves_a_name(monkeypatch, tmp_path: Path) -> None:
    """A name is not an identity, so nothing is destroyed and the operator is told why."""

    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ["volume", "destroy", VOLUME_NAME, "--yes"], env=_env())

    assert result.exit_code == 0, result.output
    assert "already absent" in result.stdout
    assert "exact provider ID" in result.stdout
    assert client.volume_destroy_calls == []
    assert client.volumes == [_volume()]


def test_volume_destroy_warns_about_mounting_pods_and_leaves_them_alone(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)
    workers = WorkerStateStore(tmp_path / "workers.json")
    workers.record_created(
        cli.WorkerCreationPlan(spec=_worker_spec_for_state(), offer=_offer()),
        _worker(),
    )

    result = runner.invoke(app, ["volume", "destroy", VOLUME_ID, "--yes"], env=_env())

    assert result.exit_code == 0, result.output
    assert f"that mount this volume (they are NOT destroyed): {POD_ID}" in result.stdout
    assert client.volume_destroy_calls == [VOLUME_ID]
    assert client.worker_destroy_calls == []
    assert workers.get(POD_ID) is not None, "Pod state must be untouched by a volume destroy"


def test_volume_destroy_reports_an_already_absent_volume(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(volumes=[])
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ["volume", "destroy", "vol-gone", "--yes"], env=_env())

    assert result.exit_code == 0, result.output
    assert "already absent" in result.stdout
    assert client.volume_destroy_calls == []


def test_volume_destroy_states_that_pods_and_s3_are_untouched(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, ["volume", "destroy", VOLUME_ID, "--yes"], env=_env())

    assert "No Pod was stopped or destroyed, and no S3 object was deleted" in result.stdout


# --- volume-constrained Pod creation ---------------------------------------------------


def _worker_spec_for_state() -> WorkerSpec:
    return WorkerSpec(
        name="wavcse-training-pod-state",
        gpu_type="NVIDIA L4",
        gpu_count=1,
        cloud_type=CloudType.SECURE,
        image="runpod/pytorch:example",
        network_volume_id=VOLUME_ID,
        volume_mount_path="/workspace/cache",
        start_ssh=True,
    )


def _create_args() -> list[str]:
    return [
        "worker",
        "create",
        "--gpu",
        "NVIDIA L4",
        "--cloud",
        "secure",
        "--image",
        "runpod/pytorch:example",
        "--network-volume-id",
        VOLUME_ID,
        "--start-ssh",
        "--yes",
    ]


def test_worker_create_constrains_placement_and_defaults_the_mount(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cli, "_infra_worker_name", lambda prefix: "wavcse-cache-abc123")

    result = runner.invoke(app, _create_args(), env=_env())

    assert result.exit_code == 0, result.output
    spec = client.worker_create_specs[0]
    assert spec.data_center_ids == ("EU-RO-1",)
    assert spec.network_volume_id == VOLUME_ID
    assert spec.volume_mount_path == "/workspace/cache"
    assert "Network volume data center: EU-RO-1 (placement constrained)" in result.stdout


def test_worker_create_rejects_community_cloud_with_a_network_volume(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)
    args = [arg if arg != "secure" else "community" for arg in _create_args()]

    result = runner.invoke(app, args, env=_env())

    assert result.exit_code != 0
    assert "Secure Cloud Pods" in result.stderr
    assert client.worker_create_calls == 0


def test_worker_create_rejects_a_mismatched_data_center(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, [*_create_args(), "--data-center", "US-KS-2"], env=_env())

    assert result.exit_code != 0
    assert "can only be placed in" in result.stderr
    assert client.worker_create_calls == 0


def test_worker_create_refuses_an_unavailable_gpu_in_the_volume_data_center(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient(
        volumes=[_volume()],
        offer=_offer(availability=Availability.NONE),
    )
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, _create_args(), env=_env())

    assert result.exit_code != 0
    assert "network volume" in result.stderr
    assert "No Pod was created" in result.stderr
    assert client.worker_create_calls == 0, "a paid request must never be issued"


def test_worker_create_reports_an_absent_network_volume(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(volumes=[])
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(app, _create_args(), env=_env())

    assert result.exit_code == 1
    assert "vol-abc123 was not found" in result.stderr
    assert client.worker_create_calls == 0


def test_worker_create_does_not_delete_a_volume(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(volumes=[_volume()])
    _install(monkeypatch, tmp_path, client)

    runner.invoke(app, _create_args(), env=_env())

    assert client.volume_destroy_calls == []


# --- cache inspection ------------------------------------------------------------------


class FakeCacheClient:
    """Report one parsed cache-statistics payload through the real cache collaborator."""

    def __init__(self, payload: dict[str, str]) -> None:
        self.payload = payload

    def stats(self, worker_id: str, **kwargs: object) -> object:
        del worker_id, kwargs
        from wavcse_infra.storage.cache import CacheStats

        return CacheStats.model_validate(self.payload)


def _install_cache(
    monkeypatch,
    tmp_path: Path,
    client: FakeClient,
    returned: dict[str, str],
) -> None:
    _install(monkeypatch, tmp_path, client)
    workers = WorkerStateStore(tmp_path / "workers.json")
    workers.record_created(
        cli.WorkerCreationPlan(spec=_worker_spec_for_state(), offer=_offer()),
        _worker(),
    )
    monkeypatch.setattr(cli, "_ssh_access", lambda client, settings: (object(), object()))
    monkeypatch.setattr(
        cli,
        "WorkerArtifactCache",
        lambda *args, **kwargs: FakeCacheClient(returned),
    )


def test_volume_cache_stats_renders_worker_facts(monkeypatch, tmp_path: Path) -> None:
    _install_cache(
        monkeypatch,
        tmp_path,
        FakeClient(worker=_worker()),
        {
            "root": "/workspace/cache",
            "entries": 4,
            "cached_bytes": 17179869184,
            "staging_bytes": 0,
            "unverified_entries": 0,
            "marker_schema_version": "1",
        },
    )

    result = runner.invoke(app, ["volume", "cache", "stats", "--worker", POD_ID], env=_env())

    assert result.exit_code == 0, result.output
    assert "Cache root: /workspace/cache" in result.stdout
    assert "Verified entries: 4" in result.stdout
    assert "16.00 GiB" in result.stdout
    assert "Marker schema version: 1" in result.stdout
    assert "operator-managed" in result.stdout


def test_volume_cache_stats_requires_a_recorded_volume_mount(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, FakeClient(worker=_worker()))

    result = runner.invoke(app, ["volume", "cache", "stats", "--worker", POD_ID], env=_env())

    assert result.exit_code == 2
    assert "no network volume mount recorded" in result.stderr


def test_volume_cache_stats_requires_a_running_worker(monkeypatch, tmp_path: Path) -> None:
    _install_cache(
        monkeypatch,
        tmp_path,
        FakeClient(worker=_worker(state=WorkerState.STOPPED)),
        {"root": "/workspace/cache"},
    )

    result = runner.invoke(app, ["volume", "cache", "stats", "--worker", POD_ID], env=_env())

    assert result.exit_code == 2
    assert "requires provider state RUNNING" in result.stderr


def test_volume_cache_stats_json_is_machine_readable(monkeypatch, tmp_path: Path) -> None:
    _install_cache(
        monkeypatch,
        tmp_path,
        FakeClient(worker=_worker()),
        {
            "root": "/workspace/cache",
            "entries": 1,
            "cached_bytes": 10,
            "staging_bytes": 2,
            "unverified_entries": 0,
            "marker_schema_version": None,
        },
    )

    result = runner.invoke(
        app, ["volume", "cache", "stats", "--worker", POD_ID, "--json"], env=_env()
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["entries"] == 1
    assert payload["marker_schema_version"] is None


def test_worker_create_offer_failure_without_a_volume_stays_a_plain_error(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient(offer_error=ResourceUnavailableError("no confirmed availability"))
    _install(monkeypatch, tmp_path, client)

    result = runner.invoke(
        app,
        [
            "worker",
            "create",
            "--gpu",
            "NVIDIA L4",
            "--cloud",
            "secure",
            "--image",
            "runpod/pytorch:example",
            "--yes",
        ],
        env=_env(),
    )

    assert result.exit_code == 1
    assert "This Pod must mount network volume" not in result.stderr
    assert client.worker_create_calls == 0
