from pathlib import Path

import pytest
from pydantic import SecretStr

from wavcse_infra.config import Settings
from wavcse_infra.credentials import CredentialSource, ResolvedRunPodCredential
from wavcse_infra.doctor import (
    AwsIdentity,
    CheckStatus,
    DoctorCheck,
    SystemProbes,
    _require_instance_profile,
    run_doctor,
)
from wavcse_infra.errors import CredentialError

PARAMETER_NAME = "/wavcse-infra/runpod/api-key"


class HealthyProbes(SystemProbes):
    def __init__(self, existing_paths: set[Path]) -> None:
        self.existing_paths = existing_paths

    def command_path(self, command: str) -> str | None:
        return f"/usr/bin/{command}"

    def python_version(self) -> tuple[int, int, int]:
        return (3, 12, 4)

    def python_version_text(self) -> str:
        return "3.12.4"

    def path_is_directory(self, path: Path) -> bool:
        return path in self.existing_paths

    def path_is_file(self, path: Path) -> bool:
        return path in self.existing_paths

    def file_mode(self, path: Path) -> int:
        assert path in self.existing_paths
        return 0o600

    def http_status(self, url: str, timeout_seconds: float) -> int:
        del url, timeout_seconds
        return 200

    def aws_identity(self, timeout_seconds: float, region: str | None) -> AwsIdentity:
        del timeout_seconds, region
        return AwsIdentity(
            credential_method="iam-role",
            region="ap-south-1",
            account="123456789012",
            arn="arn:aws:sts::123456789012:assumed-role/controller/i-example",
        )

    def s3_prefix_count(
        self,
        bucket: str,
        prefix: str,
        timeout_seconds: float,
        region: str | None,
    ) -> int:
        del bucket, prefix, timeout_seconds, region
        return 1


def _configured_settings(tmp_path: Path) -> Settings:
    wavcse_path = tmp_path / "wavCSE"
    ssh_key = tmp_path / "worker-key"
    return Settings.model_validate(
        {
            "aws": {"region": "ap-south-1"},
            "paths": {"wavcse": wavcse_path},
            "runpod": {"api_key": SecretStr("fake-token")},
            "storage": {"bucket": "private-wavcse-artifacts", "prefix": "wavcse"},
            "ssh": {"private_key": ssh_key},
        }
    )


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text("# test controller configuration\n", encoding="utf-8")
    return path


def test_doctor_passes_with_expected_controller_dependencies(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key, config_path}

    report = run_doctor(
        settings,
        HealthyProbes(existing_paths),
        config_path=config_path,
    )

    assert report.successful
    assert report.failed_count == 0
    assert all(check.status is not CheckStatus.FAIL for check in report.checks)
    checks = {check.name: check for check in report.checks}
    assert checks["OMP"] == DoctorCheck("OMP", CheckStatus.PASS, "/usr/bin/omp")
    assert checks["Codex"] == DoctorCheck("Codex", CheckStatus.PASS, "/usr/bin/codex")
    assert checks["AGF"] == DoctorCheck("AGF", CheckStatus.PASS, "/usr/bin/agf")
    assert checks["RunPod credential"] == DoctorCheck(
        "RunPod credential",
        CheckStatus.PASS,
        "RUNPOD_API_KEY environment variable",
    )


def test_doctor_reports_ssm_credential_source(tmp_path: Path) -> None:
    configured = _configured_settings(tmp_path)
    settings = configured.model_copy(
        update={
            "runpod": configured.runpod.model_copy(
                update={"api_key": None, "api_key_parameter": PARAMETER_NAME}
            )
        }
    )
    config_path = _config_file(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key, config_path}

    class SsmCredentialProbes(HealthyProbes):
        def runpod_credential(self, settings: Settings) -> ResolvedRunPodCredential:
            del settings
            return ResolvedRunPodCredential(
                api_key=SecretStr("fake-ssm-token"),
                source=CredentialSource.SSM,
                parameter_name=PARAMETER_NAME,
            )

    report = run_doctor(
        settings,
        SsmCredentialProbes(existing_paths),
        config_path=config_path,
    )

    check = next(check for check in report.checks if check.name == "RunPod credential")
    assert check == DoctorCheck(
        "RunPod credential",
        CheckStatus.PASS,
        f"resolved from AWS SSM Parameter Store parameter {PARAMETER_NAME}",
    )


