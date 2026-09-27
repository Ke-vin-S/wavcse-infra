from pathlib import Path

from typer.testing import CliRunner

from wavcse_infra import cli
from wavcse_infra.cli import app
from wavcse_infra.doctor import CheckStatus, DoctorCheck, DoctorReport

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
        checks=(DoctorCheck("AWS identity", CheckStatus.FAIL, "instance profile missing"),)
    )
    monkeypatch.setattr(cli, "run_doctor", lambda settings: report)

    result = runner.invoke(app, ["doctor"], env={})

    assert result.exit_code == 1
    assert "FAIL AWS identity: instance profile missing" in result.stdout
    assert "1 required check" in result.stdout
