import logging
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.session import Session as BotocoreSession
from botocore.stub import Stubber
from pydantic import SecretStr

from wavcse_infra.config import Settings, load_settings
from wavcse_infra.credentials import CredentialSource, resolve_runpod_api_key
from wavcse_infra.errors import CredentialError

PARAMETER_NAME = "/wavcse-infra/runpod/api-key"
TEST_SECRET = "unit-test-runpod-secret"


def _settings(
    *, api_key: str | None = None, parameter_name: str | None = PARAMETER_NAME
) -> Settings:
    runpod: dict[str, object] = {"api_key_parameter": parameter_name}
    if api_key is not None:
        runpod["api_key"] = SecretStr(api_key)
    return Settings.model_validate(
        {
            "aws": {"region": "us-east-1"},
            "runpod": runpod,
        }
    )


def _ssm_client():
    session = BotocoreSession()
    session.set_credentials("unit-test-access", "unit-test-secret", "unit-test-session")
    return session.create_client("ssm", region_name="us-east-1")


def _add_secret_response(stubber: Stubber, value: str = TEST_SECRET) -> None:
    stubber.add_response(
        "get_parameter",
        {
            "Parameter": {
                "Name": PARAMETER_NAME,
                "Type": "SecureString",
                "Value": value,
            }
        },
        {"Name": PARAMETER_NAME, "WithDecryption": True},
    )


def test_environment_key_takes_precedence_without_calling_ssm() -> None:
    class UnexpectedSsmClient:
        def get_parameter(self, *, Name: str, WithDecryption: bool):
            del Name, WithDecryption
            raise AssertionError("SSM must not be called when RUNPOD_API_KEY is set")

    credential = resolve_runpod_api_key(
        _settings(api_key="environment-secret"),
        ssm_client=UnexpectedSsmClient(),
    )

    assert credential.source is CredentialSource.ENVIRONMENT
    assert credential.parameter_name is None
    assert credential.api_key.get_secret_value() == "environment-secret"


def test_ssm_secure_string_is_requested_by_name_with_decryption() -> None:
    client = _ssm_client()
    with Stubber(client) as stubber:
        _add_secret_response(stubber)

        credential = resolve_runpod_api_key(_settings(), ssm_client=client)

    assert credential.source is CredentialSource.SSM
    assert credential.parameter_name == PARAMETER_NAME
    assert credential.api_key.get_secret_value() == TEST_SECRET
    assert TEST_SECRET not in repr(credential)


def test_missing_parameter_configuration_is_actionable() -> None:
    with pytest.raises(CredentialError, match=r"runpod\.api_key_parameter is not configured"):
        resolve_runpod_api_key(_settings(parameter_name=None))


@pytest.mark.parametrize(
    ("service_code", "service_message", "expected_message"),
    [
        ("ParameterNotFound", "Parameter does not exist", "SSM parameter not found"),
        ("AccessDeniedException", "Access denied", "access denied reading SSM parameter"),
        ("InvalidKeyId", "KMS key is invalid", "cannot decrypt SSM parameter"),
        (
            "AccessDeniedException",
            "Not authorized to perform kms:Decrypt",
            "cannot decrypt SSM parameter",
        ),
    ],
)
def test_ssm_service_failures_are_translated_without_aws_response_details(
    service_code: str,
    service_message: str,
    expected_message: str,
) -> None:
    client = _ssm_client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_parameter",
            service_error_code=service_code,
            service_message=service_message,
            expected_params={"Name": PARAMETER_NAME, "WithDecryption": True},
        )

        with pytest.raises(CredentialError, match=expected_message) as captured:
            resolve_runpod_api_key(_settings(), ssm_client=client)

    assert PARAMETER_NAME in str(captured.value)
    assert service_message not in str(captured.value)


def test_empty_secure_string_is_rejected() -> None:
    client = _ssm_client()
    with Stubber(client) as stubber:
        _add_secret_response(stubber, value="   ")

        with pytest.raises(CredentialError, match="has an empty value"):
            resolve_runpod_api_key(_settings(), ssm_client=client)


def test_missing_aws_region_is_actionable() -> None:
    settings = Settings.model_validate({"runpod": {"api_key_parameter": PARAMETER_NAME}})

    with pytest.raises(CredentialError, match="AWS region is not configured"):
        resolve_runpod_api_key(settings)


def test_network_failure_is_translated() -> None:
    class FailingSsmClient:
        def get_parameter(self, *, Name: str, WithDecryption: bool):
            del Name, WithDecryption
            raise EndpointConnectionError(endpoint_url="https://ssm.us-east-1.amazonaws.com")

    with pytest.raises(CredentialError, match="AWS API, credentials, or network failure"):
        resolve_runpod_api_key(_settings(), ssm_client=FailingSsmClient())


def test_secret_is_absent_from_errors_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "secret-that-must-never-escape"
    client = _ssm_client()
    with Stubber(client) as stubber:
        stubber.add_client_error(
            "get_parameter",
            service_error_code="AccessDeniedException",
            service_message=f"denied while handling {secret}",
            expected_params={"Name": PARAMETER_NAME, "WithDecryption": True},
        )
        caplog.set_level(logging.DEBUG)

        with pytest.raises(CredentialError) as captured:
            resolve_runpod_api_key(_settings(), ssm_client=client)

    assert secret not in str(captured.value)
    assert secret not in caplog.text


def test_resolver_does_not_modify_config_or_create_files(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_text = (
        f'[aws]\nregion = "us-east-1"\n\n[runpod]\napi_key_parameter = "{PARAMETER_NAME}"\n'
    )
    config_path.write_text(config_text, encoding="utf-8")
    before_files = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    client = _ssm_client()
    with Stubber(client) as stubber:
        _add_secret_response(stubber)

        credential = resolve_runpod_api_key(
            load_settings(config_path=config_path, environ={}),
            ssm_client=client,
        )

    after_files = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert credential.api_key.get_secret_value() == TEST_SECRET
    assert config_path.read_text(encoding="utf-8") == config_text
    assert after_files == before_files
