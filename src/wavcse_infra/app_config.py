"""Repository-mirrored application configuration for the controller and workers.

`apps/manifest` is the single registry of files this control plane keeps in sync
with a remote account's home directory. The controller applies it through
`controller/app-config.sh`; workers through the reviewed applier below, which
verifies a digest before it touches anything and never overwrites a differing
remote file without explicit confirmation.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Literal

from wavcse_infra.errors import ConfigurationError, InfraError
from wavcse_infra.redaction import redact
from wavcse_infra.workers.ssh import SshCommandResult, SshExecutor, WorkerConnectionInfo

_PROTOCOL_KEY = "wavcse_app_config"
APP_CONFIG_TIMEOUT_SECONDS = 60.0
_MANIFEST = "apps/manifest"
_APP_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_MODE_PATTERN = re.compile(r"^[0-7]{3,4}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MODE = 0o644
_DIAGNOSTIC_LIMIT = 500

# Applied on the worker to inspect and then install the reviewed configuration files.
# It is a constant reviewed program: destinations, digests, and modes are passed as
# argv and the payload arrives on stdin, so nothing untrusted is interpolated into it.
_APP_CONFIG_PROGRAM = (
    "import hashlib, os, re, shutil, sys, time\n"
    "PROTOCOL_KEY = 'wavcse_app_config'\n"
    "DIGEST_PATTERN = re.compile(r'^[0-9a-f]{64}$')\n"
    "MODE_PATTERN = re.compile(r'^[0-7]{3,4}$')\n"
    "def refuse(message):\n"
    "    sys.stderr.write(message + '\\n')\n"
    "    raise SystemExit(1)\n"
    "def announce(destination, state, detail):\n"
    "    sys.stdout.write('%s\\t%s\\t%s\\t%s\\n' % (PROTOCOL_KEY, destination, state, detail))\n"
    "def resolve(destination):\n"
    "    if not destination or destination.startswith('/') or '..' in destination.split('/'):\n"
    "        refuse('app configuration destination must be a home-relative path without ..')\n"
    "    path = os.path.expanduser('~/' + destination)\n"
    "    if not os.path.isabs(path):\n"
    "        refuse('could not expand the app configuration destination in the worker home')\n"
    "    return path\n"
    "def read_digest(path):\n"
    "    try:\n"
    "        with open(path, 'rb') as handle:\n"
    "            return hashlib.sha256(handle.read()).hexdigest()\n"
    "    except OSError:\n"
    "        return None\n"
    "command = sys.argv[1] if len(sys.argv) > 1 else ''\n"
    "if command == 'inspect':\n"
    "    pairs = sys.argv[2:]\n"
    "    if not pairs or len(pairs) % 2:\n"
    "        refuse('inspect requires one or more destination and digest pairs')\n"
    "    failed = False\n"
    "    for index in range(0, len(pairs), 2):\n"
    "        destination, expected = pairs[index], pairs[index + 1]\n"
    "        if not DIGEST_PATTERN.match(expected):\n"
    "            refuse('expected digest must be 64 lowercase hexadecimal characters')\n"
    "        path = resolve(destination)\n"
    "        if os.path.islink(path) or os.path.isdir(path):\n"
    "            announce(destination, 'error', 0)\n"
    "            failed = True\n"
    "            continue\n"
    "        current = read_digest(path) if os.path.exists(path) else None\n"
    "        if current is None:\n"
    "            announce(destination, 'absent', 0)\n"
    "        elif current == expected:\n"
    "            announce(destination, 'matches', os.path.getsize(path))\n"
    "        else:\n"
    "            announce(destination, 'differs', os.path.getsize(path))\n"
    "    raise SystemExit(1 if failed else 0)\n"
    "if command != 'install':\n"
    "    refuse('unknown app configuration command')\n"
    "destination, expected, mode_text = sys.argv[2:5]\n"
    "if not MODE_PATTERN.match(mode_text):\n"
    "    refuse('install mode must be 3 or 4 octal digits')\n"
    "if not DIGEST_PATTERN.match(expected):\n"
    "    refuse('expected digest must be 64 lowercase hexadecimal characters')\n"
    "path = resolve(destination)\n"
    "if os.path.islink(path) or os.path.isdir(path):\n"
    "    refuse('refusing to replace a non-regular file at the app configuration destination')\n"
    "payload = sys.stdin.buffer.read()\n"
    "digest = hashlib.sha256(payload).hexdigest()\n"
    "if digest != expected:\n"
    "    refuse('worker app configuration payload digest does not match the reviewed digest')\n"
    "reported_backup = '-'\n"
    "if os.path.exists(path):\n"
    "    if read_digest(path) == digest:\n"
    "        announce(destination, 'unchanged', '-')\n"
    "        raise SystemExit(0)\n"
    "    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())\n"
    "    backup = path + '.wavcse-backup-' + stamp\n"
    "    shutil.copy(path, backup)\n"
    "    try:\n"
    "        shutil.copystat(path, backup)\n"
    "    except OSError:\n"
    "        pass\n"
    "    reported_backup = destination + '.wavcse-backup-' + stamp\n"
    "directory = os.path.dirname(path)\n"
    "if directory:\n"
    "    os.makedirs(directory, mode=0o700, exist_ok=True)\n"
    "temporary = path + '.partial.' + str(os.getpid())\n"
    "try:\n"
    "    with open(temporary, 'wb') as handle:\n"
    "        handle.write(payload)\n"
    "        handle.flush()\n"
    "        os.fsync(handle.fileno())\n"
    "    os.chmod(temporary, int(mode_text, 8))\n"
    "    os.replace(temporary, path)\n"
    "except BaseException:\n"
    "    try:\n"
    "        os.unlink(temporary)\n"
    "    except OSError:\n"
    "        pass\n"
    "    raise\n"
    "announce(destination, 'installed', reported_backup)\n"
)


@dataclass(frozen=True)
class AppConfigEntry:
    """One mirrored file: where it lives in this repository and where it belongs."""

    app: str
    source: str
    destination: str
    mode: int


@dataclass(frozen=True)
class AppConfigOutcome:
    """What applying one mirrored file to one remote account actually did."""

    app: str
    destination: str
    state: Literal["installed", "unchanged", "overridden", "preserved", "skipped", "error"]
    backup: str | None = None
    detail: str | None = None

    def describe(self) -> str:
        """Render one operator-facing line for this outcome."""

        target = f"~/{self.destination}"
        if self.state in {"installed", "unchanged"}:
            return f"{self.app}: {self.state} {target}"
        if self.state == "overridden":
            return f"{self.app}: overridden {target} (backup ~/{self.backup})"
        if self.state in {"preserved", "skipped"}:
            return (
                f"{self.app}: {self.state} {target} "
                "(remote content differs; rerun with --yes to override)"
            )
        return f"{self.app}: error {target} ({self.detail or 'unknown remote error'})"


def load_app_config_entries(*, app: str | None = None) -> tuple[AppConfigEntry, ...]:
    """Load the mirrored application registry, optionally limited to one app."""

    entries = _parse_manifest(_read_registry_file("manifest"))
    if app is None:
        return entries
    selected = tuple(entry for entry in entries if entry.app == app)
    if not selected:
        mirrored = ", ".join(sorted({entry.app for entry in entries}))
        raise ConfigurationError(f"unknown app {app!r}; mirrored apps are: {mirrored}")
    return selected


def load_app_config_content(entry: AppConfigEntry) -> str:
    """Return one mirrored file's exact bytes from a checkout or an installed wheel."""

    return _read_registry_file(*PurePosixPath(entry.source).parts[1:])


