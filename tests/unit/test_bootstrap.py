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
OPENCODE_SOURCE = REPOSITORY_ROOT / "apps" / "opencode" / "opencode.jsonc"
LAZYGIT_SOURCE = REPOSITORY_ROOT / "apps" / "lazygit" / "config.yml"
YAZI_SOURCE = REPOSITORY_ROOT / "apps" / "yazi" / "yazi.toml"


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
        "install_opencode\n"
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
    for command in ("omp", "codex", "agf", "opencode"):
        binary = binary_directory / command
        binary.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)

    result = _run_existing_agent_install_steps(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("preserving it") == 4
    assert all(
        (binary_directory / command).exists() for command in ("omp", "codex", "agf", "opencode")
    )


def test_agent_installer_accepts_only_the_documented_tools() -> None:
    help_result = subprocess.run(
        ["bash", str(INSTALL_AGENTS_SCRIPT), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    missing_argument = subprocess.run(
        ["bash", str(INSTALL_AGENTS_SCRIPT), "--only"],
        check=False,
        capture_output=True,
        text=True,
    )
    unknown = subprocess.run(
        ["bash", str(INSTALL_AGENTS_SCRIPT), "--only", "nope"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert help_result.returncode == 0, help_result.stderr
    assert "--only omp|codex|agf|opencode" in help_result.stdout
    assert missing_argument.returncode != 0
    assert "omp, codex, agf, or opencode" in missing_argument.stderr
    assert unknown.returncode != 0
    assert "unsupported tool for --only: nope" in unknown.stderr


def test_opencode_release_details_matches_a_pinned_digest_for_this_architecture() -> None:
    command = 'source "$1"\nopencode_release_details\n'
    result = subprocess.run(
        ["bash", "-c", command, "opencode-release-test", str(INSTALL_AGENTS_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    archive, digest = result.stdout.rstrip("\n").split("\t")
    pinned = {
        "opencode-linux-x64.tar.gz": (
            "c8f888b451f5494a18f858fffb0e0b68f4e4baa9c241761c5f206884f0fa640d",
            "90c97d4a24d36437bce36f27195c9cd2e0b70ed037a04d1c6a6740eb246c2a73",
        ),
        "opencode-linux-arm64.tar.gz": (
            "f7f2ba59ee8aa94d388f9696575a32d20e71c2ee48def9f80fc693a60fec6c72",
        ),
    }
    assert archive in pinned
    assert digest in pinned[archive]


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


def _app_config_argv(
    controller_home: Path, *arguments: str, script: Path = APP_CONFIG_SCRIPT
) -> list[str]:
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
        str(script),
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
    opencode_target = tmp_path / ".config" / "opencode" / "opencode.jsonc"
    lazygit_target = tmp_path / ".config" / "lazygit" / "config.yml"
    yazi_target = tmp_path / ".config" / "yazi" / "yazi.toml"

    installed = _run_app_config(tmp_path)
    rerun = _run_app_config(tmp_path)
    checked = _run_app_config(tmp_path, "--check")

    assert installed.returncode == 0, installed.stderr
    assert installed.stdout == (
        "tmux: installed ~/.tmux.conf\n"
        "opencode: installed ~/.config/opencode/opencode.jsonc\n"
        "lazygit: installed ~/.config/lazygit/config.yml\n"
        "yazi: installed ~/.config/yazi/yazi.toml\n"
    )
    assert target.read_bytes() == TMUX_SOURCE.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert opencode_target.read_bytes() == OPENCODE_SOURCE.read_bytes()
    assert stat.S_IMODE(opencode_target.stat().st_mode) == 0o644
    assert stat.S_IMODE(opencode_target.parent.stat().st_mode) == 0o700
    assert lazygit_target.read_bytes() == LAZYGIT_SOURCE.read_bytes()
    assert stat.S_IMODE(lazygit_target.stat().st_mode) == 0o644
    assert stat.S_IMODE(lazygit_target.parent.stat().st_mode) == 0o700
    assert yazi_target.read_bytes() == YAZI_SOURCE.read_bytes()
    assert stat.S_IMODE(yazi_target.stat().st_mode) == 0o644
    assert stat.S_IMODE(yazi_target.parent.stat().st_mode) == 0o700
    assert rerun.stdout == (
        "tmux: unchanged ~/.tmux.conf\n"
        "opencode: unchanged ~/.config/opencode/opencode.jsonc\n"
        "lazygit: unchanged ~/.config/lazygit/config.yml\n"
        "yazi: unchanged ~/.config/yazi/yazi.toml\n"
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout == (
        "tmux: current ~/.tmux.conf\n"
        "opencode: current ~/.config/opencode/opencode.jsonc\n"
        "lazygit: current ~/.config/lazygit/config.yml\n"
        "yazi: current ~/.config/yazi/yazi.toml\n"
    )


def test_controller_app_config_preserves_a_drifted_file_without_confirmation(
    tmp_path: Path,
) -> None:
    target = tmp_path / ".tmux.conf"
    target.write_text("# local edit\n", encoding="utf-8")

    checked = _run_app_config(tmp_path, "--check")
    applied = _run_app_config(tmp_path)

    assert checked.returncode == 1
    assert checked.stdout == (
        "tmux: drifted ~/.tmux.conf\n"
        "opencode: absent ~/.config/opencode/opencode.jsonc\n"
        "lazygit: absent ~/.config/lazygit/config.yml\n"
        "yazi: absent ~/.config/yazi/yazi.toml\n"
    )
    assert applied.returncode == 0, applied.stderr
    assert (
        "tmux: preserved ~/.tmux.conf (local content differs; rerun with --yes to override)"
        in applied.stdout
    )
    assert "opencode: installed ~/.config/opencode/opencode.jsonc" in applied.stdout
    assert target.read_text(encoding="utf-8") == "# local edit\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == [".config", ".tmux.conf"]


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
    assert "unknown app 'nope'; mirrored apps are: lazygit, opencode, tmux, yazi" in result.stderr


def test_controller_app_config_skips_worker_only_entries(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    script = repository / "controller" / "app-config.sh"
    script.parent.mkdir(parents=True)
    script.write_text(APP_CONFIG_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    for app, source, content in (
        ("tmux", "tmux.conf", "# tmux\n"),
        ("control", "control.conf", "# control\n"),
        ("workeronly", "workeronly.conf", "# worker only\n"),
    ):
        directory = repository / "apps" / app
        directory.mkdir(parents=True)
        (directory / source).write_text(content, encoding="utf-8")
    manifest = repository / "apps" / "manifest"
    manifest.write_text(
        "tmux\ttmux.conf\t.tmux.conf\t0644\n"
        "control\tcontrol.conf\t.control.conf\t0644\tcontroller\n"
        "workeronly\tworkeronly.conf\t.worker.conf\t0644\tworker\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()

    applied = subprocess.run(
        _app_config_argv(home, script=script),
        check=False,
        capture_output=True,
        text=True,
    )
    rejected = subprocess.run(
        _app_config_argv(home, "--app", "workeronly", script=script),
        check=False,
        capture_output=True,
        text=True,
    )

    assert applied.returncode == 0, applied.stderr
    assert (home / ".tmux.conf").read_text(encoding="utf-8") == "# tmux\n"
    assert (home / ".control.conf").read_text(encoding="utf-8") == "# control\n"
    assert not (home / ".worker.conf").exists()
    assert "workeronly: installed" not in applied.stdout
    assert rejected.returncode != 0
    assert "worker-only" in rejected.stderr


def test_controller_bootstrap_installs_mirrored_app_config_without_overriding() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert 'script="${REPOSITORY_ROOT}/controller/app-config.sh"' in bootstrap
    assert '"${script}" --preserve-existing' in bootstrap
    assert bootstrap.index("install_app_config\n") < bootstrap.index("install_os_packages\n")


def _run_git_configuration_step(
    controller_home: Path, **environment: str
) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"\nCONTROLLER_USER="$(id -un)"\nCONTROLLER_HOME="$2"\nconfigure_git\n'
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in ("WAVCSE_INFRA_GIT_USER_NAME", "WAVCSE_INFRA_GIT_USER_EMAIL", "XDG_CONFIG_HOME")
    }
    env["HOME"] = str(controller_home)
    env.update(environment)
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bootstrap-git-test",
            str(BOOTSTRAP_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_bootstrap_configures_the_controller_git_identity(tmp_path: Path) -> None:
    result = _run_git_configuration_step(
        tmp_path,
        WAVCSE_INFRA_GIT_USER_NAME="Controller User",
        WAVCSE_INFRA_GIT_USER_EMAIL="controller@example.com",
    )

    assert result.returncode == 0, result.stderr
    assert "Configured Git identity for" in result.stdout
    config = (tmp_path / ".gitconfig").read_text(encoding="utf-8")
    assert "name = Controller User" in config
    assert "email = controller@example.com" in config


def test_bootstrap_git_identity_is_idempotent(tmp_path: Path) -> None:
    environment = {
        "WAVCSE_INFRA_GIT_USER_NAME": "Controller User",
        "WAVCSE_INFRA_GIT_USER_EMAIL": "controller@example.com",
    }

    first = _run_git_configuration_step(tmp_path, **environment)
    before = (tmp_path / ".gitconfig").read_bytes()
    second = _run_git_configuration_step(tmp_path, **environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "Git identity already configured" in second.stdout
    assert (tmp_path / ".gitconfig").read_bytes() == before


def test_bootstrap_reports_a_replaced_git_identity_without_hiding_the_previous_one(
    tmp_path: Path,
) -> None:
    (tmp_path / ".gitconfig").write_text(
        "[user]\n\tname = Old Name\n\temail = old@example.com\n",
        encoding="utf-8",
    )

    result = _run_git_configuration_step(
        tmp_path,
        WAVCSE_INFRA_GIT_USER_NAME="New Name",
        WAVCSE_INFRA_GIT_USER_EMAIL="new@example.com",
    )

    assert result.returncode == 0, result.stderr
    assert "Replacing Git identity Old Name <old@example.com>" in result.stdout
    config = (tmp_path / ".gitconfig").read_text(encoding="utf-8")
    assert "name = New Name" in config
    assert "email = new@example.com" in config


def test_bootstrap_reports_a_missing_git_identity_without_failing(tmp_path: Path) -> None:
    result = _run_git_configuration_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Git identity not configured" in result.stdout
    assert "WAVCSE_INFRA_GIT_USER_NAME" in result.stdout
    assert not (tmp_path / ".gitconfig").exists()


def test_bootstrap_rejects_a_partial_or_invalid_git_identity(tmp_path: Path) -> None:
    partial = _run_git_configuration_step(tmp_path, WAVCSE_INFRA_GIT_USER_NAME="Only Name")
    invalid = _run_git_configuration_step(
        tmp_path,
        WAVCSE_INFRA_GIT_USER_NAME="Controller User",
        WAVCSE_INFRA_GIT_USER_EMAIL="not-an-email",
    )

    assert partial.returncode != 0
    assert "set both WAVCSE_INFRA_GIT_USER_NAME" in partial.stderr
    assert invalid.returncode != 0
    assert "invalid WAVCSE_INFRA_GIT_USER_EMAIL: not-an-email" in invalid.stderr
    assert not (tmp_path / ".gitconfig").exists()


def test_controller_bootstrap_applies_the_git_identity_after_installing_git() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert (
        bootstrap.index("install_os_packages\n")
        < bootstrap.index("  configure_git\n")
        < bootstrap.index("install_uv\n")
    )


def _run_lazygit_install_step(
    controller_home: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"\nCONTROLLER_USER="$(id -un)"\nCONTROLLER_HOME="$2"\ninstall_lazygit\n'
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "lazygit-install-test",
            str(BOOTSTRAP_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(environment or {})},
    )


def test_lazygit_release_details_matches_a_pinned_digest_for_this_architecture() -> None:
    command = 'source "$1"\nlazygit_release_details\n'
    result = subprocess.run(
        ["bash", "-c", command, "lazygit-release-test", str(BOOTSTRAP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    archive, digest = result.stdout.rstrip("\n").split("\t")
    pinned = {
        "lazygit_0.66.0_linux_x86_64.tar.gz": (
            "5b45541155d20bd32bf2cc5ab5b7e3d91c2eebf0fb1242281350edc27d59d2b7"
        ),
        "lazygit_0.66.0_linux_arm64.tar.gz": (
            "9a4fc4656897ac9f7877b835473ce1a75620cc267f554c57fc4ff266407f3257"
        ),
    }
    assert pinned.get(archive) == digest


def test_lazygit_install_preserves_a_matching_pinned_binary_without_network(
    tmp_path: Path,
) -> None:
    """A matching pin is left alone, and its version is not confused with git's own.

    `lazygit --version` reports a trailing "git version=..." field; reading that field as
    the tool version would send every run back to the network for a redundant download.
    """
    binary = tmp_path / ".local" / "bin" / "lazygit"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'commit=deadbeef, build date=2026-10-05T17:12:24Z, "
        "build source=binaryRelease, version=0.66.0, os=linux, arch=amd64, "
        "git version=2.53.0\\n'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    result = _run_lazygit_install_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "lazygit 0.66.0 is already installed." in result.stdout
    assert "Installing lazygit" not in result.stdout


def test_lazygit_version_override_requires_the_matching_architecture_digest() -> None:
    command = 'source "$1"\nvalidate_lazygit_version_override\n'
    argv = ["bash", "-c", command, "lazygit-override-test", str(BOOTSTRAP_SCRIPT)]

    missing_digest = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "WAVCSE_INFRA_LAZYGIT_VERSION": "0.65.0"},
    )
    supplied_digest = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "WAVCSE_INFRA_LAZYGIT_VERSION": "0.65.0",
            "WAVCSE_INFRA_LAZYGIT_X86_64_SHA256": "0" * 64,
            "WAVCSE_INFRA_LAZYGIT_AARCH64_SHA256": "0" * 64,
        },
    )

    assert missing_digest.returncode != 0
    assert "set WAVCSE_INFRA_LAZYGIT_" in missing_digest.stderr
    assert "_SHA256 when overriding WAVCSE_INFRA_LAZYGIT_VERSION" in missing_digest.stderr
    assert supplied_digest.returncode == 0, supplied_digest.stderr


def test_controller_bootstrap_installs_the_review_ui_after_its_prerequisites() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    # install_lazygit downloads and extracts an archive, so it must follow the package
    # step that provides curl; the locale it depends on is converged before it too.
    assert (
        bootstrap.index("install_os_packages\n")
        < bootstrap.index("  configure_locale\n")
        < bootstrap.index("  install_lazygit\n")
        < bootstrap.index("  install_uv\n")
    )


def _run_yazi_install_step(
    controller_home: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"\nCONTROLLER_USER="$(id -un)"\nCONTROLLER_HOME="$2"\ninstall_yazi\n'
    return subprocess.run(
        [
            "bash",
            "-c",
            command,
            "yazi-install-test",
            str(BOOTSTRAP_SCRIPT),
            str(controller_home),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(environment or {})},
    )


def test_yazi_release_details_matches_a_pinned_digest_for_this_architecture() -> None:
    command = 'source "$1"\nyazi_release_details\n'
    result = subprocess.run(
        ["bash", "-c", command, "yazi-release-test", str(BOOTSTRAP_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    archive, digest = result.stdout.rstrip("\n").split("\t")
    # Yazi's asset names carry no version, so the digest is the whole pin.
    pinned = {
        "yazi-x86_64-unknown-linux-gnu.zip": (
            "a02fe91d3304294048c681f010f1100856872a4e98ecf6927328e888d40a6ad2"
        ),
        "yazi-aarch64-unknown-linux-gnu.zip": (
            "02807f08d6b589b65b7516a4e259d83f5995d7a23bb12b3a155141385b370b3a"
        ),
    }
    assert pinned.get(archive) == digest


def test_yazi_install_preserves_a_matching_pinned_binary_without_network(
    tmp_path: Path,
) -> None:
    """A matching pin is left alone, and its version is read from the right line.

    `yazi --version` prints a program header and several labelled fields before the
    version; reading the header would send every run back to the network for a redundant
    download.
    """
    binary = tmp_path / ".local" / "bin" / "yazi"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'Yazi\\n    Version: 26.9.1 (8dd895c 2026-09-01)\\n"
        "    Debug  : false\\n    Triple : x86_64-unknown-linux-gnu (linux-x86_64)\\n'\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    result = _run_yazi_install_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "yazi 26.9.1 is already installed." in result.stdout
    assert "Installing yazi" not in result.stdout


def test_yazi_version_override_requires_the_matching_architecture_digest() -> None:
    command = 'source "$1"\nvalidate_yazi_version_override\n'
    argv = ["bash", "-c", command, "yazi-override-test", str(BOOTSTRAP_SCRIPT)]

    missing_digest = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "WAVCSE_INFRA_YAZI_VERSION": "26.8.15"},
    )
    supplied_digest = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "WAVCSE_INFRA_YAZI_VERSION": "26.8.15",
            "WAVCSE_INFRA_YAZI_X86_64_SHA256": "0" * 64,
            "WAVCSE_INFRA_YAZI_AARCH64_SHA256": "0" * 64,
        },
    )

    assert missing_digest.returncode != 0
    assert "set WAVCSE_INFRA_YAZI_" in missing_digest.stderr
    assert "_SHA256 when overriding WAVCSE_INFRA_YAZI_VERSION" in missing_digest.stderr
    assert supplied_digest.returncode == 0, supplied_digest.stderr


def test_controller_bootstrap_installs_the_file_explorer_after_its_prerequisites() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    # install_yazi downloads and unzips an archive, so it must follow the package step that
    # provides curl and unzip, and the locale it renders under is converged before it.
    assert (
        bootstrap.index("install_os_packages\n")
        < bootstrap.index("  configure_locale\n")
        < bootstrap.index("  install_yazi\n")
        < bootstrap.index("  install_uv\n")
    )
