from pathlib import Path

from typer.testing import CliRunner

from wavcse_infra.cli import app

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
