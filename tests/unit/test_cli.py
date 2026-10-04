from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.config import SshConfig
from wavcse_infra.doctor import CheckStatus, DoctorCheck, DoctorReport
from wavcse_infra.errors import CredentialError, ProviderNotFoundError
from wavcse_infra.models import (
    Availability,
    CloudType,
    GpuOffer,
    HealthCheckStatus,
    Worker,
    WorkerConnectionInfo,
    WorkerGpuInfo,
    WorkerHealthCheck,
    WorkerHealthReport,
    WorkerReadinessState,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.providers import runpod as runpod_provider
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.bootstrap import WorkerBootstrapper
from wavcse_infra.workers.ssh import SshCommandResult, SshWaitResult

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
        self.created_specs: list[WorkerSpec] = []
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
        require_public_ip: bool = False,
    ) -> list[GpuOffer]:
        assert cloud_type is CloudType.COMMUNITY
        assert gpu_count == 1
        assert data_center_ids == ()
        if require_public_ip:
            return [self.offer.model_copy(update={"public_ip_capable": True})]
        return [self.offer]

    def get_gpu_offer(
        self,
        gpu_type: str,
        cloud_type: CloudType,
        gpu_count: int,
        *,
        data_center_ids=(),
        require_public_ip: bool = False,
    ) -> GpuOffer:
        assert gpu_type == "NVIDIA RTX A5000"
        assert cloud_type is CloudType.COMMUNITY
        assert gpu_count == 1
        assert data_center_ids == ()
        if require_public_ip:
            assert self.offer.public_ip_capable is True
        return self.offer

    def create_worker(self, spec: WorkerSpec) -> Worker:
        self.create_calls += 1
        self.created_specs.append(spec)
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


