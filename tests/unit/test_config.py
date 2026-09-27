from pathlib import Path

import pytest

from wavcse_infra.config import DEFAULT_RUNPOD_API_URL, load_settings
from wavcse_infra.errors import ConfigurationError


def test_defaults_are_safe_and_do_not_require_secrets() -> None:
    settings = load_settings(environ={})

    assert str(settings.runpod.api_url).rstrip("/") == DEFAULT_RUNPOD_API_URL
    assert settings.runpod.api_key is None
    assert settings.storage.bucket is None
    assert settings.paths.wavcse == Path("~/projects/wavCSE").expanduser()


def test_precedence_is_cli_then_environment_then_file_then_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[runpod]
request_timeout_seconds = 20
max_read_attempts = 2

[storage]
prefix = "from-file"
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(
        config_path=config_file,
        environ={
            "WAVCSE_INFRA_RUNPOD_TIMEOUT_SECONDS": "30",
            "WAVCSE_INFRA_S3_PREFIX": "from-environment",
        },
        cli_overrides={"runpod.request_timeout_seconds": 40},
    )

    assert settings.runpod.request_timeout_seconds == 40
    assert settings.runpod.max_read_attempts == 2
    assert settings.storage.prefix == "from-environment"


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


def test_unknown_configuration_field_is_an_error(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[runpod]\nunknown = true\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match=r"runpod\.unknown"):
        load_settings(config_path=config_file, environ={})
