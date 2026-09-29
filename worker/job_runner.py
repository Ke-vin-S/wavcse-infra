"""Worker-side exact-commit job runner.

This file runs on a disposable GPU worker, not on the controller. The controller installs
it once at `jobs.runner_path` (verified by SHA-256) and then invokes it over direct,
non-interactive SSH for every job phase:

    python3 <runner> prepare    # stdin: descriptor JSON
    python3 <runner> start      # stdin: descriptor JSON, including secret values
    python3 <runner> inspect    # stdin: descriptor JSON
    python3 <runner> logs       # stdin: descriptor JSON
    python3 <runner> cancel     # stdin: descriptor JSON
    python3 <runner> __supervise__ <job-directory>   # internal, detached

Design properties that must be preserved:

* stdlib only, and Python 3.10 compatible, because the worker is a plain Ubuntu image
  with `python3` installed by `infra worker bootstrap`;
* the job specification's argv is executed directly, never through a shell;
* `HEAD` after checkout is compared with the requested commit, and a mismatch is fatal;
* a job runs in its own session and its output goes to a log file, so closing the SSH
  connection does not destroy the job;
* no AWS credential, RunPod token, GitHub credential, or controller SSH key is accepted
  or forwarded; secret environment values arrive on stdin and live only in process memory;
* nothing printed by this runner includes a descriptor, URL, or credential.

The job directory layout is:

    <worker_root>/<job-id>/
      source/    detached checkout of the requested commit
      inputs/    materialized Phase 5 artifacts
      outputs/   declared experiment outputs
      logs/job.log
      state/     pid, finished, cancelled, started, non-secret descriptor copies, and
                 the advisory prepare/launch locks (released automatically on death)
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any

SCHEMA_KEY = "wavcse_job_schema"
SCHEMA_VERSION = "1"
ERROR_KEY = "wavcse_job_error"
BOOTSTRAP_MARKER = "~/.local/state/wavcse-worker/bootstrap-version"

SOURCE_DIRNAME = "source"
INPUTS_DIRNAME = "inputs"
OUTPUTS_DIRNAME = "outputs"
LOGS_DIRNAME = "logs"
STATE_DIRNAME = "state"
LOG_FILENAME = "job.log"
PID_FILENAME = "pid.json"
FINISHED_FILENAME = "finished.json"
CANCELLED_FILENAME = "cancelled.json"
DESCRIPTOR_FILENAME = "descriptor.json"
JOB_INFO_FILENAME = "job.json"

TIMEOUT_EXIT_CODE = 124
CANCELLED_EXIT_CODE = 143
KILL_GRACE_SECONDS = 15.0
POLL_SECONDS = 0.25
GIT_TIMEOUT_SECONDS = 3600.0
MAX_TAIL_BYTES = 16 * 1024 * 1024
# Deterministic execution environment: an explicit baseline plus declared variables.
BASELINE_ENVIRONMENT = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
}
JOB_ID_PATTERN = re.compile(r"^job-[0-9a-f]{16}$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_CANCELLED = "cancelled"
STATUS_UNKNOWN = "unknown"

# A launch and a cancellation of the same job are mutually exclusive: whichever takes the
# per-job lifecycle lock first decides the outcome, and the other observes that decision.
LIFECYCLE_BUSY_MARKER = "is already in progress on this worker"
CANCELLED_BEFORE_START_MARKER = "was cancelled before it started"

STAGE_SETUP = "setup"
STAGE_COMMAND = "command"
STAGE_STARTUP = "startup"


class RunnerError(Exception):
    """Base class for actionable worker-side job-runner failures."""


class RunnerInputError(RunnerError):
    """Raised when the controller-supplied request is malformed or unsafe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def emit(key: str, value: object) -> None:
    """Write one sanitized tab-separated protocol row on stdout."""

    text = str(value).replace("\t", " ").replace("\n", " ").replace("\r", " ")
    sys.stdout.write(f"{key}\t{text}\n")


