import os
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOL_LINKS_SCRIPT = REPOSITORY_ROOT / "controller" / "tool-links.sh"
BOOTSTRAP_SCRIPT = REPOSITORY_ROOT / "controller" / "bootstrap.sh"

# The tools the script knows about; the tests install a subset so the absent-tool path is
# covered as well.
INSTALLED_TOOLS = ("lazygit", "omp", "uv")


def _install_fake_tool(home: Path, tool: str) -> Path:
    """A stand-in for an installed tool, executable and recognisable when it runs."""
    binary = home / ".local" / "bin" / tool
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(f'#!/usr/bin/env bash\nprintf "{tool} fake\\n"\n', encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _tool_links_argv(home: Path, *arguments: str) -> list[str]:
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
        "tool-links-test",
        str(TOOL_LINKS_SCRIPT),
        str(home),
        *arguments,
    ]


def _run_tool_links(
    home: Path, link_directory: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _tool_links_argv(home, *arguments),
        check=False,
        capture_output=True,
        text=True,
        # The script reads this when it is sourced, so it must be in the child environment.
        env={**os.environ, "WAVCSE_INFRA_TOOL_LINK_DIR": str(link_directory)},
    )


def _resolves_without_a_login_shell(link_directory: Path, tool: str) -> bool:
    """Resolution the way `ssh host '<command>'`, cron, and systemd see it."""
    probe = subprocess.run(
        [
            "env",
            "-i",
            f"PATH={link_directory}:/usr/local/bin:/usr/bin:/bin",
            "sh",
            "-c",
            f"command -v {tool}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0


def test_publishes_installed_tools_and_skips_absent_ones(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    for tool in INSTALLED_TOOLS:
        _install_fake_tool(home, tool)

    result = _run_tool_links(home, link_directory)

    assert result.returncode == 0, result.stderr
    for tool in INSTALLED_TOOLS:
        link = link_directory / tool
        source = home / ".local" / "bin" / tool
        assert link.is_symlink(), f"{tool} was not published"
        assert Path(os.readlink(link)) == source
        assert _resolves_without_a_login_shell(link_directory, tool)
    # A tool that was never installed is reported, not created.
    assert "codex: not installed in ~/.local/bin; nothing to publish" in result.stdout
    assert not (link_directory / "codex").exists()
    assert (
        f"Controller tools published in {link_directory} "
        f"({len(INSTALLED_TOOLS)} linked, 0 already current, 7 not installed)."
    ) in result.stdout


def test_publishing_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    for tool in INSTALLED_TOOLS:
        _install_fake_tool(home, tool)

    first = _run_tool_links(home, link_directory)
    before = {tool: os.readlink(link_directory / tool) for tool in INSTALLED_TOOLS}
    second = _run_tool_links(home, link_directory)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "already current" in second.stdout
    assert "linked" in second.stdout
    assert {tool: os.readlink(link_directory / tool) for tool in INSTALLED_TOOLS} == before
    assert "lazygit: current" in second.stdout


def test_published_tool_runs_through_its_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    _install_fake_tool(home, "lazygit")

    assert _run_tool_links(home, link_directory).returncode == 0
    executed = subprocess.run(
        [str(link_directory / "lazygit")], check=False, capture_output=True, text=True
    )

    assert executed.returncode == 0, executed.stderr
    assert executed.stdout == "lazygit fake\n"


def test_check_reports_drift_and_writes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    _install_fake_tool(home, "lazygit")

    result = _run_tool_links(home, link_directory, "--check")

    assert result.returncode == 1
    assert f"lazygit: missing {link_directory}/lazygit" in result.stdout
    assert not link_directory.exists()


def test_check_is_clean_after_publishing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    _install_fake_tool(home, "lazygit")

    assert _run_tool_links(home, link_directory).returncode == 0
    checked = _run_tool_links(home, link_directory, "--check")

    assert checked.returncode == 0, checked.stderr
    assert f"Controller tools are published in {link_directory}" in checked.stdout


def test_a_foreign_file_is_reported_and_never_replaced(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    _install_fake_tool(home, "lazygit")
    _install_fake_tool(home, "uv")
    link_directory.mkdir(parents=True)
    foreign = link_directory / "uv"
    foreign.write_text("#!/bin/sh\nprintf 'someone elses uv\\n'\n", encoding="utf-8")
    foreign.chmod(0o755)

    result = _run_tool_links(home, link_directory)
    checked = _run_tool_links(home, link_directory, "--check")

    assert result.returncode == 1
    assert f"uv: {foreign} exists and this repository did not create it" in result.stderr
    assert f"Refusing to replace: {foreign}" in result.stderr
    assert foreign.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert not foreign.is_symlink()
    # The conflict is local to that entry: the other tool is still published.
    assert (link_directory / "lazygit").is_symlink()
    assert checked.returncode == 1


def test_a_foreign_symlink_elsewhere_is_reported_and_never_replaced(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    _install_fake_tool(home, "uv")
    link_directory.mkdir(parents=True)
    (link_directory / "uv").symlink_to("/bin/true")

    result = _run_tool_links(home, link_directory)

    assert result.returncode == 1
    assert os.readlink(link_directory / "uv") == "/bin/true"
    assert f"uv: {link_directory / 'uv'} exists and this repository did not create it" in (
        result.stderr
    )


def test_a_symlink_that_resolves_to_the_managed_binary_is_current(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    _install_fake_tool(home, "lazygit")
    link_directory.mkdir(parents=True)
    # A differently spelled, relative pointer at the same binary is already correct.
    relative = os.path.relpath(home / ".local/bin/lazygit", link_directory)
    (link_directory / "lazygit").symlink_to(relative)

    result = _run_tool_links(home, link_directory)

    assert result.returncode == 0, result.stderr
    assert "lazygit: current" in result.stdout


def test_a_removed_tool_leaves_an_orphaned_entry_that_check_reports(tmp_path: Path) -> None:
    home = tmp_path / "home"
    link_directory = tmp_path / "bin"
    binary = _install_fake_tool(home, "lazygit")
    assert _run_tool_links(home, link_directory).returncode == 0

    binary.unlink()
    applied = _run_tool_links(home, link_directory)
    checked = _run_tool_links(home, link_directory, "--check")

    assert applied.returncode == 0, applied.stderr
    assert (
        f"lazygit: not installed in ~/.local/bin but {link_directory / 'lazygit'} remains"
    ) in applied.stderr
    assert "Stale entries left in place: lazygit" in applied.stderr
    assert checked.returncode == 1
    assert (link_directory / "lazygit").is_symlink()


def test_bootstrap_publishes_tool_links_outside_the_skip_agents_gate() -> None:
    bootstrap = BOOTSTRAP_SCRIPT.read_text(encoding="utf-8")

    assert "controller/tool-links.sh" in bootstrap
    assert (
        bootstrap.index("  install_agent_tools\n")
        < bootstrap.index("  publish_tool_links\n")
        < bootstrap.index("printf 'Controller bootstrap complete.\\n'")
    )
