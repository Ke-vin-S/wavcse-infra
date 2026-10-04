import os
import pty
import stat
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SCRIPT = REPOSITORY_ROOT / "controller" / "bootstrap.sh"
INSTALL_AGENTS_SCRIPT = REPOSITORY_ROOT / "controller" / "install-agents.sh"
EXAMPLE_CONFIG = REPOSITORY_ROOT / "config" / "infra.example.toml"

APP_CONFIG_SCRIPT = REPOSITORY_ROOT / "controller" / "app-config.sh"
TMUX_SOURCE = REPOSITORY_ROOT / "apps" / "tmux" / "tmux.conf"


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


def _run_colab_install_step(controller_home: Path) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"\nCONTROLLER_USER="$(id -un)"\nCONTROLLER_HOME="$2"\ninstall_colab_cli\n'
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "colab-install-test",
            str(BOOTSTRAP_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write_fake_colab(controller_home: Path, version: str) -> Path:
    binary = controller_home / ".local" / "bin" / "colab"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f'#!/usr/bin/env bash\nprintf "Version: {version}\\n"\n', encoding="utf-8")
    binary.chmod(0o755)
    return binary


def test_colab_install_skips_uv_when_the_pinned_version_is_present(tmp_path: Path) -> None:
    _write_fake_colab(tmp_path, "0.7.4")

    result = _run_colab_install_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Google Colab CLI 0.7.4 is already installed." in result.stdout
    assert not (tmp_path / "uv-calls.txt").exists()


def test_colab_install_pins_the_version_and_never_authenticates_or_requests_compute(
    tmp_path: Path,
) -> None:
    record = tmp_path / "uv-calls.txt"
    uv_binary = tmp_path / ".local" / "bin" / "uv"
    uv_binary.parent.mkdir(parents=True, exist_ok=True)
    uv_binary.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> '{record}'\n"
        f"printf '#!/usr/bin/env bash\\nprintf \"Version: 0.7.4\\\\n\"\\n' > "
        f"'{uv_binary.parent / 'colab'}'\n"
        f"chmod 0755 '{uv_binary.parent / 'colab'}'\n",
        encoding="utf-8",
    )
    uv_binary.chmod(0o755)

    result = _run_colab_install_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert record.read_text(encoding="utf-8").strip() == (
        "tool install --force google-colab-cli==0.7.4"
    )
    assert "Installing pinned Google Colab CLI 0.7.4." in result.stdout
    assert "gcloud auth application-default login --scopes=openid," in result.stdout
    assert "https://www.googleapis.com/auth/colaboratory" in result.stdout
    assert "--auth=adc" in result.stdout
    combined = result.stdout + result.stderr
    for forbidden in ("colab auth", "colab new", "colab run", "--gpu", "--tpu"):
        assert forbidden not in combined


def test_colab_install_rejects_success_without_binary(tmp_path: Path) -> None:
    uv_binary = tmp_path / ".local" / "bin" / "uv"
    uv_binary.parent.mkdir(parents=True, exist_ok=True)
    uv_binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    uv_binary.chmod(0o755)

    result = _run_colab_install_step(tmp_path)

    assert result.returncode != 0
    assert "not executable" in result.stderr


def _app_config_argv(controller_home: Path, *arguments: str) -> list[str]:
    command = (
        'source "$1"\n'
        'CONTROLLER_USER="$(id -un)"\n'
        'WAVCSE_INFRA_CONTROLLER_HOME="$2"\n'
        "shift 2\n"
        'main "$@"\n'
    )
    return [
        "bash",
        "-c",
        command,
        "app-config-test",
        str(APP_CONFIG_SCRIPT),
        str(controller_home),
        *arguments,
    ]


def _run_app_config(
    controller_home: Path, *arguments: str, stdin: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _app_config_argv(controller_home, *arguments),
        check=False,
        capture_output=True,
        text=True,
        input=stdin,
    )


def _run_app_config_on_tty(
    controller_home: Path, *arguments: str, answer: str
) -> subprocess.CompletedProcess[str]:
    """Answer the interactive override prompt through a real terminal."""
    master, slave = pty.openpty()
    try:
        os.write(master, answer.encode("utf-8"))
        return subprocess.run(
            _app_config_argv(controller_home, *arguments),
            check=False,
            capture_output=True,
            text=True,
            stdin=slave,
        )
    finally:
        os.close(master)
        os.close(slave)


