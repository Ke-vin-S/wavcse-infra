import stat
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SCRIPT = REPOSITORY_ROOT / "controller" / "bootstrap.sh"
INSTALL_AGENTS_SCRIPT = REPOSITORY_ROOT / "controller" / "install-agents.sh"
EXAMPLE_CONFIG = REPOSITORY_ROOT / "config" / "infra.example.toml"


def _run_user_config_step(controller_home: Path) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"\nCONTROLLER_USER="$(id -un)"\nCONTROLLER_HOME="$2"\nensure_user_config\n'
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bootstrap-config-test",
            str(BOOTSTRAP_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_path_configuration_step(controller_home: Path) -> subprocess.CompletedProcess[str]:
    command = (
        'source "$1"\n'
        'CONTROLLER_USER="$(id -un)"\n'
        'CONTROLLER_HOME="$2"\n'
        'CONTROLLER_PATH="$PATH"\n'
        'CONTROLLER_LOGIN_SHELL="/bin/bash"\n'
        "ensure_path_configuration\n"
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "agent-path-test",
            str(INSTALL_AGENTS_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_existing_agent_install_steps(controller_home: Path) -> subprocess.CompletedProcess[str]:
    command = (
        'source "$1"\n'
        'CONTROLLER_USER="$(id -un)"\n'
        'CONTROLLER_HOME="$2"\n'
        'CONTROLLER_PATH="${CONTROLLER_HOME}/.local/bin:$PATH"\n'
        "install_omp\n"
        "install_codex\n"
        "install_agf\n"
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "existing-agent-test",
            str(INSTALL_AGENTS_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_bootstrap_creates_user_config_when_absent(tmp_path: Path) -> None:
    result = _run_user_config_step(tmp_path)

    config_directory = tmp_path / ".config" / "wavcse-infra"
    config_file = config_directory / "config.toml"
    assert result.returncode == 0, result.stderr
    assert config_file.read_text(encoding="utf-8") == EXAMPLE_CONFIG.read_text(encoding="utf-8")
    assert stat.S_IMODE(config_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert f"Created controller configuration: {config_file}" in result.stdout
    assert f"edit {config_file}" in result.stdout


def test_bootstrap_preserves_existing_user_config(tmp_path: Path) -> None:
    config_file = tmp_path / ".config" / "wavcse-infra" / "config.toml"
    config_file.parent.mkdir(parents=True)
    existing_content = '[storage]\nbucket = "controller-specific-bucket"\n'
    config_file.write_text(existing_content, encoding="utf-8")

    first_result = _run_user_config_step(tmp_path)
    second_result = _run_user_config_step(tmp_path)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert config_file.read_text(encoding="utf-8") == existing_content
    assert f"Preserving existing controller configuration: {config_file}" in first_result.stdout
    assert f"Preserving existing controller configuration: {config_file}" in second_result.stdout


def test_agent_path_configuration_is_idempotent_and_preserves_existing_content(
    tmp_path: Path,
) -> None:
    profile = tmp_path / ".profile"
    existing_content = "# existing shell configuration\nexport EDITOR=vim\n"
    profile.write_text(existing_content, encoding="utf-8")

    first_result = _run_path_configuration_step(tmp_path)
    second_result = _run_path_configuration_step(tmp_path)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    content = profile.read_text(encoding="utf-8")
    assert content.startswith(existing_content)
    assert content.count("# >>> wavcse-infra controller tools >>>") == 1
    assert content.count("# <<< wavcse-infra controller tools <<<") == 1
    assert content.count("${HOME}/.local/bin") == 2
    assert content.count("${HOME}/.cargo/bin") == 2
    assert content.count("${HOME}/.bun/bin") == 2
    assert "already configured" in second_result.stdout


def test_agent_installer_preserves_existing_commands_without_network(tmp_path: Path) -> None:
    binary_directory = tmp_path / ".local" / "bin"
    binary_directory.mkdir(parents=True)
    for command in ("omp", "codex", "agf"):
        binary = binary_directory / command
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)

    result = _run_existing_agent_install_steps(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("preserving it") == 3
    assert all((binary_directory / command).exists() for command in ("omp", "codex", "agf"))


def test_bootstrap_delegates_agent_installation_and_supports_explicit_skip() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert 'INSTALL_AGENTS_SCRIPT="${REPOSITORY_ROOT}/controller/install-agents.sh"' in bootstrap
    assert '"${INSTALL_AGENTS_SCRIPT}"' in bootstrap
    assert "--skip-agents" in bootstrap


def test_worker_scripts_do_not_install_controller_agent_tools() -> None:
    worker_directory = REPOSITORY_ROOT / "worker"
    worker_scripts = worker_directory.rglob("*.sh") if worker_directory.exists() else ()

    for script in worker_scripts:
        content = script.read_text(encoding="utf-8").lower()
        assert "oh-my-pi" not in content
        assert "@openai/codex" not in content
        assert "cargo install agf" not in content