def test_colab_stop_rejects_without_calling_runpod(monkeypatch, tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    store.record_colab_intent("wavcse-123456789abc", "T4")
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    monkeypatch.setattr(
        cli.RunPodClient,
        "from_settings",
        lambda settings: (_ for _ in ()).throw(AssertionError("wrong provider")),
    )
    result = runner.invoke(app, ["worker", "stop", "wavcse-123456789abc"], env={})
    assert result.exit_code == 2
    assert "Colab worker" in result.stderr
    assert "destroy is terminal" in result.stderr


def test_colab_listing_routes_without_runpod_and_reconciles_only_colab(
    monkeypatch, tmp_path: Path
) -> None:
    from wavcse_infra.models import ExecutionTransport, ProviderKind

    store = WorkerStateStore(tmp_path / "workers.json")
    name = "wavcse-123456789abc"
    store.record_colab_intent(name, "T4")
    colab_worker = Worker(
        provider=ProviderKind.COLAB,
        execution_transport=ExecutionTransport.COLAB_EXEC,
        id=name,
        name=name,
        state=WorkerState.RUNNING,
        gpu_type="T4",
        gpu_count=1,
    )
    store.record_colab_created(colab_worker)
    monkeypatch.setattr(cli, "_state_store", lambda: store)

    class FakeColab:
        def __init__(self, config):
            assert config.enabled

        def list_workers(self):
            return [colab_worker]

    monkeypatch.setattr(cli, "ColabClient", FakeColab)
    monkeypatch.setattr(
        cli.RunPodClient,
        "from_settings",
        lambda settings: (_ for _ in ()).throw(
            AssertionError("Colab read should not resolve RunPod credentials")
        ),
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text("[colab]\nenabled = true\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--config", str(config_file), "worker", "list", "--provider", "colab"],
        env={},
    )
    assert result.exit_code == 0
    assert f"colab\t{name}\tRUNNING\tT4" in result.stdout
    assert store.get(name).provider_absent is False


def test_colab_pending_identity_cannot_be_released(monkeypatch, tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    name = "wavcse-123456789abc"
    store.record_colab_intent(name, "T4")
    monkeypatch.setattr(cli, "_state_store", lambda: store)
    monkeypatch.setattr(
        cli,
        "ColabClient",
        lambda config: (_ for _ in ()).throw(
            AssertionError("unconfirmed ownership must not reach provider")
        ),
    )
    config_file = tmp_path / "config.toml"
    config_file.write_text("[colab]\nenabled = true\n", encoding="utf-8")
    result = runner.invoke(
        app, ["--config", str(config_file), "worker", "destroy", name, "--yes"], env={}
    )
    assert result.exit_code == 2
    assert "lacks confirmed ownership" in result.stderr
    assert store.get(name).create_pending


def test_confirmed_owned_colab_session_is_released_only_after_provider_absence(
    monkeypatch, tmp_path: Path
) -> None:
    from wavcse_infra.models import ExecutionTransport, ProviderKind

    name = "wavcse-123456789abc"
    state = WorkerStateStore(tmp_path / "workers.json")
    state.record_colab_intent(name, "T4")
    worker = Worker(
        provider=ProviderKind.COLAB,
        execution_transport=ExecutionTransport.COLAB_EXEC,
        id=name,
        name=name,
        state=WorkerState.RUNNING,
        gpu_type="T4",
        gpu_count=1,
    )
    state.record_colab_created(worker)
    monkeypatch.setattr(cli, "_state_store", lambda: state)
    operations: list[str] = []

    class FakeColab:
        def __init__(self, config):
            assert config.enabled

        def get_worker(self, requested: str) -> Worker:
            assert requested == name
            return worker

        def destroy_worker(self, record) -> None:
            assert record.infra_identity == name and not record.create_pending
            operations.append("release")

        def list_workers(self) -> list[Worker]:
            assert operations == ["release"]
            return []

    monkeypatch.setattr(cli, "ColabClient", FakeColab)
    config_file = tmp_path / "config.toml"
    config_file.write_text("[colab]\nenabled = true\n", encoding="utf-8")
    result = runner.invoke(
        app,
        ["--config", str(config_file), "worker", "destroy", name, "--yes"],
        env={},
    )
    assert result.exit_code == 0
    assert f"terminal release target: {name}" in result.stdout
    assert state.get(name).provider_absent
    assert operations == ["release"]


def test_worker_list_requires_resolvable_credential(monkeypatch) -> None:
    resolver = Mock(
        side_effect=CredentialError(
            "RunPod credential unavailable: unit-test credential sources are disabled"
        )
    )
    monkeypatch.setattr(runpod_provider, "resolve_runpod_api_key", resolver)

    result = runner.invoke(app, ["worker", "list"], env={"RUNPOD_API_KEY": ""})

    assert result.exit_code == 2
    assert "RunPod credential unavailable" in result.stderr
    resolver.assert_called_once()


def test_worker_list_renders_normalized_workers(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    result = runner.invoke(app, ["worker", "list"], env={"RUNPOD_API_KEY": "fake-token"})
    assert result.exit_code == 0
    assert (
        "pod-123\tRUNNING\tNVIDIA RTX A5000\t1\tCOMMUNITY\t$0.1600\twavcse-training-abc123"
    ) in result.stdout


def test_worker_read_only_inspection_does_not_create_or_update_state(
    monkeypatch, tmp_path: Path
) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    state_path = tmp_path / "workers.json"
    listed = runner.invoke(
        app, ["worker", "list", "--read-only", "--json"], env={"RUNPOD_API_KEY": "fake-token"}
    )
    shown = runner.invoke(
        app,
        ["worker", "show", "pod-123", "--read-only", "--json"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert listed.exit_code == shown.exit_code == 0
    assert not state_path.exists()


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


def test_worker_show_displays_proxy_endpoint_without_inventing_direct_endpoint(
    monkeypatch,
    tmp_path: Path,
) -> None:
    proxy = WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="proxy",
        host="ssh.runpod.io",
        port=22,
        username="pod-123-route",
    )
    worker = _worker().model_copy(
        update={
            "public_ip": None,
            "ssh_port": None,
            "ssh_direct": None,
            "ssh_proxy": proxy,
        }
    )
    _install_fakes(monkeypatch, tmp_path, FakeClient(worker=worker))

    result = runner.invoke(
        app,
        ["worker", "show", "pod-123"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert "Public IP: -" in result.stdout
    assert "SSH port: -" in result.stdout
    assert "SSH direct endpoint: -" in result.stdout
    assert "SSH proxy endpoint: pod-123-route@ssh.runpod.io:22" in result.stdout


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
    assert "NVIDIA RTX A5000\t24 GB\tHIGH\t-\t2\t$0.1600\t$0.1600" in result.stdout


def test_gpu_types_can_filter_public_ip_capable_capacity(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    result = runner.invoke(
        app,
        [
            "worker",
            "gpu-types",
            "--cloud",
            "COMMUNITY",
            "--require-direct-ssh",
        ],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0, result.output
    assert "NVIDIA RTX A5000\t24 GB\tHIGH\tYES\t2\t$0.1600\t$0.1600" in result.stdout


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


def test_create_direct_ssh_constraint_reaches_provider(monkeypatch, tmp_path: Path) -> None:
    compatible = _offer().model_copy(update={"public_ip_capable": True})
    client = FakeClient(offer=compatible)
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
            "--start-ssh",
            "--require-direct-ssh",
            "--max-price",
            "0.20",
            "--yes",
        ],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 0, result.output
    assert client.created_specs[0].require_direct_ssh is True


def test_create_direct_ssh_requires_start_ssh_before_provider_call(
    monkeypatch, tmp_path: Path
) -> None:
    client = FakeClient()
    _install_fakes(monkeypatch, tmp_path, client)
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
            "--require-direct-ssh",
            "--max-price",
            "0.20",
            "--yes",
        ],
        env={"RUNPOD_API_KEY": "fake-token"},
    )
    assert result.exit_code == 2
    assert "require_direct_ssh requires start_ssh" in result.stderr
    assert client.create_calls == 0


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


def _connection() -> WorkerConnectionInfo:
    return WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="direct",
        host="203.0.113.9",
        port=30222,
        username="root",
    )


def _health_report(*, ready: bool = True) -> WorkerHealthReport:
    status = HealthCheckStatus.PASS if ready else HealthCheckStatus.FAIL
    return WorkerHealthReport(
        provider_worker_id="pod-123",
        provider_state=WorkerState.RUNNING,
        readiness_state=(WorkerReadinessState.READY if ready else WorkerReadinessState.FAILED),
        connection=_connection(),
        bootstrap_version_expected="1",
        bootstrap_version_observed="1" if ready else None,
        disk_path="/workspace",
        disk_available_bytes=50 * 1024**3,
        git_version="git version 2.43.0",
        python_version="Python 3.12.3",
        uv_version="uv 0.10.9",
        gpu=(
            WorkerGpuInfo(
                count=1,
                models=("NVIDIA RTX A4000",),
                memory_mib=(16376,),
                driver_version="550.54.15",
                cuda_version="12.8",
            )
            if ready
            else None
        ),
        checks=(WorkerHealthCheck(name="gpu", status=status, detail="GPU result"),),
    )


def test_wait_ssh_command_reports_normalized_mapped_endpoint(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    _install_fakes(monkeypatch, tmp_path, client)

    class Waiter:
        def wait(self, worker_id: str, *, timeout_seconds: float | None = None):
            assert (worker_id, timeout_seconds) == ("pod-123", 15)
            return SshWaitResult(worker=_worker(), connection=_connection())

    monkeypatch.setattr(cli, "_worker_access", lambda client, settings: (object(), Waiter()))
    result = runner.invoke(
        app,
        ["worker", "wait-ssh", "pod-123", "--wait-timeout", "15", "--json"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert '"kind": "direct"' in result.stdout
    assert '"port": 30222' in result.stdout
    assert "fake-token" not in result.stdout


def test_ssh_command_uses_proxy_only_for_interactive_pty(monkeypatch, tmp_path: Path) -> None:
    proxy = WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="proxy",
        host="ssh.runpod.io",
        port=22,
        username="pod-123-route",
    )
    worker = _worker().model_copy(update={"ssh_proxy": proxy, "ssh_direct": None})
    _install_fakes(monkeypatch, tmp_path, FakeClient(worker=worker))
    connections: list[WorkerConnectionInfo] = []

    class Executor:
        def __init__(self, settings: object) -> None:
            del settings

        def run_interactive(self, connection: WorkerConnectionInfo) -> int:
            connections.append(connection)
            return 0

    monkeypatch.setattr(cli, "SshExecutor", Executor)
    result = runner.invoke(
        app,
        ["worker", "ssh", "pod-123"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert [connection.kind for connection in connections] == ["proxy"]


def test_exec_command_uses_direct_noninteractive_transport_and_preserves_streams(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())
    calls: list[tuple[tuple[str, ...], float | None]] = []

    class Waiter:
        def wait(self, worker_id: str, *, timeout_seconds: float | None = None):
            assert (worker_id, timeout_seconds) == ("pod-123", 15)
            return SshWaitResult(worker=_worker(), connection=_connection())

    class Executor:
        def run(
            self,
            connection: WorkerConnectionInfo,
            remote_argv: tuple[str, ...],
            *,
            timeout_seconds: float | None = None,
        ) -> SshCommandResult:
            assert connection.kind == "direct"
            calls.append((remote_argv, timeout_seconds))
            return SshCommandResult(0, "command stdout\n", "command stderr\n")

    monkeypatch.setattr(cli, "_ssh_access", lambda client, settings: (Executor(), Waiter()))
    result = runner.invoke(
        app,
        [
            "worker",
            "exec",
            "pod-123",
            "--wait-timeout",
            "15",
            "--command-timeout",
            "20",
            "--",
            "echo",
            "hello world",
        ],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert calls == [(("echo", "hello world"), 20)]
    assert result.stdout == "command stdout\n"
    assert result.stderr == "command stderr\n"


def test_bootstrap_command_reports_ready_health(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())

    class Bootstrapper:
        def bootstrap(self, worker_id: str, **kwargs: object) -> WorkerHealthReport:
            assert worker_id == "pod-123"
            assert kwargs == {"wait_timeout_seconds": None, "command_timeout_seconds": None}
            return _health_report()

        def install_app_config(self, worker_id: str, **kwargs: object) -> tuple[object, ...]:
            assert worker_id == "pod-123"
            assert kwargs == {"wait_timeout_seconds": None, "command_timeout_seconds": None}
            return ()

    monkeypatch.setattr(cli, "_worker_access", lambda client, settings: (Bootstrapper(), object()))
    result = runner.invoke(
        app,
        ["worker", "bootstrap", "pod-123"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert "Readiness: READY" in result.stdout
    assert "PASS gpu: GPU result" in result.stdout
    assert "Disk available: 50.00 GiB" in result.stdout


class _AppConfigExecutor:
    """Answer the app configuration protocol and record every remote call."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        self.calls.append((tuple(remote_argv), input_text))
        mode, *rest = remote_argv[3:]
        if mode == "inspect":
            return SshCommandResult(0, f"wavcse_app_config\t{rest[0]}\t{self.state}\t0\n", "")
        return SshCommandResult(
            0,
            f"wavcse_app_config\t{rest[0]}\tinstalled\t{rest[0]}.wavcse-backup-20261004T153000Z\n",
            "",
        )


class _AppConfigWaiter:
    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> SshWaitResult:
        assert (worker_id, timeout_seconds) == ("pod-123", None)
        return SshWaitResult(worker=_worker(), connection=_connection())


def _install_app_config_bootstrapper(monkeypatch, tmp_path: Path, executor: object) -> None:
    """Wire the CLI to a real bootstrapper that only ever speaks to `executor`."""
    _install_fakes(monkeypatch, tmp_path, FakeClient())

    bootstrapper = WorkerBootstrapper(
        _AppConfigWaiter(),  # type: ignore[arg-type]
        executor,  # type: ignore[arg-type]
        WorkerStateStore(tmp_path / "workers.json"),
        SshConfig(
            private_key=tmp_path / "unused",
            known_hosts_file=tmp_path / "known_hosts",
            command_timeout_seconds=30,
            bootstrap_timeout_seconds=120,
        ),
    )
    monkeypatch.setattr(cli, "_worker_access", lambda client, settings: (bootstrapper, object()))


def test_worker_apply_config_declines_a_differing_file_without_yes(
    monkeypatch, tmp_path: Path
) -> None:
    executor = _AppConfigExecutor("differs")
    _install_app_config_bootstrapper(monkeypatch, tmp_path, executor)

    result = runner.invoke(
        app,
        ["worker", "apply-config", "pod-123"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert "tmux: skipped ~/.tmux.conf" in result.stdout
    assert [argv[3] for argv, _ in executor.calls] == ["inspect"]


def test_worker_apply_config_overrides_with_yes(monkeypatch, tmp_path: Path) -> None:
    executor = _AppConfigExecutor("differs")
    _install_app_config_bootstrapper(monkeypatch, tmp_path, executor)

    result = runner.invoke(
        app,
        ["worker", "apply-config", "pod-123", "--yes"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0, result.output
    assert (
        "tmux: overridden ~/.tmux.conf (backup ~/.tmux.conf.wavcse-backup-20261004T153000Z)"
        in result.stdout
    )
    assert [argv[3] for argv, _ in executor.calls] == ["inspect", "install"]
    assert executor.calls[1][1] is not None and "C-a" in executor.calls[1][1]


def test_worker_apply_config_refuses_an_unknown_application(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())

    result = runner.invoke(
        app,
        ["worker", "apply-config", "pod-123", "--app", "nope"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 2
    assert "mirrored apps are: tmux" in result.stderr


def test_health_command_exits_nonzero_without_ready_transition(monkeypatch, tmp_path: Path) -> None:
    _install_fakes(monkeypatch, tmp_path, FakeClient())

    class Bootstrapper:
        def health(self, worker_id: str, **kwargs: object) -> WorkerHealthReport:
            del worker_id, kwargs
            return _health_report(ready=False)

    monkeypatch.setattr(cli, "_worker_access", lambda client, settings: (Bootstrapper(), object()))
    result = runner.invoke(
        app,
        ["worker", "health", "pod-123", "--json"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 1
    assert '"readiness_state": "FAILED"' in result.stdout
    assert '"ready": false' in result.stdout