def test_controller_app_config_installs_then_reports_unchanged(tmp_path: Path) -> None:
    target = tmp_path / ".tmux.conf"

    installed = _run_app_config(tmp_path)
    rerun = _run_app_config(tmp_path)
    checked = _run_app_config(tmp_path, "--check")

    assert installed.returncode == 0, installed.stderr
    assert installed.stdout == "tmux: installed ~/.tmux.conf\n"
    assert target.read_bytes() == TMUX_SOURCE.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert rerun.stdout == "tmux: unchanged ~/.tmux.conf\n"
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == "tmux: current ~/.tmux.conf\n"


def test_controller_app_config_preserves_a_drifted_file_without_confirmation(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".tmux.conf"
    target.write_text("# local edit\n", encoding="utf-8")

    checked = _run_app_config(tmp_path, "--check")
    applied = _run_app_config(tmp_path)

    assert checked.returncode == 1
    assert checked.stdout == "tmux: drifted ~/.tmux.conf\n"
    assert applied.returncode == 0, applied.stderr
    assert (
        "tmux: preserved ~/.tmux.conf (local content differs; rerun with --yes to override)"
        in applied.stdout
    )
    assert target.read_text(encoding="utf-8") == "# local edit\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".tmux.conf"]


def test_controller_app_config_declined_prompt_keeps_the_local_file(tmp_path: Path) -> None:
    target = tmp_path / ".tmux.conf"
    target.write_text("# local edit\n", encoding="utf-8")

    result = _run_app_config_on_tty(tmp_path, answer="n\n")

    assert result.returncode == 0, result.stderr
    assert "Override ~/.tmux.conf with apps/tmux/tmux.conf?" in result.stdout
    assert "tmux: skipped ~/.tmux.conf" in result.stdout
    assert target.read_text(encoding="utf-8") == "# local edit\n"


def test_controller_app_config_confirmed_prompt_overrides_and_backs_up(tmp_path: Path) -> None:
    target = tmp_path / ".tmux.conf"
    target.write_text("# local edit\n", encoding="utf-8")

    result = _run_app_config_on_tty(tmp_path, answer="y\n")

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == TMUX_SOURCE.read_bytes()
    backups = list(tmp_path.glob(".tmux.conf.wavcse-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# local edit\n"


def test_controller_app_config_override_backs_up_the_previous_file(tmp_path: Path) -> None:
    target = tmp_path / ".tmux.conf"
    target.write_text("# local edit\n", encoding="utf-8")

    result = _run_app_config(tmp_path, "--yes")

    assert result.returncode == 0, result.stderr
    backups = list(tmp_path.glob(".tmux.conf.wavcse-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "# local edit\n"
    assert target.read_bytes() == TMUX_SOURCE.read_bytes()
    assert f"tmux: overridden ~/.tmux.conf (backup ~/{backups[0].name})\n" in result.stdout


def test_controller_app_config_rejects_contradictory_and_unknown_arguments(tmp_path: Path) -> None:
    contradictory = _run_app_config(tmp_path, "--yes", "--preserve-existing")
    unknown = _run_app_config(tmp_path, "--bogus")

    assert contradictory.returncode == 2
    assert "contradict each other" in contradictory.stderr
    assert unknown.returncode == 2
    assert "unknown argument: --bogus" in unknown.stderr
    assert not (tmp_path / ".tmux.conf").exists()


def test_controller_app_config_reports_an_unknown_application(tmp_path: Path) -> None:
    result = _run_app_config(tmp_path, "--app", "nope")

    assert result.returncode != 0
    assert "unknown app 'nope'; mirrored apps are: tmux" in result.stderr


def test_controller_bootstrap_installs_mirrored_app_config_without_overriding() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert 'script="${REPOSITORY_ROOT}/controller/app-config.sh"' in bootstrap
    assert '"${script}" --preserve-existing' in bootstrap
    assert bootstrap.index("install_app_config\n") < bootstrap.index("install_os_packages\n")
