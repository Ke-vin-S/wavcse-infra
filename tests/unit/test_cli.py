from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.doctor import CheckStatus, DoctorCheck, DoctorReport
from wavcse_infra.errors import ProviderNotFoundError
from wavcse_infra.models import (
    Availability,
    CloudType,
    GpuOffer,
    Worker,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.state import WorkerStateStore

runner = CliRunner()


def _worker(state: WorkerState = WorkerState.RUNNING) -> Worker:
    return Worker(
        id="pod-123",
        name="wavcse-training-abc123",
        state=state,
        native_status="RUNNING" if state is WorkerState.RUNNING else "EXITED",
        gpu_type="NVIDIA RTX A5000",
        gpu_count=1,
        cloud_type=CloudType.COMMUNITY,
        hourly_cost=Decimal("0.16") if state is WorkerState.RUNNING else Decimal("0"),
        ssh_port=10341,
    )


def _offer(price: str = "0.16") -> GpuOffer:
    return GpuOffer(
        gpu_type_id="NVIDIA RTX A5000",
        display_name="RTX A5000",
        memory_gb=24,
        cloud_type=CloudType.COMMUNITY,
        gpu_count=1,
        maximum_gpu_count=2,
        availability=Availability.HIGH,
        price_per_gpu_hour=Decimal(price),
        total_price_per_hour=Decimal(price),
    )


class FakeClient:
    def __init__(self, *, worker: Worker | None = None, offer: GpuOffer | None = None) -> None:
        self.worker = worker or _worker()
        self.offer = offer or _offer()
        self.create_calls = 0
        self.destroy_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return None

    def list_workers(self) -> list[Worker]:
        return [self.worker]

    def get_worker(self, worker_id: str) -> Worker:
        assert worker_id == "pod-123"
        return self.worker

    def list_gpu_offers(
        self,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids=(),
    ) -> list[GpuOffer]:
        assert cloud_type is CloudType.COMMUNITY
        assert gpu_count == 1
        assert data_center_ids == ()
        return [self.offer]

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids=(),
    ) -> GpuOffer:
        assert gpu_type == "NVIDIA RTX A5000"
        assert cloud_type is CloudType.COMMUNITY
        assert gpu_count == 1
        assert data_center_ids == ()
        return self.offer

    def create_worker(self, spec: WorkerSpec) -> Worker:
        self.create_calls += 1
        assert spec.name == "wavcse-training-abc123"
        return self.worker

    def start_worker(self, worker_id: str) -> Worker:
        return self.worker

    def stop_worker(self, worker_id: str) -> Worker:
        return self.worker

    def destroy_worker(self, worker_id: str) -> None:
        assert worker_id == "pod-123"
        self.destroy_calls += 1


def _install_fakes(monkeypatch, tmp_path: Path, client: FakeClient) -> None:
    monkeypatch.setattr(cli.RunPodClient, "from_settings", lambda settings: client)
    monkeypatch.setattr(cli, "_state_store", lambda: WorkerStateStore(tmp_path / "workers.json"))


def test_help_succeeds() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Operate reproducible wavCSE infrastructure" in result.stdout
    assert "worker" in result.stdout


def test_version_succeeds() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.startswith("infra ")


def test_config_validate_reports_invalid_file(tmp_path: Path) -> None:
    config_file = tmp_path / "bad.toml"
    config_file.write_text("not valid TOML", encoding="utf-8")
    result = runner.invoke(app, ["--config", str(config_file), "config", "validate"])
    assert result.exit_code == 2
    assert "Configuration error" in result.stderr


def test_doctor_uses_nonzero_exit_for_failed_required_check(monkeypatch) -> None:
    report = DoctorReport(
        checks=(
            DoctorCheck("Config", CheckStatus.PASS, "/home/ubuntu/.config/config.toml"),
            DoctorCheck("AWS identity", CheckStatus.FAIL, "instance profile missing"),
        )
    )
    monkeypatch.setattr(cli, "run_doctor", lambda settings, *, config_path: report)
    result = runner.invoke(app, ["doctor"], env={})
    assert result.exit_code == 1
    assert "PASS Config: /home/ubuntu/.config/config.toml" in result.stdout
    assert "FAIL AWS identity: instance profile missing" in result.stdout


def test_worker_list_requires_resolvable_credential() -> None:
    result = runner.invoke(app, ["worker", "list"], env={"RUNPOD_API_KEY": ""})
    assert result.exit_code == 2
    assert "RunPod credential unavailable" in result.stderr


