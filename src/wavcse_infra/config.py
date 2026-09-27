"""Typed configuration loading with explicit precedence and secret boundaries."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

from wavcse_infra.errors import ConfigurationError

DEFAULT_CONFIG_PATH = Path("~/.config/wavcse-infra/config.toml")
DEFAULT_RUNPOD_API_URL = "https://rest.runpod.io/v1"
TEMPLATE_PLACEHOLDER = "CHANGE_ME"


def _placeholder_as_none(value: object) -> object:
    if isinstance(value, str) and value.strip().upper() == TEMPLATE_PLACEHOLDER:
        return None
    return value


class FrozenModel(BaseModel):
    """Base model for immutable configuration sections."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ControllerConfig(FrozenModel):
    """Controller expectations used by diagnostics."""

    expect_omp: bool = True
    github_url: AnyHttpUrl = AnyHttpUrl("https://github.com")
    mlflow_url: AnyHttpUrl | None = None

    @field_validator("github_url", "mlflow_url")
    @classmethod
    def reject_url_secrets(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        """Keep endpoint credentials and query tokens out of configuration."""

        if value is not None and (value.username or value.password or value.query):
            raise ValueError("URL credentials and query strings are not allowed")
        return value


class PathsConfig(FrozenModel):
    """Local repository paths on the controller."""

    wavcse: Path = Path("~/projects/wavCSE")


class AwsConfig(FrozenModel):
    """Non-secret AWS client configuration."""

    region: str | None = Field(default=None, min_length=1)

    @field_validator("region", mode="before")
    @classmethod
    def template_placeholder_is_unconfigured(cls, value: object) -> object:
        """Do not mistake the committed template marker for a configured region."""

        return _placeholder_as_none(value)


class RunPodConfig(FrozenModel):
    """Safe read-path settings for the RunPod REST API."""

    api_url: AnyHttpUrl = AnyHttpUrl(DEFAULT_RUNPOD_API_URL)
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    max_read_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=0.5, ge=0, le=30)

    @field_validator("api_url")
    @classmethod
    def reject_api_url_secrets(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        """Prevent bearer-like values from being persisted inside endpoint URLs."""

        if value.username or value.password or value.query:
            raise ValueError("URL credentials and query strings are not allowed")
        return value


class StorageConfig(FrozenModel):
    """Canonical artifact storage location."""

    bucket: str | None = Field(default=None, min_length=3, max_length=63)
    prefix: str = Field(default="wavcse", min_length=1)

    @field_validator("bucket", mode="before")
    @classmethod
    def template_placeholder_is_unconfigured(cls, value: object) -> object:
        """Treat the example bucket marker as absent configuration."""

        return _placeholder_as_none(value)


class SshConfig(FrozenModel):
    """Controller-side worker SSH key location."""

    private_key: Path | None = None

    @field_validator("private_key", mode="before")
    @classmethod
    def template_placeholder_is_unconfigured(cls, value: object) -> object:
        """Treat a placeholder path as absent configuration."""

        return _placeholder_as_none(value)


class Settings(FrozenModel):
    """Complete non-secret and runtime-secret application configuration."""

    aws: AwsConfig = AwsConfig()
    controller: ControllerConfig = ControllerConfig()
    paths: PathsConfig = PathsConfig()
    runpod: RunPodConfig = RunPodConfig()
    storage: StorageConfig = StorageConfig()
    ssh: SshConfig = SshConfig()


ENVIRONMENT_FIELDS: dict[str, tuple[str, str]] = {
    "RUNPOD_API_KEY": ("runpod", "api_key"),
    "WAVCSE_INFRA_AWS_REGION": ("aws", "region"),
    "WAVCSE_INFRA_EXPECT_OMP": ("controller", "expect_omp"),
    "WAVCSE_INFRA_MLFLOW_URL": ("controller", "mlflow_url"),
    "WAVCSE_INFRA_RUNPOD_API_URL": ("runpod", "api_url"),
    "WAVCSE_INFRA_RUNPOD_READ_ATTEMPTS": ("runpod", "max_read_attempts"),
    "WAVCSE_INFRA_RUNPOD_RETRY_BACKOFF_SECONDS": ("runpod", "retry_backoff_seconds"),
    "WAVCSE_INFRA_RUNPOD_TIMEOUT_SECONDS": ("runpod", "request_timeout_seconds"),
    "WAVCSE_INFRA_S3_BUCKET": ("storage", "bucket"),
    "WAVCSE_INFRA_S3_PREFIX": ("storage", "prefix"),
    "WAVCSE_INFRA_SSH_PRIVATE_KEY": ("ssh", "private_key"),
    "WAVCSE_INFRA_WAVCSE_PATH": ("paths", "wavcse"),
}


def load_settings(
    *,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, object] | None = None,
) -> Settings:
    """Load settings with CLI > environment > user file > defaults precedence."""

    environment = os.environ if environ is None else environ
    selected_path, is_explicit = _select_config_path(config_path, environment)
    file_values = _load_config_file(selected_path, required=is_explicit)
    environment_values = _environment_values(environment)
    command_values = _dotted_overrides(cli_overrides or {})

    merged = _deep_merge(file_values, environment_values)
    merged = _deep_merge(merged, command_values)
    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigurationError(_format_validation_error(exc)) from exc
    return _expand_paths(settings)


