#!/usr/bin/env python3
"""Synchronize and validate this repository's agent knowledge assets.

The canonical assets are real files: ``.agents/skills/<name>/SKILL.md``,
``.agents/commands/<name>.md``, and the repository-root ``AGENTS.md``. The only
tool-specific artifact is ``.omp/AGENTS.md``, a relative symlink to the root
``AGENTS.md``, so there is no generated text that could drift.

    python3 scripts/agents/agent_assets.py sync [--force]
    python3 scripts/agents/agent_assets.py check
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

AGENTS_MD = "AGENTS.md"
OMP_DIR = ".omp"
OMP_AGENTS_LINK = ".omp/AGENTS.md"
OMP_AGENTS_TARGET = "../AGENTS.md"
SKILLS_DIR = ".agents/skills"
COMMANDS_DIR = ".agents/commands"
SKILL_FILENAME = "SKILL.md"
SKILL_KEYS = ("name", "description")
MIN_SKILL_LINES = 20
MAX_DESCRIPTION_CHARS = 200
SYNC_HINT = "python3 scripts/agents/agent_assets.py sync"

BOUNDARY_FLAGS = (
    "may-commit",
    "may-provision-compute",
    "mutates-research-state",
    "no-commit",
    "no-paid-compute",
    "read-only",
    "writes-reports",
)

SHADOW_LOCATIONS = (
    ".claude/commands",
    ".claude/skills",
    ".codex/commands",
    ".codex/skills",
    ".omp/commands",
    ".omp/skills",
)

# These exist on some machines but OMP does not read them and Claude Code is not
# installed here, so their presence is reported and never treated as drift.
INERT_LOCATIONS = (".claude/rules", "CLAUDE.md")

CONTENT_TARGETS = (".agents", "AGENTS.md", "CLAUDE.md", "Makefile")
TOOLING_TARGETS = ("scripts/agents", "Makefile")

# Every needle below is assembled from fragments on purpose: this file lives under
# scripts/agents/, which is one of the paths the check scans, so the file must not
# contain the text it forbids.
HOME_SIGN = "~" + "/"
STATE_HOME = "." + "local/state"
FORBIDDEN_LITERALS = (
    "../" + "wavCSE",
    "../" + "wavcse-infra",
    "$HOME" + "/projects",
    HOME_SIGN + "projects",
    "-----" + "BEGIN",
    "X-" + "Amz-",
    "aws_" + "secret",
    "ssh." + "runpod.io",
    "api." + "runpod.io",
)

ABSOLUTE_HOME_PATTERN = re.compile(r"/home/[A-Za-z0-9._-]+")
ACCESS_KEY_PATTERN = re.compile(r"AKIA[0-9A-Z]{16}")
IPV4_PATTERN = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

# A home path is machine-specific, with exactly one documented exemption: the
# controller state location, which the command documentation refers to by name.
STATE_HOME_EXEMPTION = re.escape(STATE_HOME) + r"(?![A-Za-z0-9._-])"
TILDE_PATH_PATTERN = re.compile(HOME_SIGN + "(?!" + STATE_HOME_EXEMPTION + ")")

Violation = tuple[str, int, str]


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_files(root: Path, target: str) -> list[Path]:
    base = root / target
    if base.is_file():
        return [base]
    if not base.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        found.extend(Path(dirpath) / name for name in sorted(filenames))
    return found


def _forbidden_reasons(line: str) -> list[str]:
    reasons = []
    for literal in FORBIDDEN_LITERALS:
        if literal in line:
            reasons.append(f"contains the forbidden text {literal!r}")
    if ABSOLUTE_HOME_PATTERN.search(line):
        reasons.append("contains an absolute home directory path")
    if TILDE_PATH_PATTERN.search(line):
        reasons.append(f"contains a home path other than {HOME_SIGN}{STATE_HOME}/")
    if ACCESS_KEY_PATTERN.search(line):
        reasons.append("contains what looks like an AWS access key id")
    if IPV4_PATTERN.search(line):
        reasons.append("contains a bare IPv4 literal")
    return reasons


def _frontmatter(lines: list[str]) -> tuple[list[tuple[int, str, str]], int | None] | None:
    """Parse a leading `---` block into (line number, key, value) entries.

    Returns None when there is no leading block, and a body start of None when the
    block is never closed. A line without a colon is reported with an empty key.
    """
    if not lines or lines[0].strip() != "---":
        return None
    entries = []
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            return entries, index + 1
        if ":" in line:
            key, value = line.split(":", 1)
            entries.append((index + 1, key.strip(), value.strip()))
        else:
            entries.append((index + 1, "", line.strip()))
    return entries, None


def _scanned_files(root: Path, targets: tuple[str, ...]) -> list[Path]:
    found = []
    for target in targets:
        found.extend(_iter_files(root, target))
    return sorted(set(found))


def check_root_agents(root: Path, violations: list[Violation]) -> int:
    path = root / AGENTS_MD
    text = _read(path) if path.is_file() else None
    if text is None:
        violations.append((AGENTS_MD, 0, f"is missing; run {SYNC_HINT}"))
        return 0
    if not text.strip():
        violations.append((AGENTS_MD, 0, "is empty"))
    return 1


def check_omp_link(root: Path, violations: list[Violation]) -> int:
    link = root / OMP_AGENTS_LINK
    if not link.is_symlink():
        if link.is_dir():
            violations.append((OMP_AGENTS_LINK, 0, "is a directory, not a symlink"))
        elif link.is_file():
            violations.append((OMP_AGENTS_LINK, 0, "is a regular file, not a symlink"))
        else:
            violations.append((OMP_AGENTS_LINK, 0, f"is missing; run {SYNC_HINT}"))
        return 0
    target = os.readlink(link)
    if os.path.isabs(target) or target != OMP_AGENTS_TARGET:
        reason = f"{target!r} is not the relative target {OMP_AGENTS_TARGET!r}"
        violations.append((OMP_AGENTS_LINK, 0, reason))
    if Path(os.path.realpath(link)) != Path(os.path.realpath(root / AGENTS_MD)):
        violations.append((OMP_AGENTS_LINK, 0, f"does not resolve to the root {AGENTS_MD}"))
    return 1


def check_skill_file(root: Path, skill_file: Path, violations: list[Violation]) -> str | None:
    rel = _rel(root, skill_file)
    text = _read(skill_file)
    if text is None:
        violations.append((rel, 0, "is not readable"))
        return None
    lines = text.splitlines()
    non_blank = sum(1 for line in lines if line.strip())
    if non_blank < MIN_SKILL_LINES:
        reason = f"has {non_blank} non-blank lines; at least {MIN_SKILL_LINES} are required"
        violations.append((rel, 0, reason))
    parsed = _frontmatter(lines)
    if parsed is None:
        violations.append((rel, 1, "does not start with a '---' frontmatter block"))
        return None
    entries, body_start = parsed
    if body_start is None:
        violations.append((rel, 1, "frontmatter block is not closed with '---'"))
    keys: dict[str, tuple[str, int]] = {}
    for line_number, key, value in entries:
        if not key:
            violations.append((rel, line_number, "frontmatter line is not a 'key: value' pair"))
        elif key in keys:
            violations.append((rel, line_number, f"duplicate frontmatter key {key!r}"))
        else:
            keys[key] = (value, line_number)
    for key, (_, line_number) in sorted(keys.items()):
        if key not in SKILL_KEYS:
            violations.append((rel, line_number, f"frontmatter key {key!r} is not allowed"))
    name = keys.get("name")
    if name is None:
        violations.append((rel, 1, "frontmatter is missing 'name'"))
    elif not name[0]:
        violations.append((rel, name[1], "frontmatter 'name' is empty"))
    elif name[0] != skill_file.parent.name:
        owner = skill_file.parent.name
        violations.append((rel, name[1], f"name {name[0]!r} does not match directory {owner!r}"))
    description = keys.get("description")
    if description is None:
        violations.append((rel, 1, "frontmatter is missing 'description'"))
    elif not description[0]:
        violations.append((rel, description[1], "frontmatter 'description' is empty"))
    elif len(description[0]) > MAX_DESCRIPTION_CHARS:
        limit = MAX_DESCRIPTION_CHARS
        reason = f"'description' is {len(description[0])} characters; limit is {limit}"
        violations.append((rel, description[1], reason))
    return name[0] if name and name[0] else None


def check_skills(root: Path, violations: list[Violation]) -> tuple[int, list[tuple[str, str]]]:
    skills_root = root / SKILLS_DIR
    if not skills_root.is_dir():
        return 0, []
    declared = []
    directories = sorted(path for path in skills_root.iterdir() if path.is_dir())
    for directory in directories:
        skill_file = directory / SKILL_FILENAME
        if not skill_file.is_file():
            violations.append((_rel(root, directory), 0, f"is missing {SKILL_FILENAME}"))
            continue
        name = check_skill_file(root, skill_file, violations)
        if name is not None:
            declared.append((name, _rel(root, skill_file)))
    return len(directories), declared


def check_unique_skill_names(declared: list[tuple[str, str]], violations: list[Violation]) -> int:
    seen: dict[str, str] = {}
    for name, path in declared:
        if name in seen:
            violations.append((path, 0, f"skill name {name!r} is also declared by {seen[name]}"))
        else:
            seen[name] = path
    return len(declared)


def _lines_starting_with(body: list[str], start: int, prefix: str) -> list[tuple[int, str]]:
    found = []
    for index, line in enumerate(body):
        if line.startswith(prefix):
            found.append((start + index + 1, line[len(prefix) :].strip()))
    return found


def check_command_file(root: Path, command_file: Path, violations: list[Violation]) -> None:
    rel = _rel(root, command_file)
    text = _read(command_file)
    if text is None:
        violations.append((rel, 0, "is not readable"))
        return
    lines = text.splitlines()
    parsed = _frontmatter(lines)
    entries: list[tuple[int, str, str]] = []
    body_start = 0
    if parsed is None:
        violations.append((rel, 1, "does not start with a '---' frontmatter block"))
    else:
        entries, body_start = parsed
        if body_start is None:
            violations.append((rel, 1, "frontmatter block is not closed with '---'"))
            return
        description = next((value for _, key, value in entries if key == "description"), None)
        if description is None:
            violations.append((rel, 1, "frontmatter is missing 'description'"))
        elif not description:
            violations.append((rel, 1, "frontmatter 'description' is empty"))
    body = lines[body_start:]
    found_lines: dict[str, list[tuple[int, str]]] = {}
    for prefix in ("Skills:", "Boundaries:"):
        found_lines[prefix] = _lines_starting_with(body, body_start, prefix)
        if len(found_lines[prefix]) != 1:
            count = len(found_lines[prefix])
            line_number = found_lines[prefix][0][0] if count else 0
            reason = f"needs exactly one body line starting with {prefix!r}; found {count}"
            violations.append((rel, line_number, reason))
    for line_number, value in found_lines["Skills:"]:
        for name in value.split(","):
            name = name.strip()
            if not name:
                violations.append((rel, line_number, "lists an empty skill name"))
            elif not (root / SKILLS_DIR / name).is_dir():
                violations.append((rel, line_number, f"references unknown skill {name!r}"))
    for line_number, value in found_lines["Boundaries:"]:
        flags = [flag.strip() for flag in value.split(",")]
        if not any(flags):
            violations.append((rel, line_number, "lists no boundary flags"))
        for flag in flags:
            if not flag:
                violations.append((rel, line_number, "lists an empty boundary flag"))
            elif flag not in BOUNDARY_FLAGS:
                violations.append((rel, line_number, f"boundary flag {flag!r} is not allowed"))


def check_commands(root: Path, violations: list[Violation]) -> int:
    commands_root = root / COMMANDS_DIR
    if not commands_root.is_dir():
        return 0
    command_files = sorted(commands_root.glob("*.md"))
    for command_file in command_files:
        check_command_file(root, command_file, violations)
    return len(command_files)


def check_shadow_locations(root: Path, violations: list[Violation]) -> int:
    for location in SHADOW_LOCATIONS:
        if os.path.lexists(root / location):
            violations.append((location, 0, "is a retired location; assets live in .agents/"))
    return len(SHADOW_LOCATIONS)


def scan_forbidden(root: Path, targets: tuple[str, ...], violations: list[Violation]) -> int:
    files = _scanned_files(root, targets)
    for path in files:
        text = _read(path)
        if text is None:
            continue
        rel = _rel(root, path)
        for index, line in enumerate(text.splitlines()):
            for reason in _forbidden_reasons(line):
                violations.append((rel, index + 1, reason))
    return len(files)


def run_check(root: Path) -> int:
    violations: list[Violation] = []
    summaries: list[str] = []

    def record(number: int, label: str, count: int, unit: str, before: int) -> None:
        failures = len(violations) - before
        status = "ok" if failures == 0 else f"FAILED ({failures})"
        summaries.append(f"check {number} {label}: {status} ({count} {unit})")

    before = len(violations)
    record(1, "root AGENTS.md", check_root_agents(root, violations), "file", before)
    before = len(violations)
    record(2, ".omp/AGENTS.md symlink", check_omp_link(root, violations), "link", before)
    before = len(violations)
    skill_count, declared = check_skills(root, violations)
    record(3, "skills", skill_count, "skills", before)
    before = len(violations)
    record(4, "unique skill names", check_unique_skill_names(declared, violations), "names", before)
    before = len(violations)
    record(5, "commands", check_commands(root, violations), "commands", before)
    before = len(violations)
    shadows = check_shadow_locations(root, violations)
    inert = [location for location in INERT_LOCATIONS if os.path.lexists(root / location)]
    unit = f"locations absent; {len(inert)} inert present"
    record(6, "retired locations", shadows, unit, before)
    before = len(violations)
    count = scan_forbidden(root, CONTENT_TARGETS, violations)
    record(7, "forbidden content", count, "files scanned", before)
    before = len(violations)
    count = scan_forbidden(root, TOOLING_TARGETS, violations)
    record(8, "cross-repository paths", count, "files scanned", before)
    for path, line, reason in sorted(set(violations)):
        print(f"{path}:{line}: {reason}")
    for summary in summaries:
        print(summary)
    for location in inert:
        print(f"inert: {location} exists but OMP does not read it")
    if violations:
        print(f"FAILED: {len(violations)} violation(s)")
        return 1
    print("OK: agent assets are consistent")
    return 0


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames:
            os.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            os.rmdir(os.path.join(dirpath, name))
    os.rmdir(path)


def run_sync(root: Path, force: bool) -> int:
    messages = []
    omp_dir = root / OMP_DIR
    if os.path.lexists(omp_dir) and not omp_dir.is_dir():
        print(f"REFUSED: {OMP_DIR} exists and is not a directory", file=sys.stderr)
        return 1
    if omp_dir.is_dir():
        messages.append(f"{OMP_DIR}/: present")
    else:
        omp_dir.mkdir()
        messages.append(f"{OMP_DIR}/: created directory")
    link = root / OMP_AGENTS_LINK
    if link.is_symlink() and os.readlink(link) == OMP_AGENTS_TARGET:
        messages.append(f"{OMP_AGENTS_LINK}: already the relative symlink (unchanged)")
    elif os.path.lexists(link):
        if not force:
            print(f"REFUSED: {OMP_AGENTS_LINK} is not the relative symlink", file=sys.stderr)
            print("rerun with --force to replace it", file=sys.stderr)
            return 1
        _remove_path(link)
        os.symlink(OMP_AGENTS_TARGET, link)
        messages.append(f"{OMP_AGENTS_LINK}: replaced with the relative symlink (--force)")
    else:
        os.symlink(OMP_AGENTS_TARGET, link)
        messages.append(f"{OMP_AGENTS_LINK}: created relative symlink to {OMP_AGENTS_TARGET}")
    for message in sorted(messages):
        print(message)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_assets.py", description="Agent asset tooling.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync_parser = commands.add_parser("sync", help="materialize the tool-specific state")
    sync_parser.add_argument("--force", action="store_true", help="replace an existing file")
    commands.add_parser("check", help="validate the assets without mutating anything")
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "sync":
        return run_sync(root, args.force)
    return run_check(root)


if __name__ == "__main__":
    raise SystemExit(main())
