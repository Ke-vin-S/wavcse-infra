"""Read-only controller diagnostics."""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.config import Config as BotocoreConfig

from wavcse_infra.config import Settings
from wavcse_infra.credentials import (
    CredentialSource,
    ResolvedRunPodCredential,
    resolve_runpod_api_key,
)
from wavcse_infra.errors import CredentialError
from wavcse_infra.redaction import redact


class CheckStatus(StrEnum):
    """Outcome of one independent diagnostic check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class DoctorCheck:
    """One diagnostic result suitable for human output."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    """Complete doctor report."""

    checks: tuple[DoctorCheck, ...]

    @property
    def successful(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)

    @property
    def failed_count(self) -> int:
        return sum(check.status is CheckStatus.FAIL for check in self.checks)


@dataclass(frozen=True)
class AwsIdentity:
    """Non-secret identity metadata returned by STS."""

    credential_method: str
    region: str
    account: str
    arn: str


class SystemProbes:
    """Read-only adapters for the local machine and external health endpoints."""

    def command_path(self, command: str) -> str | None:
        return shutil.which(command)

    def python_version(self) -> tuple[int, int, int]:
        return sys.version_info[:3]

    def python_version_text(self) -> str:
        return platform.python_version()

    def path_is_directory(self, path: Path) -> bool:
        return path.is_dir()

    def path_is_file(self, path: Path) -> bool:
        return path.is_file()

    def runpod_credential(self, settings: Settings) -> ResolvedRunPodCredential:
        return resolve_runpod_api_key(settings)

    def http_status(self, url: str, timeout_seconds: float) -> int:
        timeout = httpx.Timeout(timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers={"User-Agent": "wavcse-infra/0.1"})
        return response.status_code

    def aws_identity(self, timeout_seconds: float, region: str | None) -> AwsIdentity:
        session = _aws_session(region)
        credential_method = _require_instance_profile(session)
        client = session.client("sts", config=_botocore_config(timeout_seconds))
        response = client.get_caller_identity()
        return AwsIdentity(
            credential_method=credential_method,
            region=str(session.region_name),
            account=str(response["Account"]),
            arn=str(response["Arn"]),
        )

    def s3_prefix_count(
        self,
        bucket: str,
        prefix: str,
        timeout_seconds: float,
        region: str | None,
    ) -> int:
        session = _aws_session(region)
        _require_instance_profile(session)
        client = session.client("s3", config=_botocore_config(timeout_seconds))
        response: dict[str, Any] = client.list_objects_v2(
            Bucket=bucket,
            Prefix=_normalized_s3_prefix(prefix),
            MaxKeys=1,
        )
        return int(response.get("KeyCount", 0))


def run_doctor(
    settings: Settings,
    probes: SystemProbes | None = None,
    *,
    config_path: Path,
) -> DoctorReport:
    """Run all controller checks without changing local or cloud state."""

    active_probes = probes or SystemProbes()
    timeout = settings.runpod.request_timeout_seconds
    expanded_config_path = config_path.expanduser()
    checks = [
        _config_file_check(active_probes, expanded_config_path),
        _aws_region_check(settings, expanded_config_path),
        _s3_bucket_check(settings, expanded_config_path),
        _ssh_key_check(active_probes, settings.ssh.private_key, expanded_config_path),
        _command_check(active_probes, "Git", "git"),
        _python_check(active_probes),
        _command_check(active_probes, "uv", "uv"),
        _command_check(active_probes, "tmux", "tmux"),
        _omp_check(active_probes, settings.controller.expect_omp),
        _agent_tool_check(active_probes, "Codex", "codex"),
        _agent_tool_check(active_probes, "AGF", "agf"),
        _wavcse_directory_check(active_probes, settings.paths.wavcse, expanded_config_path),
        _runpod_credential_check(active_probes, settings),
        _http_check(
            active_probes,
            "GitHub connectivity",
            str(settings.controller.github_url),
            timeout,
        ),
        _runpod_connectivity_check(active_probes, str(settings.runpod.api_url), timeout),
        _aws_identity_check(active_probes, timeout, settings.aws.region),
        _s3_check(active_probes, settings, timeout),
        _mlflow_check(active_probes, settings, timeout),
    ]
    return DoctorReport(checks=tuple(checks))


def _command_check(probes: SystemProbes, name: str, command: str) -> DoctorCheck:
    path = probes.command_path(command)
    if path is None:
        return DoctorCheck(name, CheckStatus.FAIL, f"{command} was not found on PATH")
    return DoctorCheck(name, CheckStatus.PASS, path)


def _agent_tool_check(probes: SystemProbes, name: str, command: str) -> DoctorCheck:
    path = probes.command_path(command)
    if path is None:
        return DoctorCheck(
            name,
            CheckStatus.FAIL,
            f"{command} was not found on PATH; run ./controller/install-agents.sh --only {command}",
        )
    return DoctorCheck(name, CheckStatus.PASS, path)


def _config_file_check(probes: SystemProbes, config_path: Path) -> DoctorCheck:
    if probes.path_is_file(config_path):
        return DoctorCheck("Config", CheckStatus.PASS, str(config_path))
    return DoctorCheck(
        "Config",
        CheckStatus.FAIL,
        f"file does not exist: {config_path}; run ./controller/bootstrap.sh or copy "
        "config/infra.example.toml to that path",
    )


def _aws_region_check(settings: Settings, config_path: Path) -> DoctorCheck:
    region = settings.aws.region
    if region is not None:
        return DoctorCheck("AWS region", CheckStatus.PASS, region)
    return DoctorCheck(
        "AWS region",
        CheckStatus.FAIL,
        f"aws.region is not configured; set it in {config_path} or set WAVCSE_INFRA_AWS_REGION",
    )


def _s3_bucket_check(settings: Settings, config_path: Path) -> DoctorCheck:
    bucket = settings.storage.bucket
    if bucket is not None:
        return DoctorCheck("S3 bucket", CheckStatus.PASS, bucket)
    return DoctorCheck(
        "S3 bucket",
        CheckStatus.FAIL,
        f"storage.bucket is not configured; replace CHANGE_ME in {config_path} or set "
        "WAVCSE_INFRA_S3_BUCKET",
    )


def _python_check(probes: SystemProbes) -> DoctorCheck:
    version = probes.python_version()
    version_text = probes.python_version_text()
    if version < (3, 12, 0):
        return DoctorCheck(
            "Python",
            CheckStatus.FAIL,
            f"Python {version_text} is too old; Python 3.12 or newer is required",
        )
    return DoctorCheck("Python", CheckStatus.PASS, version_text)


def _omp_check(probes: SystemProbes, expected: bool) -> DoctorCheck:
    if not expected:
        return DoctorCheck("OMP", CheckStatus.SKIP, "not required by configuration")
    return _agent_tool_check(probes, "OMP", "omp")


def _wavcse_directory_check(probes: SystemProbes, path: Path, config_path: Path) -> DoctorCheck:
    if probes.path_is_directory(path):
        return DoctorCheck("wavCSE repository", CheckStatus.PASS, str(path))
    return DoctorCheck(
        "wavCSE repository",
        CheckStatus.FAIL,
        f"directory does not exist: {path}; set paths.wavcse in {config_path} or set "
        "WAVCSE_INFRA_WAVCSE_PATH",
    )


def _ssh_key_check(probes: SystemProbes, path: Path | None, config_path: Path) -> DoctorCheck:
    if path is None:
        return DoctorCheck(
            "Worker SSH key",
            CheckStatus.WARN,
            f"ssh.private_key is not configured; set it in {config_path} or set "
            "WAVCSE_INFRA_SSH_PRIVATE_KEY",
        )
    if probes.path_is_file(path):
        return DoctorCheck("Worker SSH key", CheckStatus.PASS, str(path))
    return DoctorCheck(
        "Worker SSH key",
        CheckStatus.FAIL,
        f"file does not exist: {path}; set ssh.private_key in {config_path} or set "
        "WAVCSE_INFRA_SSH_PRIVATE_KEY",
    )


def _runpod_credential_check(probes: SystemProbes, settings: Settings) -> DoctorCheck:
    try:
        credential = probes.runpod_credential(settings)
    except CredentialError as exc:
        return DoctorCheck("RunPod credential", CheckStatus.FAIL, redact(exc))

    if credential.source is CredentialSource.ENVIRONMENT:
        detail = "RUNPOD_API_KEY environment variable"
    else:
        detail = f"resolved from AWS SSM Parameter Store parameter {credential.parameter_name}"
    return DoctorCheck("RunPod credential", CheckStatus.PASS, detail)


def _http_check(
    probes: SystemProbes,
    name: str,
    url: str,
    timeout_seconds: float,
    *,
    accept_client_error: bool = False,
) -> DoctorCheck:
    try:
        status_code = probes.http_status(url, timeout_seconds)
    except Exception as exc:  # External libraries expose several unrelated exception trees.
        return DoctorCheck(name, CheckStatus.FAIL, redact(exc))
    if status_code < 400 or (accept_client_error and status_code < 500):
        return DoctorCheck(name, CheckStatus.PASS, f"reachable (HTTP {status_code})")
    return DoctorCheck(name, CheckStatus.FAIL, f"unexpected HTTP {status_code}")


def _runpod_connectivity_check(
    probes: SystemProbes, api_url: str, timeout_seconds: float
) -> DoctorCheck:
    parts = urlsplit(api_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    return _http_check(
        probes,
        "RunPod connectivity",
        origin,
        timeout_seconds,
        accept_client_error=True,
    )


def _aws_identity_check(
    probes: SystemProbes, timeout_seconds: float, region: str | None
) -> DoctorCheck:
    if region is None:
        return DoctorCheck("AWS identity", CheckStatus.SKIP, "aws.region is not configured")
    try:
        identity = probes.aws_identity(timeout_seconds, region)
    except Exception as exc:  # Botocore failures are converted to one actionable result.
        return DoctorCheck("AWS identity", CheckStatus.FAIL, redact(exc))
    if identity.credential_method != "iam-role":
        return DoctorCheck(
            "AWS identity",
            CheckStatus.FAIL,
            f"credentials resolved from {identity.credential_method}; "
            "expected EC2 instance profile",
        )
    return DoctorCheck(
        "AWS identity",
        CheckStatus.PASS,
        f"account {identity.account}, {identity.arn} (instance profile, region {identity.region})",
    )


def _s3_check(probes: SystemProbes, settings: Settings, timeout_seconds: float) -> DoctorCheck:
    bucket = settings.storage.bucket
    if bucket is None:
        return DoctorCheck("S3 storage", CheckStatus.SKIP, "storage.bucket is not configured")
    if settings.aws.region is None:
        return DoctorCheck("S3 storage", CheckStatus.SKIP, "aws.region is not configured")
    try:
        count = probes.s3_prefix_count(
            bucket,
            settings.storage.prefix,
            timeout_seconds,
            settings.aws.region,
        )
    except Exception as exc:  # Preserve an independent result if S3 alone is unavailable.
        return DoctorCheck("S3 storage", CheckStatus.FAIL, redact(exc))
    return DoctorCheck(
        "S3 storage",
        CheckStatus.PASS,
        f"s3://{bucket}/{_normalized_s3_prefix(settings.storage.prefix)} "
        f"readable ({count} sampled)",
    )


def _mlflow_check(probes: SystemProbes, settings: Settings, timeout_seconds: float) -> DoctorCheck:
    url = settings.controller.mlflow_url
    if url is None:
        return DoctorCheck("MLflow connectivity", CheckStatus.SKIP, "endpoint not configured")
    check = _http_check(probes, "MLflow connectivity", str(url), timeout_seconds)
    if check.status is CheckStatus.FAIL:
        return DoctorCheck(check.name, CheckStatus.WARN, check.detail)
    return check


def _normalized_s3_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    return f"{normalized}/" if normalized else ""


def _botocore_config(timeout_seconds: float) -> BotocoreConfig:
    return BotocoreConfig(
        connect_timeout=timeout_seconds,
        read_timeout=timeout_seconds,
        retries={"max_attempts": 0},
        signature_version="v4",
    )


def _require_instance_profile(session: boto3.Session) -> str:
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("Boto3 did not resolve AWS credentials")
    if credentials.method != "iam-role":
        raise RuntimeError(
            f"AWS credentials resolved from {credentials.method}; expected EC2 instance profile"
        )
    return credentials.method


def _aws_session(region: str | None) -> boto3.Session:
    session = boto3.Session(region_name=region)
    if session.region_name is None:
        raise RuntimeError(
            "AWS region is not configured; set aws.region or WAVCSE_INFRA_AWS_REGION"
        )
    return session