def test_doctor_reports_inaccessible_ssm_parameter_cleanly(tmp_path: Path) -> None:
    configured = _configured_settings(tmp_path)
    settings = configured.model_copy(
        update={
            "runpod": configured.runpod.model_copy(
                update={"api_key": None, "api_key_parameter": PARAMETER_NAME}
            )
        }
    )
    config_path = _config_file(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key, config_path}

    class InaccessibleSsmProbes(HealthyProbes):
        def runpod_credential(self, settings: Settings) -> ResolvedRunPodCredential:
            del settings
            raise CredentialError(
                "RunPod credential unavailable: access denied reading SSM parameter "
                f"{PARAMETER_NAME}"
            )

    report = run_doctor(
        settings,
        InaccessibleSsmProbes(existing_paths),
        config_path=config_path,
    )

    check = next(check for check in report.checks if check.name == "RunPod credential")
    assert check == DoctorCheck(
        "RunPod credential",
        CheckStatus.FAIL,
        f"RunPod credential unavailable: access denied reading SSM parameter {PARAMETER_NAME}",
    )


@pytest.mark.parametrize(
    ("command", "check_name"),
    (("omp", "OMP"), ("codex", "Codex"), ("agf", "AGF")),
)
def test_doctor_reports_each_missing_agent_tool_with_install_command(
    tmp_path: Path,
    command: str,
    check_name: str,
) -> None:
    settings = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key, config_path}

    class MissingAgentProbes(HealthyProbes):
        def command_path(self, candidate: str) -> str | None:
            if candidate == command:
                return None
            return super().command_path(candidate)

    report = run_doctor(
        settings,
        MissingAgentProbes(existing_paths),
        config_path=config_path,
    )

    check = next(check for check in report.checks if check.name == check_name)
    assert check.status is CheckStatus.FAIL
    assert f"{command} was not found on PATH" in check.detail
    assert f"./controller/install-agents.sh --only {command}" in check.detail


def test_doctor_rejects_non_instance_profile_aws_credentials(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)

    class EnvironmentCredentialProbes(HealthyProbes):
        def aws_identity(self, timeout_seconds: float, region: str | None) -> AwsIdentity:
            del timeout_seconds, region
            return AwsIdentity(
                credential_method="env",
                region="ap-south-1",
                account="123456789012",
                arn="arn:aws:iam::123456789012:user/not-the-controller-role",
            )

    report = run_doctor(
        settings,
        EnvironmentCredentialProbes({settings.paths.wavcse, settings.ssh.private_key, config_path}),
        config_path=config_path,
    )

    identity_check = next(check for check in report.checks if check.name == "AWS identity")
    assert identity_check.status is CheckStatus.FAIL
    assert "expected EC2 instance profile" in identity_check.detail


def test_doctor_redacts_external_error_details(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)

    class FailingAwsProbes(HealthyProbes):
        def aws_identity(self, timeout_seconds: float, region: str | None) -> AwsIdentity:
            del timeout_seconds, region
            raise RuntimeError(
                "Authorization: Bearer super-secret "
                "https://example.test/object?X-Amz-Signature=secret"
            )

    report = run_doctor(
        settings,
        FailingAwsProbes({settings.paths.wavcse, settings.ssh.private_key, config_path}),
        config_path=config_path,
    )

    identity_check = next(check for check in report.checks if check.name == "AWS identity")
    assert identity_check.status is CheckStatus.FAIL
    assert "super-secret" not in identity_check.detail
    assert "X-Amz-Signature" not in identity_check.detail
    assert identity_check.detail.count("<redacted>") == 2