def app_config_digest(content: str) -> str:
    """Return the SHA-256 digest used to verify a mirrored file end to end."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def apply_worker_app_config(
    executor: SshExecutor,
    connection: WorkerConnectionInfo,
    *,
    app: str | None = None,
    confirm: Callable[[AppConfigEntry], bool] | None = None,
    timeout_seconds: float = APP_CONFIG_TIMEOUT_SECONDS,
) -> tuple[AppConfigOutcome, ...]:
    """Bring one worker's mirrored configuration in line with this repository.

    An absent file is installed. A differing file is only replaced when `confirm`
    accepts it, and is backed up first; with no `confirm` it is preserved as-is.
    """

    entries = load_app_config_entries(app=app)
    contents = {entry.destination: load_app_config_content(entry) for entry in entries}
    digests = {destination: app_config_digest(content) for destination, content in contents.items()}
    inspect_argv = (
        "python3",
        "-c",
        _APP_CONFIG_PROGRAM,
        "inspect",
        *(value for entry in entries for value in (entry.destination, digests[entry.destination])),
    )
    inspected = executor.run_checked(connection, inspect_argv, timeout_seconds=timeout_seconds)
    states = _parse_inspect_output(inspected)

    outcomes: list[AppConfigOutcome] = []
    for entry in entries:
        state = states.get(entry.destination)
        if state == "absent":
            outcomes.append(
                _install(
                    executor,
                    connection,
                    entry,
                    contents[entry.destination],
                    digests[entry.destination],
                    installed_state="installed",
                    timeout_seconds=timeout_seconds,
                )
            )
        elif state == "matches":
            outcomes.append(AppConfigOutcome(entry.app, entry.destination, "unchanged"))
        elif state == "differs" and confirm is not None and confirm(entry):
            outcomes.append(
                _install(
                    executor,
                    connection,
                    entry,
                    contents[entry.destination],
                    digests[entry.destination],
                    installed_state="overridden",
                    timeout_seconds=timeout_seconds,
                )
            )
        elif state == "differs":
            outcomes.append(
                AppConfigOutcome(entry.app, entry.destination, "preserved")
                if confirm is None
                else AppConfigOutcome(entry.app, entry.destination, "skipped")
            )
        else:
            outcomes.append(
                AppConfigOutcome(
                    entry.app,
                    entry.destination,
                    "error",
                    detail=(
                        "worker reported an error for this destination"
                        if state == "error"
                        else "worker returned no configuration state"
                    ),
                )
            )
    return tuple(outcomes)


def _install(
    executor: SshExecutor,
    connection: WorkerConnectionInfo,
    entry: AppConfigEntry,
    content: str,
    digest: str,
    *,
    installed_state: Literal["installed", "overridden"],
    timeout_seconds: float,
) -> AppConfigOutcome:
    """Install one verified file on the worker and map its report to an outcome."""

    result = executor.run_checked(
        connection,
        (
            "python3",
            "-c",
            _APP_CONFIG_PROGRAM,
            "install",
            entry.destination,
            digest,
            f"{entry.mode:04o}",
        ),
        input_text=content,
        timeout_seconds=timeout_seconds,
    )
    reported = _protocol_report(result, entry.destination)
    if reported is None:
        raise InfraError(
            f"Worker {connection.provider_worker_id} did not report an install result for "
            f"~/{entry.destination}; {_remote_diagnostics(result)}"
        )
    state, backup = reported
    if state == "unchanged":
        return AppConfigOutcome(entry.app, entry.destination, "unchanged")
    if state != "installed":
        raise InfraError(
            f"Worker {connection.provider_worker_id} reported {state!r} while installing "
            f"~/{entry.destination}; {_remote_diagnostics(result)}"
        )
    return AppConfigOutcome(
        entry.app,
        entry.destination,
        installed_state,
        backup=None if backup == "-" else backup,
    )


def _parse_inspect_output(result: SshCommandResult) -> dict[str, str]:
    """Read the worker's per-destination configuration states."""

    states: dict[str, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 4 and fields[0] == _PROTOCOL_KEY:
            states[fields[1]] = fields[2]
    return states


def _protocol_report(result: SshCommandResult, destination: str) -> tuple[str, str] | None:
    """Return the reported install state and backup for exactly one destination."""

    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 4 and fields[0] == _PROTOCOL_KEY and fields[1] == destination:
            return fields[2], fields[3]
    return None


def _parse_manifest(text: str) -> tuple[AppConfigEntry, ...]:
    """Parse the shared registry, refusing anything ambiguous or unsafe."""

    entries: list[AppConfigEntry] = []
    destinations: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if len(fields) not in (3, 4):
            _manifest_error(number, "expected <app> <source> <destination> [mode]")
        app, source, destination = fields[0], fields[1], fields[2]
        if not _APP_PATTERN.match(app):
            _manifest_error(number, f"invalid app name {app!r}")
        _reject_unsafe(number, source, "source")
        _reject_unsafe(number, destination, "destination")
        mode = _DEFAULT_MODE
        if len(fields) == 4:
            if not _MODE_PATTERN.match(fields[3]):
                _manifest_error(number, f"invalid mode {fields[3]!r}; expected 3 or 4 octal digits")
            mode = int(fields[3], 8)
        if destination in destinations:
            _manifest_error(
                number,
                f"duplicate destination {destination!r}, already declared on line "
                f"{destinations[destination]}",
            )
        destinations[destination] = number
        entries.append(
            AppConfigEntry(
                app=app,
                source=f"apps/{app}/{source}",
                destination=destination,
                mode=mode,
            )
        )
    if not entries:
        raise ConfigurationError(f"{_MANIFEST} has no entries to mirror")
    return tuple(entries)


def _reject_unsafe(number: int, value: str, label: str) -> None:
    """Refuse an absolute path or one that can escape the app or home directory."""

    parts = PurePosixPath(value).parts
    if not parts or PurePosixPath(value).is_absolute() or ".." in parts:
        _manifest_error(number, f"unsafe {label} {value!r}; expected a relative path without ..")


def _manifest_error(number: int, reason: str) -> None:
    raise ConfigurationError(f"{_MANIFEST} line {number}: {reason}")


def _read_registry_file(*parts: str) -> str:
    """Read one registry file from a checkout or an installed wheel."""

    repository_file = Path(__file__).resolve().parents[2].joinpath("apps", *parts)
    if repository_file.is_file():
        return repository_file.read_text(encoding="utf-8")
    return resources.files("wavcse_infra").joinpath("apps", *parts).read_text(encoding="utf-8")


def _remote_diagnostics(result: SshCommandResult) -> str:
    return "; ".join(
        (
            _stream_diagnostic("stdout", result.stdout),
            _stream_diagnostic("stderr", result.stderr),
        )
    )


def _stream_diagnostic(name: str, value: str) -> str:
    cleaned = redact(value.strip())
    if not cleaned:
        return f"remote {name}=<empty>"
    return f"remote {name}={cleaned[:_DIAGNOSTIC_LIMIT]!r}"