def resolved_config_path(
    config_path: Path | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Return the effective configuration file path without requiring it to exist."""

    environment = os.environ if environ is None else environ
    selected_path, _ = _select_config_path(config_path, environment)
    return selected_path.expanduser()


def _select_config_path(cli_path: Path | None, environment: Mapping[str, str]) -> tuple[Path, bool]:
    if cli_path is not None:
        return cli_path.expanduser(), True
    environment_path = environment.get("WAVCSE_INFRA_CONFIG")
    if environment_path:
        return Path(environment_path).expanduser(), True
    return DEFAULT_CONFIG_PATH.expanduser(), False


def _load_config_file(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigurationError(f"Configuration file does not exist: {path}")
        return {}
    if not path.is_file():
        raise ConfigurationError(f"Configuration path is not a regular file: {path}")

    try:
        with path.open("rb") as config_file:
            values = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"Could not read configuration file {path}: {exc}") from exc

    runpod_values = values.get("runpod", {})
    if isinstance(runpod_values, Mapping) and "api_key" in runpod_values:
        raise ConfigurationError(
            "runpod.api_key is not allowed in configuration files; use RUNPOD_API_KEY"
        )
    return values


def _environment_values(environment: Mapping[str, str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for variable, (section, field) in ENVIRONMENT_FIELDS.items():
        value = environment.get(variable)
        if value is not None and value != "":
            values.setdefault(section, {})[field] = value
    return values


def _dotted_overrides(overrides: Mapping[str, object]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for dotted_name, value in overrides.items():
        if value is None:
            continue
        try:
            section, field = dotted_name.split(".", maxsplit=1)
        except ValueError as exc:
            raise ConfigurationError(f"Invalid CLI configuration key: {dotted_name}") from exc
        values.setdefault(section, {})[field] = value
    return values


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = deepcopy(value)
    return result


def _expand_paths(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "paths": settings.paths.model_copy(
                update={"wavcse": settings.paths.wavcse.expanduser()}
            ),
            "ssh": settings.ssh.model_copy(
                update={
                    "private_key": (
                        settings.ssh.private_key.expanduser()
                        if settings.ssh.private_key is not None
                        else None
                    )
                }
            ),
        }
    )


def _format_validation_error(exc: ValidationError) -> str:
    details = []
    for error in exc.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in error["loc"])
        details.append(f"{location}: {error['msg']}")
    return "Invalid configuration: " + "; ".join(details)
