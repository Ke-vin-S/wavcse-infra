"""Tests for the deterministic agent-asset sync and drift check.

Every synthetic tree lives under ``tmp_path``; the real repository is never touched.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "agent_assets.py"
SKILL_BODY_LINES = 20


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


def _manifest(root: Path) -> list[str]:
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append(f"link {relative} -> {os.readlink(path)}")
        elif path.is_dir():
            entries.append(f"directory {relative}")
        else:
            entries.append(f"file {relative} {path.stat().st_size}")
    return entries


def _write_skill(
    root: Path,
    name: str,
    *,
    declared_name: str | None = None,
    description: str | None = "A test skill.",
    extra_keys: tuple[str, ...] = (),
) -> Path:
    declared = name if declared_name is None else declared_name
    frontmatter = ["---", f"name: {declared}"]
    if description is not None:
        frontmatter.append(f"description: {description}")
    frontmatter.extend(extra_keys)
    frontmatter.append("---")
    body = [f"Guidance line {index}." for index in range(1, SKILL_BODY_LINES + 1)]
    path = root / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*frontmatter, "", *body]) + "\n", encoding="utf-8")
    return path


def _write_command(
    root: Path,
    name: str,
    *,
    description: str = "A test command.",
    skills: str = "alpha",
    boundaries: str = "read-only",
    body: str = "Body text.",
) -> Path:
    lines = ["---", f"description: {description}", "---", "", f"# {name}", ""]
    lines.append(f"Skills: {skills}")
    lines.append(f"Boundaries: {boundaries}")
    lines.extend(["", body, ""])
    path = root / ".agents" / "commands" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _build_valid_tree(root: Path) -> None:
    (root / "AGENTS.md").write_text("# Agents\n\nRepository guidance.\n", encoding="utf-8")
    makefile = "agents-check:\n\tpython3 scripts/agents/agent_assets.py check\n"
    (root / "Makefile").write_text(makefile, encoding="utf-8")
    _write_skill(root, "alpha")
    _write_skill(root, "beta")
    _write_command(root, "alpha", skills="alpha, beta", boundaries="read-only, no-commit")


def test_check_passes_on_a_tree_that_satisfies_every_rule(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    synced = _run(tmp_path, "sync")
    assert synced.returncode == 0, _output(synced)
    checked = _run(tmp_path, "check")
    assert checked.returncode == 0, _output(checked)
    assert "OK: agent assets are consistent" in checked.stdout


def test_check_mutates_nothing(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    before = _manifest(tmp_path)
    _run(tmp_path, "check")
    assert _manifest(tmp_path) == before


def test_check_fails_when_a_skill_lacks_a_description(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    _write_skill(tmp_path, "alpha", description=None)
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "frontmatter is missing 'description'" in _output(completed)


def test_check_fails_when_a_skill_name_differs_from_its_directory(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    _write_skill(tmp_path, "alpha", declared_name="not-alpha")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "does not match directory 'alpha'" in _output(completed)


def test_check_fails_when_a_description_exceeds_two_hundred_characters(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    _write_skill(tmp_path, "alpha", description="x" * 201)
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "'description' is 201 characters; limit is 200" in _output(completed)


def test_check_fails_when_a_skill_declares_an_unknown_frontmatter_key(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    _write_skill(tmp_path, "alpha", extra_keys=("allowed-tools: Bash",))
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "frontmatter key 'allowed-tools' is not allowed" in _output(completed)


def test_check_fails_when_a_command_references_an_unknown_skill(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    _write_command(tmp_path, "alpha", skills="alpha, ghost", boundaries="read-only")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "references unknown skill 'ghost'" in _output(completed)


def test_check_fails_when_a_command_uses_an_unknown_boundary_flag(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    _write_command(tmp_path, "alpha", skills="alpha, beta", boundaries="read-only, may-fly")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "boundary flag 'may-fly' is not allowed" in _output(completed)


def test_check_fails_when_a_command_has_two_skills_lines(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    command = tmp_path / ".agents" / "commands" / "alpha.md"
    text = command.read_text(encoding="utf-8").replace("Boundaries:", "Skills: ghost\nBoundaries:")
    command.write_text(text, encoding="utf-8")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    output = _output(completed)
    reason = "needs exactly one body line starting with 'Skills:'; found 2"
    assert f".agents/commands/alpha.md:7: {reason}" in output
    assert ".agents/commands/alpha.md:8: references unknown skill 'ghost'" in output


def test_check_fails_when_a_command_has_no_frontmatter(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    command = tmp_path / ".agents" / "commands" / "alpha.md"
    text = "# alpha\n\nSkills: ghost\nBoundaries: read-only\n"
    command.write_text(text, encoding="utf-8")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    output = _output(completed)
    assert "does not start with a '---' frontmatter block" in output
    assert ".agents/commands/alpha.md:3: references unknown skill 'ghost'" in output


def test_check_fails_when_a_skill_frontmatter_is_not_closed(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    skill = tmp_path / ".agents" / "skills" / "alpha" / "SKILL.md"
    body = "\n".join(f"Guidance line {index}." for index in range(1, 21))
    skill.write_text(f"---\nname: alpha\ndescription: ok\n{body}\n", encoding="utf-8")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "frontmatter block is not closed with '---'" in _output(completed)


def test_check_fails_when_a_command_frontmatter_is_not_closed(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    command = tmp_path / ".agents" / "commands" / "alpha.md"
    command.write_text(
        "---\ndescription: A test command.\nSkills: alpha\nBoundaries: read-only\n",
        encoding="utf-8",
    )
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert ".agents/commands/alpha.md:1: frontmatter block is not closed with '---'" in _output(
        completed
    )


def test_check_fails_when_the_omp_agents_file_is_a_regular_file(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    (tmp_path / ".omp").mkdir()
    copy = tmp_path / ".omp" / "AGENTS.md"
    copy.write_text("# stale copy\n", encoding="utf-8")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert ".omp/AGENTS.md:0: is a regular file, not a symlink" in _output(completed)


def test_check_fails_when_the_omp_agents_symlink_target_is_wrong(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    (tmp_path / ".omp").mkdir()
    os.symlink("AGENTS.md", tmp_path / ".omp" / "AGENTS.md")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert "is not the relative target" in _output(completed)


def test_check_fails_when_a_retired_location_exists(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    (tmp_path / ".omp" / "skills").mkdir()
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    assert ".omp/skills:0: is a retired location" in _output(completed)


def test_inert_claude_locations_are_reported_without_failing(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    (tmp_path / ".claude" / "rules").mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("# Claude guidance\n", encoding="utf-8")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 0, _output(completed)
    assert "inert: CLAUDE.md exists" in completed.stdout
    assert "inert: .claude/rules exists" in completed.stdout


def test_sync_is_idempotent_and_check_passes_afterwards(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    first = _run(tmp_path, "sync")
    assert first.returncode == 0, _output(first)
    link = tmp_path / ".omp" / "AGENTS.md"
    assert link.is_symlink()
    assert os.readlink(link) == "../AGENTS.md"
    before = _manifest(tmp_path)
    second = _run(tmp_path, "sync")
    assert second.returncode == 0, _output(second)
    assert _manifest(tmp_path) == before
    assert "unchanged" in second.stdout
    checked = _run(tmp_path, "check")
    assert checked.returncode == 0, _output(checked)


def test_sync_refuses_to_replace_a_regular_file_without_force(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    (tmp_path / ".omp").mkdir()
    target = tmp_path / ".omp" / "AGENTS.md"
    target.write_text("# stale copy\n", encoding="utf-8")
    refused = _run(tmp_path, "sync")
    assert refused.returncode == 1
    assert "--force" in _output(refused)
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "# stale copy\n"
    forced = _run(tmp_path, "sync", "--force")
    assert forced.returncode == 0, _output(forced)
    assert target.is_symlink()
    assert os.readlink(target) == "../AGENTS.md"
    assert "replaced" in forced.stdout


def test_check_fails_on_forbidden_credentials_and_addresses(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    agents_md = tmp_path / "AGENTS.md"
    secret = "\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\nendpoint = 10.0.0.7\n"
    agents_md.write_text(agents_md.read_text(encoding="utf-8") + secret, encoding="utf-8")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 1
    output = _output(completed)
    assert "AGENTS.md:" in output
    assert "contains what looks like an AWS access key id" in output
    assert "contains a bare IPv4 literal" in output


def test_check_accepts_legitimate_lookalikes(tmp_path: Path) -> None:
    _build_valid_tree(tmp_path)
    text = "# Agents\n\nRequires python3.12 and OMP 18.3.2.\n"
    text += "The token AKIAEXAMPLE is too short to be a key.\n"
    text += "Use ssh.runpod and aws-secret-name, never a committed credential.\n"
    (tmp_path / "AGENTS.md").write_text(text, encoding="utf-8")
    _run(tmp_path, "sync")
    completed = _run(tmp_path, "check")
    assert completed.returncode == 0, _output(completed)


def test_documented_state_location_is_allowed_while_a_projects_path_is_not(
    tmp_path: Path,
) -> None:
    _build_valid_tree(tmp_path)
    _run(tmp_path, "sync")
    body = "Tracked state lives under ~/.local/state/wavcse-infra/workers.json."
    _write_command(tmp_path, "alpha", skills="alpha, beta", boundaries="no-commit", body=body)
    accepted = _run(tmp_path, "check")
    assert accepted.returncode == 0, _output(accepted)
    body = "The checkout lives at ~/projects/wavCSE."
    _write_command(tmp_path, "alpha", skills="alpha, beta", boundaries="no-commit", body=body)
    rejected = _run(tmp_path, "check")
    assert rejected.returncode == 1
    reason = "contains a home path other than ~/.local/state/"
    assert f".agents/commands/alpha.md:10: {reason}" in _output(rejected)
