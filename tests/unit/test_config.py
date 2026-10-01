from pathlib import Path

import pytest

from wavcse_infra.config import (
    DEFAULT_RUNPOD_API_URL,
    DEFAULT_RUNPOD_GRAPHQL_URL,
    load_settings,
    resolved_config_path,
)
from wavcse_infra.errors import ConfigurationError

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "infra.example.toml"


def test_defaults_are_safe_and_do_not_require_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = load_settings(environ={})

    assert str(settings.runpod.api_url).rstrip("/") == DEFAULT_RUNPOD_API_URL
    assert str(settings.runpod.graphql_url).rstrip("/") == DEFAULT_RUNPOD_GRAPHQL_URL
    assert settings.runpod.api_key is None
    assert settings.runpod.api_key_parameter is None
    assert settings.storage.bucket is None
    assert settings.paths.wavcse == Path("~/projects/wavCSE").expanduser()
    assert (
        settings.ssh.known_hosts_file
        == Path("~/.local/state/wavcse-infra/known_hosts").expanduser()
    )


def test_default_user_config_is_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config_file = tmp_path / ".config" / "wavcse-infra" / "config.toml"
    config_file.parent.mkdir(parents=True)
    config_file.write_text(
        """
[aws]
region = "us-east-1"

[storage]
bucket = "wavcse-research-artifacts"

[ssh]
private_key = "~/.ssh/wavcse_worker"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(environ={})

    assert resolved_config_path(environ={}) == config_file
    assert settings.aws.region == "us-east-1"
    assert settings.storage.bucket == "wavcse-research-artifacts"
    assert settings.ssh.private_key == tmp_path / ".ssh" / "wavcse_worker"


def test_ssh_environment_settings_override_file_and_expand_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[ssh]\nprivate_key = "/from-file"\nreadiness_timeout_seconds = 30\n',
        encoding="utf-8",
    )

    settings = load_settings(
        config_path=config_file,
        environ={
            "WAVCSE_INFRA_SSH_PRIVATE_KEY": str(tmp_path / "worker-key"),
            "WAVCSE_INFRA_SSH_KNOWN_HOSTS_FILE": str(tmp_path / "known_hosts"),
            "WAVCSE_INFRA_SSH_READINESS_TIMEOUT_SECONDS": "45",
        },
    )

    assert settings.ssh.private_key == tmp_path / "worker-key"
    assert settings.ssh.known_hosts_file == tmp_path / "known_hosts"
    assert settings.ssh.readiness_timeout_seconds == 45


def test_committed_example_is_valid_and_contains_no_secret_fields() -> None:
    settings = load_settings(config_path=EXAMPLE_CONFIG, environ={})
    example_text = EXAMPLE_CONFIG.read_text(encoding="utf-8")

    assert settings.aws.region == "us-east-1"
    assert str(settings.runpod.api_url).rstrip("/") == DEFAULT_RUNPOD_API_URL
    assert str(settings.runpod.graphql_url).rstrip("/") == DEFAULT_RUNPOD_GRAPHQL_URL
    assert settings.runpod.api_key_parameter == "/wavcse-infra/runpod/api-key"
    assert settings.storage.bucket is None
    assert settings.storage.prefix == "wavcse"
    assert settings.ssh.private_key is not None
    assert settings.ssh.private_key.name == "wavcse_worker"
    for secret_name in (
        "RUNPOD_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        assert secret_name not in example_text


def test_precedence_is_cli_then_environment_then_file_then_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[runpod]
request_timeout_seconds = 20
max_read_attempts = 2
poll_interval_seconds = 2
api_key_parameter = "/from-file"

[storage]
prefix = "from-file"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(
        config_path=config_file,
        environ={
            "WAVCSE_INFRA_RUNPOD_TIMEOUT_SECONDS": "30",
            "WAVCSE_INFRA_RUNPOD_API_KEY_PARAMETER": "/from-environment",
            "WAVCSE_INFRA_RUNPOD_POLL_INTERVAL_SECONDS": "4",
            "WAVCSE_INFRA_S3_PREFIX": "from-environment",
        },
        cli_overrides={"runpod.request_timeout_seconds": 40},
    )

    assert settings.runpod.request_timeout_seconds == 40
    assert settings.runpod.max_read_attempts == 2
    assert settings.runpod.poll_interval_seconds == 4
    assert settings.runpod.api_key_parameter == "/from-environment"
    assert settings.storage.prefix == "from-environment"


def test_colab_config_precedence_and_secret_fields_are_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[colab]\nenabled = true\ncli = "colab-from-file"\ncommand_timeout_seconds = 30\n',
        encoding="utf-8",
    )
    settings = load_settings(
        config_path=config_file,
        environ={"WAVCSE_INFRA_COLAB_CLI": "colab-from-environment"},
        cli_overrides={"colab.cli": "/opt/colab"},
    )
    assert settings.colab.enabled
    assert settings.colab.cli == "/opt/colab"
    assert settings.colab.command_timeout_seconds == 30
    with pytest.raises(ConfigurationError):
        load_settings(
            config_path=config_file,
            environ={},
            cli_overrides={"colab.access_token": "must-not-be-configured"},
        )


def test_colab_cu_policy_precedence_and_single_lease_limit(tmp_path: Path) -> None:
    from decimal import Decimal

    from wavcse_infra.models import ProviderKind

    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "[colab]\nminimum_balance_cu = 8\nmax_incremental_rate_cu_per_hour = 2\n"
        'max_job_cu = 4\n[placement]\npreferred_providers = ["runpod", "colab"]\n'
    )
    settings = load_settings(
        config_path=config_file,
        environ={"WAVCSE_INFRA_COLAB_MAX_JOB_CU": "6"},
        cli_overrides={"colab.minimum_balance_cu": "9"},
    )
    assert settings.colab.minimum_balance_cu == Decimal("9")
    assert settings.colab.max_incremental_rate_cu_per_hour == Decimal("2")
    assert settings.colab.max_job_cu == Decimal("6")
    assert settings.placement.preferred_providers == (
        ProviderKind.RUNPOD,
        ProviderKind.COLAB,
    )
    with pytest.raises(ConfigurationError):
        load_settings(
            config_path=config_file,
            environ={},
            cli_overrides={"colab.max_simultaneous_workers": 2},
        )


def test_runpod_key_is_only_loaded_from_environment() -> None:
    settings = load_settings(environ={"RUNPOD_API_KEY": "not-a-real-key"})

    assert settings.runpod.api_key is not None
    assert settings.runpod.api_key.get_secret_value() == "not-a-real-key"
    assert "not-a-real-key" not in repr(settings)


def test_runpod_key_in_config_file_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[runpod]\napi_key = "must-not-be-here"\n', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="RUNPOD_API_KEY"):
        load_settings(config_path=config_file, environ={})


def test_explicit_missing_config_file_is_an_error(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing.toml"

    with pytest.raises(ConfigurationError, match="does not exist"):
        load_settings(config_path=missing_file, environ={})


def test_template_placeholders_are_treated_as_unconfigured(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[aws]
region = "change_me"

[storage]
bucket = "CHANGE_ME"

[ssh]
private_key = " CHANGE_ME "
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_file, environ={})

    assert settings.aws.region is None
    assert settings.storage.bucket is None
    assert settings.ssh.private_key is None


def test_unknown_configuration_field_is_an_error(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[runpod]\nunknown = true\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match=r"runpod\.unknown"):
        load_settings(config_path=config_file, environ={})


def test_urls_cannot_embed_credentials_or_query_secrets() -> None:
    with pytest.raises(ConfigurationError, match="query strings are not allowed"):
        load_settings(
            environ={"WAVCSE_INFRA_RUNPOD_API_URL": "https://example.test/v1?token=secret"}
        )