def test_unconfigured_optional_ssh_and_mlflow_checks_do_not_fail(tmp_path: Path) -> None:
    configured = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)
    settings = Settings.model_validate(
        {
            "aws": {"region": "ap-south-1"},
            "paths": {"wavcse": configured.paths.wavcse},
            "runpod": {"api_key": SecretStr("fake-token")},
            "storage": {
                "bucket": configured.storage.bucket,
                "prefix": configured.storage.prefix,
            },
            "ssh": {"private_key": None},
            "controller": {
                "expect_omp": False,
                "github_url": "https://github.com",
            },
        }
    )
    probes = HealthyProbes({settings.paths.wavcse, config_path})

    report = run_doctor(settings, probes, config_path=config_path)

    assert report.successful
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["Worker SSH key"] is CheckStatus.WARN
    assert statuses["OMP"] is CheckStatus.SKIP
    assert statuses["MLflow connectivity"] is CheckStatus.SKIP


def test_doctor_reports_loaded_configuration_values(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key, config_path}

    report = run_doctor(
        settings,
        HealthyProbes(existing_paths),
        config_path=config_path,
    )

    checks = {check.name: check for check in report.checks}
    assert checks["Config"] == DoctorCheck("Config", CheckStatus.PASS, str(config_path))
    assert checks["AWS region"] == DoctorCheck("AWS region", CheckStatus.PASS, "ap-south-1")
    assert checks["S3 bucket"] == DoctorCheck(
        "S3 bucket", CheckStatus.PASS, "private-wavcse-artifacts"
    )
    assert checks["Worker SSH key"] == DoctorCheck(
        "Worker SSH key", CheckStatus.PASS, str(settings.ssh.private_key)
    )


def test_doctor_rejects_worker_key_readable_by_other_users(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)
    config_path = _config_file(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key, config_path}

    class InsecureKeyProbes(HealthyProbes):
        def file_mode(self, path: Path) -> int:
            del path
            return 0o644

    report = run_doctor(
        settings,
        InsecureKeyProbes(existing_paths),
        config_path=config_path,
    )

    check = next(check for check in report.checks if check.name == "Worker SSH key")
    assert check.status is CheckStatus.FAIL
    assert "0600 or stricter" in check.detail


def test_doctor_reports_missing_config_and_actionable_keys(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "controller": {"expect_omp": False},
            "paths": {"wavcse": tmp_path / "wavCSE"},
            "runpod": {"api_key": SecretStr("fake-token")},
        }
    )
    missing_config = tmp_path / "missing-config.toml"
    probes = HealthyProbes({settings.paths.wavcse})

    report = run_doctor(settings, probes, config_path=missing_config)

    checks = {check.name: check for check in report.checks}
    assert checks["Config"].status is CheckStatus.FAIL
    assert str(missing_config) in checks["Config"].detail
    assert checks["AWS region"].status is CheckStatus.FAIL
    assert "aws.region" in checks["AWS region"].detail
    assert "WAVCSE_INFRA_AWS_REGION" in checks["AWS region"].detail
    assert checks["S3 bucket"].status is CheckStatus.FAIL
    assert "storage.bucket" in checks["S3 bucket"].detail
    assert "WAVCSE_INFRA_S3_BUCKET" in checks["S3 bucket"].detail
    assert checks["Worker SSH key"].status is CheckStatus.WARN
    assert "ssh.private_key" in checks["Worker SSH key"].detail


def test_system_probe_refuses_static_aws_credentials_before_use() -> None:
    class StaticCredentials:
        method = "env"

    class StaticCredentialSession:
        def get_credentials(self) -> StaticCredentials:
            return StaticCredentials()

    with pytest.raises(RuntimeError, match="expected EC2 instance profile"):
        _require_instance_profile(StaticCredentialSession())