def test_worker_list_renders_normalized_workers(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    result = runner.invoke(app, ["worker", "list"], env={"RUNPOD_API_KEY": "fake-token"})
    assert result.exit_code == 0
    assert (
        "pod-123\tRUNNING\tNVIDIA RTX A5000\t1\tCOMMUNITY\t$0.1600\twavcse-training-abc123"
    ) in result.stdout


def test_worker_show_supports_normalized_json(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    result = runner.invoke(
        app,
        ["worker", "show", "pod-123", "--json"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0
    assert '"id": "pod-123"' in result.stdout
    assert '"state": "RUNNING"' in result.stdout
    assert "fake-token" not in result.stdout


def test_worker_show_marks_tracked_worker_absent_on_provider_404(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class AbsentClient(FakeClient):
        def get_worker(self, worker_id: str) -> Worker:
            raise ProviderNotFoundError(f"RunPod worker {worker_id} was not found")

    marked_absent: list[str] = []
    _install_fakes(monkeypatch, tmp_path, AbsentClient())
    monkeypatch.setattr(cli, "_mark_state_destroyed", marked_absent.append)

    result = runner.invoke(
        app,
        ["worker", "show", "pod-123"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 1
    assert "was not found" in result.stderr
    assert marked_absent == ["pod-123"]


def test_gpu_types_renders_current_offer(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    result = runner.invoke(
        app,
        ["worker", "gpu-types", "--cloud", "COMMUNITY"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0
    assert "NVIDIA RTX A5000\t24 GB\tHIGH\t2\t$0.1600\t$0.1600" in result.stdout


def test_create_yes_prints_plan_and_bypasses_confirmation(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install_fakes(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cli, "_infra_worker_name", lambda prefix: "wavcse-training-abc123")
    result = runner.invoke(
        app,
        [
            "worker",
            "create",
            "--gpu",
            "NVIDIA RTX A5000",
            "--cloud",
            "COMMUNITY",
            "--image",
            "runpod/pytorch:example",
            "--max-price",
            "0.20",
            "--yes",
        ],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0, result.output
    assert "RunPod creation plan" in result.stdout
    assert "Provider list price/hour: $0.1600" in result.stdout
    assert "RunPod worker created and reached RUNNING" in result.stdout
    assert client.create_calls == 1


def test_create_confirmation_rejection_makes_no_mutation(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install_fakes(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cli, "_infra_worker_name", lambda prefix: "wavcse-training-abc123")
    result = runner.invoke(
        app,
        [
            "worker",
            "create",
            "--gpu",
            "NVIDIA RTX A5000",
            "--cloud",
            "COMMUNITY",
            "--image",
            "runpod/pytorch:example",
        ],
        input="n\n",
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0
    assert "no Pod was created" in result.stdout
    assert client.create_calls == 0


def test_yes_does_not_bypass_max_price_guard(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient(offer=_offer("0.25"))
    _install_fakes(monkeypatch, tmp_path, client)
    monkeypatch.setattr(cli, "_infra_worker_name", lambda prefix: "wavcse-training-abc123")
    result = runner.invoke(
        app,
        [
            "worker",
            "create",
            "--gpu",
            "NVIDIA RTX A5000",
            "--cloud",
            "COMMUNITY",
            "--image",
            "runpod/pytorch:example",
            "--max-price",
            "0.20",
            "--yes",
        ],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 1
    assert "exceeds --max-price" in result.stderr
    assert client.create_calls == 0


def test_destroy_confirmation_rejection_makes_no_mutation(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install_fakes(monkeypatch, tmp_path, client)
    result = runner.invoke(
        app,
        ["worker", "destroy", "pod-123"],
        input="n\n",
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0
    assert "RunPod destroy target" in result.stdout
    assert "Destroy cancelled" in result.stdout
    assert client.destroy_calls == 0


def test_destroy_yes_uses_exact_id_and_waits_for_absence(monkeypatch, tmp_path: Path) -> None:
    class DestroyingClient(FakeClient):
        def get_worker(self, worker_id: str) -> Worker:
            assert worker_id == "pod-123"
            if self.destroy_calls:
                raise ProviderNotFoundError("absent")
            return self.worker

    client = DestroyingClient()
    _install_fakes(monkeypatch, tmp_path, client)
    result = runner.invoke(
        app,
        ["worker", "destroy", "pod-123", "--yes"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0, result.output
    assert "pod-123 was destroyed and is now absent" in result.stdout
    assert client.destroy_calls == 1


def test_destroy_already_absent_is_successful_and_never_mutates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class AbsentClient(FakeClient):
        def get_worker(self, worker_id: str) -> Worker:
            raise ProviderNotFoundError(f"RunPod worker {worker_id} was not found")

    client = AbsentClient()
    _install_fakes(monkeypatch, tmp_path, client)
    result = runner.invoke(
        app,
        ["worker", "destroy", "pod-123", "--yes"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0
    assert "already absent" in result.stdout
    assert client.destroy_calls == 0


def test_generated_worker_names_are_recognizable_and_unique() -> None:
    first = cli._infra_worker_name("DG 0004")
    second = cli._infra_worker_name("DG 0004")

    assert first.startswith("wavcse-dg-0004-")
    assert second.startswith("wavcse-dg-0004-")
    assert first != second
