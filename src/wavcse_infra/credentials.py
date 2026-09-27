"""Runtime-only credential resolution for external providers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

import boto3
from botocore.config import Config as BotocoreConfig
from botocore.exceptions import BotoCoreError, ClientError, NoRegionError
from pydantic import SecretStr

from wavcse_infra.config import Settings
from wavcse_infra.errors import CredentialError

_KMS_ERROR_CODES = frozenset(
    {
        "DecryptionFailure",
        "InvalidKeyId",
        "KMSAccessDeniedException",
        "KMSDisabledException",
        "KMSInvalidStateException",
        "KMSNotFoundException",
    }
)


class CredentialSource(StrEnum):
    """Supported in-memory sources for the RunPod credential."""

    ENVIRONMENT = "environment"
    SSM = "ssm"


@dataclass(frozen=True)
class ResolvedRunPodCredential:
    """A RunPod secret and its safe-to-display provenance."""

    api_key: SecretStr = field(repr=False)
    source: CredentialSource
    parameter_name: str | None = None


class SsmClient(Protocol):
    """Narrow SSM client contract used by the resolver and unit tests."""

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Mapping[str, Any]: ...


def resolve_runpod_api_key(
    settings: Settings,
    *,
    ssm_client: SsmClient | None = None,
) -> ResolvedRunPodCredential:
    """Resolve the RunPod key once, preferring the process environment over SSM."""

    environment_key = settings.runpod.api_key
    if environment_key is not None and environment_key.get_secret_value():
        return ResolvedRunPodCredential(
            api_key=environment_key,
            source=CredentialSource.ENVIRONMENT,
        )

    parameter_name = settings.runpod.api_key_parameter
    if parameter_name is None:
        raise CredentialError(
            "RunPod credential unavailable: RUNPOD_API_KEY is unset and "
            "runpod.api_key_parameter is not configured"
        )
    if settings.aws.region is None:
        raise CredentialError(
            "RunPod credential unavailable: AWS region is not configured; set aws.region "
            "or WAVCSE_INFRA_AWS_REGION before reading SSM parameter "
            f"{parameter_name}"
        )

    try:
        client = ssm_client or _ssm_client(
            settings.aws.region,
            settings.runpod.request_timeout_seconds,
        )
        response = client.get_parameter(Name=parameter_name, WithDecryption=True)
    except NoRegionError as exc:
        raise CredentialError(
            "RunPod credential unavailable: AWS region is unavailable while reading "
            f"SSM parameter {parameter_name}"
        ) from exc
    except ClientError as exc:
        raise _client_error(parameter_name, exc) from exc
    except BotoCoreError as exc:
        raise CredentialError(
            "RunPod credential unavailable: cannot read SSM parameter "
            f"{parameter_name}: AWS API, credentials, or network failure"
        ) from exc

    parameter = response.get("Parameter")
    value = parameter.get("Value") if isinstance(parameter, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise CredentialError(
            f"RunPod credential unavailable: SSM parameter {parameter_name} has an empty value"
        )

    return ResolvedRunPodCredential(
        api_key=SecretStr(value),
        source=CredentialSource.SSM,
        parameter_name=parameter_name,
    )


def _ssm_client(region: str, timeout_seconds: float) -> SsmClient:
    session = boto3.Session(region_name=region)
    return session.client(
        "ssm",
        config=BotocoreConfig(
            connect_timeout=timeout_seconds,
            read_timeout=timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _client_error(parameter_name: str, exc: ClientError) -> CredentialError:
    error = exc.response.get("Error", {})
    code = str(error.get("Code", ""))
    message = str(error.get("Message", ""))
    normalized_code = code.casefold()
    normalized_message = message.casefold()

    if code in _KMS_ERROR_CODES or "kms" in normalized_code or "kms" in normalized_message:
        return CredentialError(
            "RunPod credential unavailable: cannot decrypt SSM parameter "
            f"{parameter_name}; verify kms:Decrypt permission and KMS key state"
        )
    if normalized_code == "parameternotfound":
        return CredentialError(
            f"RunPod credential unavailable: SSM parameter not found: {parameter_name}"
        )
    if "accessdenied" in normalized_code or "unauthorized" in normalized_code:
        return CredentialError(
            f"RunPod credential unavailable: access denied reading SSM parameter {parameter_name}"
        )
    return CredentialError(
        "RunPod credential unavailable: cannot read SSM parameter "
        f"{parameter_name}: AWS API failure ({code or 'unknown error'})"
    )
