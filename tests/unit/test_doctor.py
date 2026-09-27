from pathlib import Path

import pytest
from pydantic import SecretStr

from wavcse_infra.config import Settings
from wavcse_infra.doctor import (
    AwsIdentity,
    CheckStatus,
    SystemProbes,
    _require_instance_profile,
    run_doctor,
)


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


def test_doctor_passes_with_expected_controller_dependencies(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)
    existing_paths = {settings.paths.wavcse, settings.ssh.private_key}

    report = run_doctor(settings, HealthyProbes(existing_paths))

    assert report.successful
    assert report.failed_count == 0
    assert all(check.status is not CheckStatus.FAIL for check in report.checks)


def test_doctor_rejects_non_instance_profile_aws_credentials(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)

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
        EnvironmentCredentialProbes({settings.paths.wavcse, settings.ssh.private_key}),
    )

    identity_check = next(check for check in report.checks if check.name == "AWS identity")
    assert identity_check.status is CheckStatus.FAIL
    assert "expected EC2 instance profile" in identity_check.detail


def test_doctor_redacts_external_error_details(tmp_path: Path) -> None:
    settings = _configured_settings(tmp_path)

    class FailingAwsProbes(HealthyProbes):
        def aws_identity(self, timeout_seconds: float, region: str | None) -> AwsIdentity:
            del timeout_seconds, region
            raise RuntimeError(
                "Authorization: Bearer super-secret "
                "https://example.test/object?X-Amz-Signature=secret"
            )

    report = run_doctor(
        settings,
        FailingAwsProbes({settings.paths.wavcse, settings.ssh.private_key}),
    )

    identity_check = next(check for check in report.checks if check.name == "AWS identity")
    assert identity_check.status is CheckStatus.FAIL
    assert "super-secret" not in identity_check.detail
    assert "X-Amz-Signature" not in identity_check.detail
    assert identity_check.detail.count("<redacted>") == 2


def test_unconfigured_optional_ssh_and_mlflow_checks_do_not_fail(tmp_path: Path) -> None:
    configured = _configured_settings(tmp_path)
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
    probes = HealthyProbes({settings.paths.wavcse})

    report = run_doctor(settings, probes)

    assert report.successful
    statuses = {check.name: check.status for check in report.checks}
    assert statuses["Worker SSH key"] is CheckStatus.WARN
    assert statuses["OMP"] is CheckStatus.SKIP
    assert statuses["MLflow connectivity"] is CheckStatus.SKIP


def test_system_probe_refuses_static_aws_credentials_before_use() -> None:
    class StaticCredentials:
        method = "env"

    class StaticCredentialSession:
        def get_credentials(self) -> StaticCredentials:
            return StaticCredentials()

    with pytest.raises(RuntimeError, match="expected EC2 instance profile"):
        _require_instance_profile(StaticCredentialSession())