def _read_descriptor() -> dict[str, Any]:
    """Parse the descriptor JSON from stdin without ever echoing its contents."""

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RunnerInputError(f"descriptor is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerInputError("descriptor must be a JSON object")
    return payload


def _require_text(container: dict[str, Any], key: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise RunnerInputError(f"descriptor field {key!r} is required")
    return value


def _absolute_job_directory(descriptor: dict[str, Any]) -> str:
    job_directory = _require_text(descriptor, "job_directory")
    if not os.path.isabs(job_directory):
        raise RunnerInputError("descriptor job_directory must be an absolute path")
    if os.pardir in job_directory.split(os.sep):
        raise RunnerInputError("descriptor job_directory must not contain '..' segments")
    if os.path.basename(job_directory) != _job_id(descriptor):
        raise RunnerInputError("descriptor job_directory does not belong to its job_id")
    return job_directory


def _job_id(descriptor: dict[str, Any]) -> str:
    job_id = _require_text(descriptor, "job_id")
    if not JOB_ID_PATTERN.fullmatch(job_id):
        raise RunnerInputError("descriptor job_id is malformed")
    return job_id


def _job_paths(job_directory: str) -> dict[str, str]:
    return {
        "job": job_directory,
        "source": os.path.join(job_directory, SOURCE_DIRNAME),
        "inputs": os.path.join(job_directory, INPUTS_DIRNAME),
        "outputs": os.path.join(job_directory, OUTPUTS_DIRNAME),
        "logs": os.path.join(job_directory, LOGS_DIRNAME),
        "state": os.path.join(job_directory, STATE_DIRNAME),
        "log": os.path.join(job_directory, LOGS_DIRNAME, LOG_FILENAME),
        "pid": os.path.join(job_directory, STATE_DIRNAME, PID_FILENAME),
        "finished": os.path.join(job_directory, STATE_DIRNAME, FINISHED_FILENAME),
        "cancelled": os.path.join(job_directory, STATE_DIRNAME, CANCELLED_FILENAME),
        "descriptor": os.path.join(job_directory, STATE_DIRNAME, DESCRIPTOR_FILENAME),
        "info": os.path.join(job_directory, JOB_INFO_FILENAME),
        "lifecycle": os.path.join(job_directory, STATE_DIRNAME, "lifecycle.lock"),
    }


def resolve_within(root: str, relative: str) -> str:
    """Resolve a relative path beneath one root, refusing any escape or absolute path."""

    if os.path.isabs(relative):
        raise RunnerInputError(f"path must be relative to the job workspace: {relative!r}")
    segments = relative.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise RunnerInputError(f"path must not contain empty, '.' or '..' segments: {relative!r}")
    resolved = os.path.normpath(os.path.join(root, relative))
    root_prefix = os.path.normpath(root) + os.sep
    if not resolved.startswith(root_prefix):
        raise RunnerInputError(f"path escapes the job workspace: {relative!r}")
    return resolved


def _ensure_directories(paths: dict[str, str]) -> None:
    for key in ("job", "source", "inputs", "outputs", "logs", "state"):
        os.makedirs(paths[key], mode=0o700, exist_ok=True)


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    temporary = f"{path}.partial.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"could not read job state {path!r}: {_safe_text(exc)}") from exc
    return payload if isinstance(payload, dict) else None


def _read_optional_json(path: str) -> dict[str, Any] | None:
    """Read state that may legitimately be absent, empty, or written by an older build.

    `start.lock` is the only file with that history: it was an empty marker before it
    started carrying the launch identity, and an unreadable or empty marker must never
    break an inspection of a job that is otherwise fine.
    """

    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _acquire_prepare_lock(paths: dict[str, str]) -> int:
    """Take this job's prepare lock, or report that another prepare already owns it.

    An advisory `flock` is released by the kernel when the process dies, so a controller
    that stopped waiting can always retry prepare; a controller that repeats a phase the
    worker is still running is told so instead of racing two checkouts of one directory.
    """

    path = os.path.join(paths["state"], "prepare.lock")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise RunnerError(
            f"could not open the prepare lock for this job: {_safe_text(exc)}"
        ) from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RunnerError(
                "another prepare for this job is already in progress on this worker"
            ) from exc
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _acquire_lifecycle_lock(paths: dict[str, str]) -> int | None:
    """Try to take the per-job lifecycle lock; None when another operation owns it.

    The lock serializes the two operations that must not interleave: creating the job's
    process, and cancelling a job whose process does not exist yet. It is an advisory
    `flock`, so it is released by the kernel if the holder dies, and it is only ever held
    for the length of a claim, never across a checkout or a command.
    """

    path = paths["lifecycle"]
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise RunnerError(f"could not open the lifecycle lock: {_safe_text(exc)}") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _claim_start_lock(paths: dict[str, str]) -> None:
    """Claim this job's single launch slot, recording the process that owns it.

    The identity is what lets a controller prove, later and without guessing, that a
    launch which never recorded a supervisor can no longer be running. The creation is
    exclusive, so a repeated launch is always refused rather than duplicated.
    """

    path = os.path.join(paths["state"], "start.lock")
    payload = {
        "schema_version": 1,
        "pid": os.getpid(),
        "starttime": _process_starttime(os.getpid()),
        "started_at": _utc_now(),
    }
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RunnerError(
            "this job was already started; inspect its state instead of starting it again"
        ) from exc
    except OSError as exc:
        raise RunnerError(f"could not claim this job's launch slot: {_safe_text(exc)}") from exc
    try:
        os.write(descriptor, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_text(value: object) -> str:
    text = str(value)
    for pattern in (r"https?://[^\s]*", r"(?i)\b(x-amz-signature|authorization)\b[^\s]*"):
        text = re.sub(pattern, "<redacted>", text)
    return " ".join(text.split())[:300]


def _environment_for_git() -> dict[str, str]:
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_ASKPASS"] = "/bin/true"
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    environment["GCM_INTERACTIVE"] = "never"
    return environment


def _git_executable() -> str:
    executable = os.environ.get("WAVCSE_JOB_GIT") or shutil.which("git")
    if executable is None:
        raise RunnerError("git is not available on this worker; run `infra worker bootstrap`")
    return executable


def _git(
    arguments: list[str],
    *,
    cwd: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one git argv without a shell and with interactive prompts disabled."""

    command = [_git_executable(), "-c", "advice.detachedHead=false", *arguments]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=_environment_for_git(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"git {arguments[0]} exceeded {GIT_TIMEOUT_SECONDS:g} seconds") from exc
    except OSError as exc:
        raise RunnerError(
            f"could not run git {arguments[0]} in {cwd or 'the current directory'}: "
            f"{_safe_text(exc)}"
        ) from exc
    if check and completed.returncode != 0:
        detail = _safe_text(completed.stderr.strip() or completed.stdout.strip() or "no detail")
        raise RunnerError(f"git {arguments[0]} failed (exit {completed.returncode}): {detail}")
    return completed


def _git_succeeds(arguments: list[str], *, cwd: str | None = None) -> bool:
    return _git(arguments, cwd=cwd, check=False).returncode == 0


def _validate_repository(repository: str) -> str:
    """Refuse anything that is not an anonymous HTTPS remote."""

    normalized = repository.strip()
    if normalized != repository or not normalized.startswith("https://"):
        raise RunnerInputError(
            "source repository must be an anonymous https:// URL; SSH remotes and "
            "credential-bearing URLs are never used on a worker"
        )
    authority = normalized[len("https://") :].split("/", maxsplit=1)[0]
    if not authority or "@" in authority:
        raise RunnerInputError("source repository must not embed credentials")
    if "#" in normalized or "?" in normalized:
        raise RunnerInputError("source repository must not contain a query string or fragment")
    return normalized


def _validate_commit(commit: str) -> str:
    normalized = commit.strip().lower()
    if not COMMIT_PATTERN.fullmatch(normalized):
        raise RunnerInputError("source commit must be a full hexadecimal commit ID")
    return normalized


def _clone_source(repository: str, source_directory: str) -> None:
    """Clone without a checkout, falling back to a plain clone when filters are unsupported."""

    if _git_succeeds(
        [
            "clone",
            "--no-checkout",
            "--no-tags",
            "--depth",
            "1",
            "--filter=blob:none",
            repository,
            source_directory,
        ]
    ):
        return
    if os.path.exists(source_directory):
        shutil.rmtree(source_directory)
    _git(["clone", "--no-checkout", "--no-tags", "--depth", "1", repository, source_directory])


def _fetch_commit(commit: str, source_directory: str) -> None:
    if _git_succeeds(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=source_directory):
        return
    if _git_succeeds(["fetch", "--depth", "1", "origin", commit], cwd=source_directory):
        return
    # Some servers refuse a fetch that names an arbitrary object ID; fall back to a plain
    # fetch of the remote's refs, then require the exact object to exist locally.
    fallback = _git(["fetch", "origin"], cwd=source_directory, check=False)
    if not _git_succeeds(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=source_directory):
        detail = _safe_text(fallback.stderr.strip() or fallback.stdout.strip())
        raise RunnerError(
            f"the remote does not contain commit {commit}"
            + (
                f"; the fallback fetch exited {fallback.returncode} and reported: {detail}"
                if fallback.returncode != 0 and detail
                else ""
            )
            + "; push the exact commit to GitHub before submitting a recorded job"
        )


def materialize_source(repository: str, commit: str, source_directory: str) -> str:
    """Check out exactly one commit and return the verified object ID of `HEAD`."""

    remote = _validate_repository(repository)
    requested = _validate_commit(commit)
    if os.path.isdir(os.path.join(source_directory, ".git")):
        _git(["remote", "set-url", "origin", remote], cwd=source_directory)
    else:
        _clone_source(remote, source_directory)
    _fetch_commit(requested, source_directory)
    _git(["checkout", "--detach", "--force", requested], cwd=source_directory)
    head = _git(["rev-parse", "HEAD"], cwd=source_directory).stdout.strip().lower()
    if head != requested:
        raise RunnerError(
            f"checked out HEAD {head or 'unknown'} does not match the requested commit "
            f"{requested}; refusing to execute a different revision"
        )
    status = _git(["status", "--porcelain"], cwd=source_directory).stdout.strip()
    if status:
        raise RunnerError(
            "the source checkout is not clean after checkout; recorded jobs never "
            "execute a dirty working tree"
        )
    return head


def _verify_bootstrap(descriptor: dict[str, Any]) -> str:
    expected = descriptor.get("expected_bootstrap_version")
    if not isinstance(expected, str) or not expected:
        raise RunnerInputError("descriptor expected_bootstrap_version is required")
    marker = os.path.expanduser(BOOTSTRAP_MARKER)
    observed = None
    try:
        with open(marker, encoding="utf-8") as handle:
            observed = handle.read().strip()
    except OSError:
        observed = None
    if observed != expected:
        raise RunnerError(
            f"worker bootstrap marker is {observed or 'missing'}, but version {expected} is "
            "required; run `infra worker bootstrap <worker-id>` and retry"
        )
    return expected


def prepare(descriptor: dict[str, Any]) -> None:
    """Create the isolated workspace and check out the exact requested commit."""

    job_id = _job_id(descriptor)
    job_directory = _absolute_job_directory(descriptor)
    _verify_bootstrap(descriptor)
    source = descriptor.get("source")
    if not isinstance(source, dict):
        raise RunnerInputError("descriptor source is required")
    repository = _validate_repository(_require_text(source, "repository"))
    requested = _validate_commit(_require_text(source, "commit"))
    paths = _job_paths(job_directory)
    _ensure_directories(paths)
    _create_input_directories(paths, descriptor.get("input_destinations"))
    # Serialize prepare per job so a repeated phase can never race an in-flight checkout.
    prepare_lock = _acquire_prepare_lock(paths)
    try:
        executed = materialize_source(repository, requested, paths["source"])
        _atomic_write_json(
            paths["info"],
            {
                "schema_version": 1,
                "job_id": job_id,
                "name": descriptor.get("name"),
                "repository": repository,
                "requested_commit": requested,
                "executed_commit": executed,
                "prepared_at": _utc_now(),
            },
        )
    finally:
        os.close(prepare_lock)
    emit("executed_commit", executed)
    emit("job_directory", job_directory)
    emit("source_directory", paths["source"])


def _create_input_directories(paths: dict[str, str], destinations: object) -> None:
    """Create the parent directory of every declared input destination."""

    if destinations is None:
        return
    if not isinstance(destinations, list) or not all(
        isinstance(item, str) for item in destinations
    ):
        raise RunnerInputError("descriptor input_destinations must be a list of relative paths")
    for destination in destinations:
        resolved = resolve_within(paths["inputs"], destination)
        parent = os.path.dirname(resolved)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)


def _non_secret_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Return the descriptor fields that are safe to persist on the worker."""

    return {
        "job_id": _job_id(descriptor),
        "job_directory": _absolute_job_directory(descriptor),
        "source": descriptor.get("source"),
        "command": descriptor.get("command"),
        "setup_argv": descriptor.get("setup_argv"),
        "environment": descriptor.get("environment") or {},
        "secret_names": sorted((descriptor.get("secrets") or {}).keys()),
        "timeout_seconds": descriptor.get("timeout_seconds"),
        "infra": descriptor.get("infra") or {},
        "started_at": _utc_now(),
    }


def _execution_environment(descriptor: dict[str, Any]) -> dict[str, str]:
    """Build the deterministic job environment from an explicit baseline and declarations."""

    environment: dict[str, str] = dict(BASELINE_ENVIRONMENT)
    home = os.environ.get("HOME")
    if home:
        environment["HOME"] = home
    else:
        environment["HOME"] = os.path.expanduser("~")
    for variable, value in (descriptor.get("environment") or {}).items():
        if not isinstance(variable, str) or not isinstance(value, str):
            raise RunnerInputError("descriptor environment must map names to strings")
        environment[variable] = value
    secrets = descriptor.get("secrets") or {}
    if not isinstance(secrets, dict):
        raise RunnerInputError("descriptor secrets must be an object")
    for variable, value in secrets.items():
        if not isinstance(variable, str) or not isinstance(value, str) or not value:
            raise RunnerInputError("descriptor secrets must map names to non-empty strings")
        environment[variable] = value
    infra = descriptor.get("infra") or {}
    if not isinstance(infra, dict):
        raise RunnerInputError("descriptor infra must be an object")
    for variable, value in infra.items():
        if not isinstance(variable, str) or not isinstance(value, str):
            raise RunnerInputError("descriptor infra must map names to strings")
        environment[variable] = value
    return environment


def start(descriptor: dict[str, Any]) -> None:
    """Verify provenance again, then launch the job detached in its own session."""

    job_id = _job_id(descriptor)
    job_directory = _absolute_job_directory(descriptor)
    paths = _job_paths(job_directory)
    if not os.path.isdir(paths["state"]):
        raise RunnerError("job workspace is missing; run the prepare phase first")
    source = descriptor.get("source")
    if not isinstance(source, dict):
        raise RunnerInputError("descriptor source is required")
    repository = _validate_repository(_require_text(source, "repository"))
    requested = _validate_commit(_require_text(source, "commit"))
    info = _read_json(paths["info"])
    if (
        info is None
        or info.get("job_id") != job_id
        or info.get("repository") != repository
        or info.get("requested_commit") != requested
    ):
        raise RunnerError("prepared job identity or source does not match this start request")
    command = descriptor.get("command")
    if not isinstance(command, dict):
        raise RunnerInputError("descriptor command is required")
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise RunnerInputError("descriptor command.argv must be a non-empty argument list")
    setup_argv = descriptor.get("setup_argv") or None
    if setup_argv is not None and (
        not isinstance(setup_argv, list)
        or not setup_argv
        or not all(isinstance(item, str) for item in setup_argv)
    ):
        raise RunnerInputError("descriptor setup_argv must be a non-empty argument list")
    timeout = descriptor.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
        raise RunnerInputError("descriptor timeout_seconds must be a positive integer")

    # Phase one: claim the launch slot before the checkout, so a repeated start cannot
    # force-checkout the source tree of a command that is already running, and a
    # cancellation that has already been established excludes this launch outright.
    lock = _acquire_lifecycle_lock(paths)
    if lock is None:
        raise RunnerError(
            f"another lifecycle operation for this job {LIFECYCLE_BUSY_MARKER}; retry"
        )
    try:
        if os.path.lexists(paths["cancelled"]):
            raise RunnerError(f"this job {CANCELLED_BEFORE_START_MARKER}; it will not be launched")
        _claim_start_lock(paths)
    finally:
        os.close(lock)

    executed = materialize_source(repository, requested, paths["source"])
    working_directory = paths["source"]
    relative = command.get("working_directory")
    if relative is not None:
        if not isinstance(relative, str):
            raise RunnerInputError("descriptor command.working_directory must be a string")
        working_directory = resolve_within(paths["source"], relative)
        if not os.path.isdir(working_directory):
            raise RunnerError(
                f"the requested working directory does not exist in the checkout: {relative!r}"
            )
        source_real = os.path.realpath(paths["source"])
        working_real = os.path.realpath(working_directory)
        if not working_real.startswith(source_real + os.sep):
            raise RunnerError(
                f"the requested working directory resolves outside the checkout: {relative!r}"
            )

    environment = _execution_environment(descriptor)
    environment.update(
        {
            "WAVCSE_JOB_ID": job_id,
            "WAVCSE_JOB_COMMIT": executed,
            "WAVCSE_JOB_DIRECTORY": job_directory,
            "WAVCSE_JOB_LOG": paths["log"],
            "WAVCSE_JOB_GIT": _git_executable(),
        }
    )

    # Phase two: re-check the cancellation decision under the lock and create the process
    # inside the same critical section, so a cancellation can only ever observe "no launch
    # yet, and this one is now excluded" or "the launch already happened".
    lock = _acquire_lifecycle_lock(paths)
    if lock is None:
        raise RunnerError(
            f"another lifecycle operation for this job {LIFECYCLE_BUSY_MARKER}; retry"
        )
    try:
        if os.path.lexists(paths["cancelled"]):
            raise RunnerError(f"this job {CANCELLED_BEFORE_START_MARKER}; no process was created")
        _atomic_write_json(paths["descriptor"], _non_secret_descriptor(descriptor))
        log_descriptor = os.open(paths["log"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            process = subprocess.Popen(
                [
                    sys.executable or "python3",
                    os.path.abspath(__file__),
                    "__supervise__",
                    job_directory,
                ],
                cwd=working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_descriptor,
                stderr=log_descriptor,
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(log_descriptor)
        started_at = _utc_now()
        _atomic_write_json(
            paths["pid"],
            {
                "supervisor_pid": process.pid,
                "supervisor_starttime": _process_starttime(process.pid),
                "child_pid": None,
                "child_running": False,
                "started_at": started_at,
                "executed_commit": executed,
            },
        )
    finally:
        os.close(lock)
    emit("pid", process.pid)
    emit("started_at", started_at)
    emit("executed_commit", executed)


def _process_command_line(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            payload = handle.read()
    except OSError:
        return None
    return [part.decode("utf-8", "replace") for part in payload.split(b"\0") if part]


def _process_starttime(pid: int) -> int | None:
    """Read Linux process start ticks, which distinguish reuse of the same PID."""

    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[1].split()
        return int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _same_process(pid: int, starttime: object) -> bool:
    return isinstance(starttime, int) and _process_starttime(pid) == starttime


def _is_our_supervisor(pid: int, job_directory: str, starttime: object) -> bool:
    if not _same_process(pid, starttime):
        return False
    command_line = _process_command_line(pid)
    if not command_line:
        return False
    return (
        os.path.basename(command_line[0]).startswith("python")
        and "__supervise__" in command_line
        and job_directory in command_line
    )


def _is_our_child(pid: int, starttime: object) -> bool:
    if not _same_process(pid, starttime):
        return False
    try:
        if os.getpgid(pid) != pid:
            return False
    except OSError:
        return False
    # An executable may replace itself with another image (for example `uv run`),
    # so its command line is not stable. The start ticks and private process group
    # recorded immediately after Popen identify the intended child instead.
    return True


def _process_alive(pid: int) -> bool:
    """Return whether a PID is a live, non-zombie process.

    An exited child stays visible to `kill(pid, 0)` until its parent reaps it, so the
    process state is inspected as well; otherwise a bounded shutdown would always wait
    out its whole grace period.
    """

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            state = handle.read().split()[2]
    except (OSError, IndexError):
        return True
    return state != b"Z"


def _signal_group(pid: int, number: int) -> bool:
    try:
        os.killpg(pid, number)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise RunnerError(f"not permitted to signal job process group {pid}") from exc
    return True


def _group_alive(pid: int) -> bool:
    """Look for a live member, including descendants after the leader has exited."""

    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
            if int(fields[2]) == pid and fields[0] != b"Z":
                return True
        except (OSError, IndexError, ValueError):
            continue
    return False


def terminate_group(
    pid: int, *, expected_starttime: int | None, grace: float = KILL_GRACE_SECONDS
) -> None:
    """Pin the leader's PID identity while terminating its private process group."""

    if expected_starttime is None:
        raise RunnerError(f"cannot verify process group {pid} before signalling")
    try:
        pidfd = os.pidfd_open(pid)
    except OSError as exc:
        raise RunnerError(f"cannot pin process group {pid} before signalling") from exc
    try:
        try:
            same_group = os.getpgid(pid) == pid
        except ProcessLookupError:
            same_group = False
        if not _same_process(pid, expected_starttime) or not same_group:
            raise RunnerError(f"process group {pid} changed identity before signalling")
        if not _signal_group(pid, signal.SIGTERM):
            return
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not _group_alive(pid):
                return
            time.sleep(POLL_SECONDS)
        if _group_alive(pid):
            _signal_group(pid, signal.SIGKILL)
    finally:
        os.close(pidfd)


def terminate_job_group(
    pgid: int,
    *,
    expected_starttime: int | None,
    grace: float = KILL_GRACE_SECONDS,
) -> bool:
    """Terminate one job stage's process group, verified against PID reuse.

    The group id is the stage leader's PID. While any member of the group is alive the
    kernel keeps that number allocated to the group, so a live member is proof that the
    group is this job's - and if the number has instead been reused by a different process
    (different start ticks), nothing at all is signalled.

    The recorded start ticks are required. If they are missing, the recorded number cannot
    be shown to be this job's group at all, and signalling it could kill an unrelated
    workload; refusing is the only safe answer, because a job that survives is recoverable
    and an unrelated process that is killed is not.
    """

    if expected_starttime is None:
        raise RunnerError(
            f"refusing to signal process group {pgid}: its recorded identity cannot be "
            "verified, so it may not be this job's group; nothing was signalled"
        )
    observed = _process_starttime(pgid)
    if observed is not None and observed != expected_starttime:
        raise RunnerError(
            f"process group {pgid} was reused by an unrelated process; nothing was signalled"
        )
    if observed is not None:
        # The leader is still alive: use the fully pinned path.
        terminate_group(pgid, expected_starttime=expected_starttime, grace=grace)
        return True
    if not _group_alive(pgid):
        return False
    if not _signal_group(pgid, signal.SIGTERM):
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(POLL_SECONDS)
    if _group_alive(pgid):
        _signal_group(pgid, signal.SIGKILL)
    return True


def _run_stage(
    argv: list[str],
    *,
    cwd: str,
    timeout: int | None,
) -> tuple[int, bool, int | None, int | None]:
    """Run one argv stage in its own session; return exit code, timeout, PID, start ticks."""

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=os.environ,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    starttime = _process_starttime(process.pid)
    _record_child_pid(process.pid, running=True)
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group(process.pid, expected_starttime=starttime)
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        exit_code = TIMEOUT_EXIT_CODE
    _record_child_pid(process.pid, running=False)
    return exit_code, timed_out, process.pid, starttime


def _state_paths_from_environment() -> dict[str, str]:
    job_directory = os.environ.get("WAVCSE_JOB_DIRECTORY")
    if not job_directory:
        raise RunnerError("WAVCSE_JOB_DIRECTORY is missing from the job environment")
    return _job_paths(job_directory)


def _record_child_pid(child_pid: int | None, *, running: bool) -> None:
    """Publish the current stage's process group so cancel and inspection can target it.

    The last stage's identity is kept after the stage exits, because a process group
    outlives its leader: a descendant that is still running is still this job's process,
    and its group id is what makes terminating it possible without guessing.

    The recorded start ticks are the proof of that identity, so they are never replaced
    with "unknown" for the same leader: a leader that has already exited has no start ticks
    to read any more, and overwriting a verified identity with nothing would make a genuine
    surviving descendant indistinguishable from an unrelated process group. A different
    leader - the next stage - has its own identity, so its start ticks are recorded afresh.
    """

    paths = _state_paths_from_environment()
    payload = _read_json(paths["pid"]) or {}
    if child_pid is not None:
        previous = payload.get("child_pid")
        payload["child_pid"] = child_pid
        observed = _process_starttime(child_pid)
        if observed is not None or previous != child_pid:
            payload["child_starttime"] = observed
    payload["child_running"] = running
    _atomic_write_json(paths["pid"], payload)


def _checkout_verified(requested: str) -> bool:
    observed = _git(["rev-parse", "HEAD"], cwd=os.getcwd(), check=False)
    clean = _git(["status", "--porcelain"], cwd=os.getcwd(), check=False)
    return (
        observed.returncode == 0
        and observed.stdout.strip().lower() == requested
        and clean.returncode == 0
        and not clean.stdout.strip()
    )


def supervise(job_directory: str) -> int:
    """Detached supervisor: run setup, then the command, then record the outcome."""

    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    paths = _job_paths(job_directory)
    # The launcher records our PID before we publish a child PID. Otherwise a fast
    # child can write pid.json first and the launcher can overwrite its identity.
    for _ in range(40):
        initial_pid = _read_json(paths["pid"])
        if initial_pid and initial_pid.get("supervisor_pid") == os.getpid():
            break
        time.sleep(POLL_SECONDS)
    else:
        sys.stderr.write("launcher did not publish the supervisor identity\n")
        return 1
    descriptor = _read_json(paths["descriptor"])
    if descriptor is None:
        sys.stderr.write("job descriptor is missing\n")
        return 1
    command = descriptor.get("command") or {}
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv:
        sys.stderr.write("job descriptor has no command\n")
        return 1
    setup_argv = descriptor.get("setup_argv") or None
    timeout = descriptor.get("timeout_seconds")
    infra = descriptor.get("infra") if isinstance(descriptor.get("infra"), dict) else {}
    requested = _validate_commit(_require_text(descriptor["source"], "commit"))
    commit = os.environ.get("WAVCSE_JOB_COMMIT") or infra.get("INFRA_GIT_COMMIT") or "unknown"
    started_at = _utc_now()
    sys.stdout.write(
        f"==== wavcse job {descriptor.get('job_id')} started {started_at} (commit {commit}) ====\n"
    )
    sys.stdout.flush()

    stage = STAGE_COMMAND
    exit_code = 0
    timed_out = False
    # The source was verified during prepare and start, but either SSH phase can be
    # separated from this actual process launch by a checkout change. Prove HEAD and
    # cleanliness immediately before the first user supplied argv is executed.
    if commit != requested or not _checkout_verified(requested):
        stage = STAGE_STARTUP
        exit_code = 1
        sys.stdout.write("source HEAD or cleanliness changed before execution; refusing job\n")
        sys.stdout.flush()
    stages: list[tuple[int, int | None]] = []
    if exit_code == 0 and setup_argv:
        stage = STAGE_SETUP
        sys.stdout.write("---- setup ----\n")
        sys.stdout.flush()
        exit_code, timed_out, child_pid, child_starttime = _run_stage(
            setup_argv, cwd=os.getcwd(), timeout=timeout
        )
        if child_pid is not None:
            stages.append((child_pid, child_starttime))
    if exit_code == 0:
        stage = STAGE_COMMAND
        if not _checkout_verified(requested):
            exit_code = 1
            sys.stdout.write("source HEAD or cleanliness changed during setup; refusing command\n")
            sys.stdout.flush()
        else:
            sys.stdout.write("---- command ----\n")
            sys.stdout.flush()
            exit_code, timed_out, child_pid, child_starttime = _run_stage(
                argv, cwd=os.getcwd(), timeout=timeout
            )
            if child_pid is not None:
                stages.append((child_pid, child_starttime))

    # A command that left background processes behind has not finished: a terminal record
    # means nothing of this job is still running, so survivors are terminated before the
    # outcome is written. Their exit does not change the command's own exit status.
    surviving: list[int] = []
    for group_pid, group_starttime in stages:
        if not _group_alive(group_pid):
            continue
        sys.stdout.write(f"---- terminating processes that outlived stage group {group_pid} ----\n")
        sys.stdout.flush()
        try:
            terminate_job_group(group_pid, expected_starttime=group_starttime)
        except RunnerError as exc:
            sys.stdout.write(f"could not terminate a surviving process group: {exc}\n")
            sys.stdout.flush()
        if _group_alive(group_pid):
            surviving.append(group_pid)

    if surviving:
        # The bounded termination policy is exhausted and this job's own group is still
        # alive. Writing finished.json now would claim a clean terminal outcome while the
        # job is still executing, so no outcome is recorded: the job stays nonterminal and
        # a later inspection reports the live group for reconciliation.
        sys.stdout.write(
            "refusing to record a terminal outcome while this job's own process groups "
            f"{surviving} are still alive; the job remains running for reconciliation\n"
        )
        sys.stdout.flush()
        return 1

    finished_at = _utc_now()
    sys.stdout.write(
        f"==== wavcse job finished {finished_at} (exit {exit_code}"
        + (", timed out" if timed_out else "")
        + f", stage {stage}) ====\n"
    )
    sys.stdout.flush()
    _atomic_write_json(
        paths["finished"],
        {
            "exit_code": exit_code,
            "stage": stage,
            "timed_out": timed_out,
            "started_at": started_at,
            "finished_at": finished_at,
            "executed_commit": requested if stage != STAGE_STARTUP else None,
        },
    )
    return 0


def _read_evidence(paths: dict[str, str]) -> dict[str, Any]:
    """Read the job's decision-relevant state files as one labelled group."""

    return {
        "finished": _read_json(paths["finished"]),
        "cancelled": _read_json(paths["cancelled"]),
        "pid": _read_json(paths["pid"]) or {},
        "start": _read_optional_json(os.path.join(paths["state"], "start.lock")),
    }


def _child_alive(pid_state: dict[str, Any]) -> bool:
    """Return whether the recorded stage leader itself is still running.

    An exited process stays visible in `/proc` until it is reaped, so its state is checked
    as well: a zombie holds no execution, and reporting one as live would leave a job that
    can never finish looking like it is still running.
    """

    child_pid = pid_state.get("child_pid")
    if not isinstance(child_pid, int):
        return False
    running = pid_state.get("child_running")
    if running is None:
        # A record written before this field existed only carries a live leader.
        running = True
    return (
        bool(running)
        and _same_process(child_pid, pid_state.get("child_starttime"))
        and _process_alive(child_pid)
    )


def _process_identity_incomplete(pid_state: dict[str, Any]) -> bool:
    """Whether a recorded child PID lacks the start ticks that identify it.

    A record written before start ticks were published cannot prove that the process it
    names is still that process, so neither cancelling it nor declaring that nothing is
    running is safe from it.
    """

    child_pid = pid_state.get("child_pid")
    return isinstance(child_pid, int) and not isinstance(pid_state.get("child_starttime"), int)


def _live_job_group(pid_state: dict[str, Any]) -> int | None:
    """Return the job's own process group when a member of it is still alive.

    The group id is the recorded stage leader's PID. A live member proves the group is
    this job's, because the kernel keeps that number allocated to the group for as long as
    any member exists; a reused number with different start ticks is never reported.

    The recorded start ticks are required, not optional. Without them a numeric group
    cannot be distinguished from an unrelated process group that happens to carry the same
    id, so the answer is "no group I can prove is this job's" rather than a guess. That
    only ever makes the runner more conservative: an unprovable group is reported as
    unknown, and it is never signalled.
    """

    child_pid = pid_state.get("child_pid")
    if not isinstance(child_pid, int) or child_pid <= 0:
        return None
    recorded = pid_state.get("child_starttime")
    if not isinstance(recorded, int):
        return None
    observed = _process_starttime(child_pid)
    if observed is not None and observed != recorded:
        return None
    return child_pid if _group_alive(child_pid) else None


def inspect(descriptor: dict[str, Any]) -> None:
    """Report job status from the worker's own recorded evidence.

    The state files and the process table change independently, so the report is derived
    from an ordered, bounded snapshot: the outcome is read first, the process state is
    observed next, and anything that appeared while the process state was being observed is
    re-read before a "nothing is recorded and nothing is running" conclusion is reported.
    A conclusion of that kind is never reached from one inconsistent read, because the
    controller treats it as terminal evidence.
    """

    job_directory = _absolute_job_directory(descriptor)
    paths = _job_paths(job_directory)

    supervisor_pid: Any = None
    supervisor_alive = False
    child_alive = False
    group_pid = _live_job_group(_read_json(paths["pid"]) or {})
    status = STATUS_UNKNOWN
    evidence: dict[str, Any] = {}
    for _pass in range(2):
        evidence = _read_evidence(paths)
        finished = evidence["finished"]
        cancelled = evidence["cancelled"]
        if finished is not None:
            status = STATUS_FINISHED
            break
        if cancelled is not None:
            status = STATUS_CANCELLED
            break
        pid_state = evidence["pid"]
        supervisor_pid = pid_state.get("supervisor_pid")
        supervisor_alive = (
            isinstance(supervisor_pid, int)
            and _same_process(supervisor_pid, pid_state.get("supervisor_starttime"))
            and _process_alive(supervisor_pid)
        )
        child_alive = _child_alive(pid_state)
        if supervisor_alive or child_alive:
            status = STATUS_RUNNING
            break
        group_pid = _live_job_group(pid_state)
        if group_pid is not None:
            # A descendant that outlived its leader is still this job's execution.
            status = STATUS_RUNNING
            break
        # Nothing is running and nothing terminal is recorded. Re-read the evidence before
        # the controller may treat that as the final word: an outcome or launch record
        # written while the process state was observed is newer than what was read first.
        if _read_evidence(paths) == evidence:
            status = STATUS_UNKNOWN
            break
    else:
        status = STATUS_UNKNOWN

    finished = evidence.get("finished")
    cancelled = evidence.get("cancelled")
    pid_state = evidence.get("pid") or {}

    # Preparation evidence: which parts of the workspace exist and whether a launch is
    # still in progress. Reported separately from `status` so a controller can tell "the
    # command never started" apart from "the workspace is gone".
    job_directory_exists = os.path.isdir(job_directory)
    info = _read_json(paths["info"])
    prepared = (
        job_directory_exists
        and isinstance(info, dict)
        and info.get("job_id") == _job_id(descriptor)
    )
    start_lock = evidence.get("start") or {}
    started = os.path.lexists(os.path.join(paths["state"], "start.lock"))
    launch_alive: bool | None = None
    if started and not isinstance(supervisor_pid, int):
        launch_pid = start_lock.get("pid")
        launch_starttime = start_lock.get("starttime")
        if isinstance(launch_pid, int) and isinstance(launch_starttime, int):
            launch_alive = _same_process(launch_pid, launch_starttime)

    log_bytes = 0
    try:
        log_bytes = os.stat(paths["log"]).st_size
    except OSError:
        log_bytes = 0

    # Never rewrite execution provenance from the checkout's *current* HEAD. The
    # checkout can change after the process has exited.
    executed = (finished or {}).get("executed_commit")
    if executed is None and status in {STATUS_RUNNING, STATUS_CANCELLED}:
        executed = pid_state.get("executed_commit")

    emit("status", status)
    emit("pid", supervisor_pid if isinstance(supervisor_pid, int) else "")
    emit("exit_code", finished.get("exit_code") if finished else "")
    emit("stage", finished.get("stage") if finished else "")
    emit("timed_out", "true" if finished and finished.get("timed_out") else "false")
    emit("cancelled", "true" if cancelled is not None else "false")
    emit(
        "pre_start",
        "true" if cancelled is not None and cancelled.get("pre_start") is True else "false",
    )
    emit("started_at", (finished or cancelled or pid_state).get("started_at") or "")
    emit("finished_at", (finished or cancelled or {}).get("finished_at") or "")
    emit("executed_commit", executed or "")
    emit("log_bytes", log_bytes)
    emit("job_directory_exists", "true" if job_directory_exists else "false")
    emit("prepared", "true" if prepared else "false")
    emit("started", "true" if started else "false")
    if launch_alive is not None:
        emit("launch_alive", "true" if launch_alive else "false")
    if group_pid is not None:
        emit("group_pid", group_pid)


def logs(descriptor: dict[str, Any]) -> None:
    """Write a bounded tail of the job log to stdout without any protocol wrapper."""

    paths = _job_paths(_absolute_job_directory(descriptor))
    tail = descriptor.get("tail_bytes")
    if tail is None:
        tail = MAX_TAIL_BYTES
    if not isinstance(tail, int) or tail < 0 or tail > MAX_TAIL_BYTES:
        raise RunnerInputError(f"tail_bytes must be between 0 and {MAX_TAIL_BYTES}")
    if tail == 0:
        return
    try:
        size = os.stat(paths["log"]).st_size
    except FileNotFoundError:
        return
    with open(paths["log"], "rb") as handle:
        if tail > 0:
            handle.seek(max(0, size - tail))
        payload = handle.read()
    sys.stdout.write(payload.decode("utf-8", "replace"))


def cancel(descriptor: dict[str, Any]) -> None:
    """Terminate the job's own process tree, or make a later launch impossible.

    Creating the job's process and cancelling a job that has no process yet are the same
    decision made from two sides, so both take the per-job lifecycle lock. Whichever takes
    it first wins outright: a cancellation that finds no launch slot records a durable
    cancellation (which every later launch refuses), and a launch that has already created
    its process is instead terminated through the identity it recorded. Neither outcome can
    leave a command running after the controller recorded CANCELLED.
    """

    job_directory = _absolute_job_directory(descriptor)
    paths = _job_paths(job_directory)
    if not os.path.isdir(paths["state"]):
        # Nothing was ever prepared here, so nothing can be launched: a launch requires the
        # workspace this job would have created.
        _emit_pre_start_cancellation()
        return
    lock = _acquire_lifecycle_lock(paths)
    if lock is None:
        raise RunnerError(
            f"another lifecycle operation for this job {LIFECYCLE_BUSY_MARKER}; retry"
        )
    try:
        _cancel_locked(descriptor, paths, job_directory)
    finally:
        os.close(lock)


def _emit_pre_start_cancellation() -> None:
    emit("cancelled", "true")
    emit("already_finished", "false")
    emit("pre_start", "true")
    emit("pid", "")


def _cancel_locked(
    descriptor: dict[str, Any],
    paths: dict[str, str],
    job_directory: str,
) -> None:
    """Decide and apply one cancellation while holding the lifecycle lock."""

    pid_state = _read_json(paths["pid"]) or {}
    finished = _read_json(paths["finished"])
    supervisor_pid = pid_state.get("supervisor_pid")
    child_pid = pid_state.get("child_pid")
    supervisor_alive = (
        isinstance(supervisor_pid, int)
        and _same_process(supervisor_pid, pid_state.get("supervisor_starttime"))
        and _process_alive(supervisor_pid)
    )
    child_alive = _child_alive(pid_state)
    group_pid = _live_job_group(pid_state)
    start_lock = _read_optional_json(os.path.join(paths["state"], "start.lock"))
    started = os.path.lexists(os.path.join(paths["state"], "start.lock"))

    if finished is not None and group_pid is None:
        # The outcome is recorded and nothing of this job's process tree is left: the
        # supervisor may still be winding down, which is not something to cancel.
        emit("cancelled", "false")
        emit("already_finished", "true")
        emit("exit_code", finished.get("exit_code"))
        emit("pid", "")
        return

    if _read_json(paths["cancelled"]) is not None:
        emit("cancelled", "true")
        emit("already_finished", "false")
        emit("pre_start", "false")
        emit("pid", supervisor_pid if isinstance(supervisor_pid, int) else "")
        return

    if not started and not child_alive and group_pid is None and not supervisor_alive:
        # No launch slot was ever claimed and nothing is running: cancelling here is what
        # makes every later launch refuse.
        _record_pre_start_cancellation(paths)
        return

    if started and not supervisor_alive and not child_alive and group_pid is None:
        if _process_identity_incomplete(pid_state):
            # A child PID is recorded without the start ticks that would prove whether it
            # is still that process. Recording a pre-start cancellation here would claim
            # that no command ran while a recorded process might still be running, so the
            # cancellation is reported as unestablished instead of guessed.
            raise RunnerError(
                f"refusing to cancel job at {paths['pid']}: its recorded process identity "
                "cannot be verified, so whether a command is running is unknown"
            )
        launch_pid = start_lock.get("pid")
        launch_starttime = start_lock.get("starttime")
        if (
            isinstance(launch_pid, int)
            and isinstance(launch_starttime, int)
            and _same_process(launch_pid, launch_starttime)
        ):
            # The launch is in flight and will create the process itself; the operator
            # retries cancellation against the launched process instead of racing it.
            emit("cancelled", "false")
            emit("already_finished", "false")
            emit("launch_in_progress", "true")
            emit("pid", "")
            return
        # The launch is gone and never recorded a process, so it can never run one.
        _record_pre_start_cancellation(paths)
        return

    if child_alive:
        if not _is_our_child(child_pid, pid_state.get("child_starttime")):
            raise RunnerError(
                f"refusing to signal process {child_pid}: it does not match this job's "
                "recorded command"
            )
        terminate_group(child_pid, expected_starttime=pid_state.get("child_starttime"))
    elif group_pid is not None:
        # The stage leader is gone but a descendant of the job's own process group is not.
        terminate_job_group(group_pid, expected_starttime=pid_state.get("child_starttime"))
    if (
        supervisor_alive
        and _same_process(supervisor_pid, pid_state.get("supervisor_starttime"))
        and _process_alive(supervisor_pid)
    ):
        try:
            supervisor_fd = os.pidfd_open(supervisor_pid)
        except ProcessLookupError:
            supervisor_fd = None
        if supervisor_fd is not None:
            try:
                if not _is_our_supervisor(
                    supervisor_pid, job_directory, pid_state.get("supervisor_starttime")
                ):
                    raise RunnerError(
                        f"refusing to signal process {supervisor_pid}: it does not match "
                        "this job's recorded supervisor"
                    )
                if not child_alive and group_pid is None:
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(supervisor_fd, signal.SIGTERM)
                    deadline = time.monotonic() + KILL_GRACE_SECONDS
                    while time.monotonic() < deadline and _process_alive(supervisor_pid):
                        time.sleep(POLL_SECONDS)
                    if _process_alive(supervisor_pid):
                        signal.pidfd_send_signal(supervisor_fd, signal.SIGKILL)
            finally:
                os.close(supervisor_fd)

    cancelled_at = _utc_now()
    _atomic_write_json(
        paths["cancelled"],
        {"cancelled_at": cancelled_at, "exit_code": CANCELLED_EXIT_CODE, "pre_start": False},
    )
    emit("cancelled", "true")
    emit("already_finished", "false")
    emit("pre_start", "false")
    emit("pid", supervisor_pid if isinstance(supervisor_pid, int) else "")
    emit("cancelled_at", cancelled_at)


def _record_pre_start_cancellation(paths: dict[str, str]) -> None:
    """Record the durable decision that this job must never be launched."""

    cancelled_at = _utc_now()
    _atomic_write_json(
        paths["cancelled"],
        {"cancelled_at": cancelled_at, "exit_code": CANCELLED_EXIT_CODE, "pre_start": True},
    )
    emit("cancelled", "true")
    emit("already_finished", "false")
    emit("pre_start", "true")
    emit("pid", "")
    emit("cancelled_at", cancelled_at)


def main(argv: list[str] | None = None) -> int:
    """Dispatch one worker-side job phase."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        sys.stderr.write(f"{ERROR_KEY}\tno subcommand supplied\n")
        return 2
    subcommand = arguments[0]
    try:
        if subcommand == "__supervise__":
            if len(arguments) != 2:
                raise RunnerInputError("__supervise__ requires an absolute job directory")
            return supervise(arguments[1])
        descriptor = _read_descriptor()
        if subcommand == "logs":
            # Raw job output: no protocol framing, so the controller can stream it verbatim.
            logs(descriptor)
            return 0
        sys.stdout.write(f"{SCHEMA_KEY}\t{SCHEMA_VERSION}\n")
        if subcommand == "prepare":
            prepare(descriptor)
        elif subcommand == "start":
            start(descriptor)
        elif subcommand == "inspect":
            inspect(descriptor)
        elif subcommand == "cancel":
            cancel(descriptor)
        else:
            raise RunnerInputError(f"unknown subcommand: {subcommand!r}")
    except RunnerError as exc:
        sys.stderr.write(f"{ERROR_KEY}\t{_safe_text(exc)}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
