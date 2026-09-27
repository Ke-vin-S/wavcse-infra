from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.doctor import CheckStatus, DoctorCheck, DoctorReport
from wavcse_infra.models import Worker, WorkerState

runner = CliRunner()


def test_help_succeeds() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "Operate reproducible wavCSE infrastructure" in result.stdout
    assert "config" in result.stdout


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
    assert "1 required check" in result.stdout


def test_worker_list_requires_resolvable_credential() -> None:
    result = runner.invoke(app, ["worker", "list"], env={"RUNPOD_API_KEY": ""})

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr
    assert "RunPod credential unavailable" in result.stderr


def test_worker_list_renders_normalized_workers(monkeypatch) -> None:
    worker = Worker(
        id="pod-123",
        name="training-worker",
        state=WorkerState.RUNNING,
        native_status="RUNNING",
        gpu_type="NVIDIA RTX 4090",
        gpu_count=1,
        hourly_cost=Decimal("0.69"),
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def list_workers(self) -> list[Worker]:
            return [worker]

    monkeypatch.setattr(cli.RunPodClient, "from_settings", lambda settings: FakeClient())

    result = runner.invoke(
        app,
        ["worker", "list"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0
    assert "pod-123\tRUNNING\tNVIDIA RTX 4090\t1\t0.6900\ttraining-worker" in result.stdout


def test_worker_show_renders_normalized_worker(monkeypatch) -> None:
    worker = Worker(
        id="pod-123",
        name="training-worker",
        state=WorkerState.STOPPED,
        native_status="EXITED",
        hourly_cost=Decimal("0.69"),
        ssh_port=10341,
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def get_worker(self, worker_id: str) -> Worker:
            assert worker_id == "pod-123"
            return worker

    monkeypatch.setattr(cli.RunPodClient, "from_settings", lambda settings: FakeClient())

    result = runner.invoke(
        app,
        ["worker", "show", "pod-123"],
        env={"RUNPOD_API_KEY": "fake-token"},
    )

    assert result.exit_code == 0
    assert "ID: pod-123" in result.stdout
    assert "State: STOPPED" in result.stdout
    assert "Native status: EXITED" in result.stdout
    assert "SSH port: 10341" in result.stdout
