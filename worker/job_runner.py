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
      state/     pid, finished, cancelled, and non-secret descriptor copies
"""

from __future__ import annotations

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

    # Acquire this before checkout: a repeated start must not force-checkout the
    # source tree of a command that is already running.
    try:
        lock = os.open(
            os.path.join(paths["state"], "start.lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError as exc:
        raise RunnerError(
            "this job was already started; inspect its state instead of starting it again"
        ) from exc
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
            "started_at": started_at,
            "executed_commit": executed,
        },
    )
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


def _run_stage(
    argv: list[str],
    *,
    cwd: str,
    timeout: int | None,
) -> tuple[int, bool, int | None]:
    """Run one argv stage in its own session and return (exit code, timed out, child pid)."""

    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=os.environ,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    _record_child_pid(process.pid)
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_group(process.pid, expected_starttime=_process_starttime(process.pid))
        try:
            process.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        exit_code = TIMEOUT_EXIT_CODE
    _record_child_pid(None)
    return exit_code, timed_out, process.pid


def _state_paths_from_environment() -> dict[str, str]:
    job_directory = os.environ.get("WAVCSE_JOB_DIRECTORY")
    if not job_directory:
        raise RunnerError("WAVCSE_JOB_DIRECTORY is missing from the job environment")
    return _job_paths(job_directory)


def _record_child_pid(child_pid: int | None) -> None:
    """Publish the current stage's process-group id so cancel can target it exactly."""

    paths = _state_paths_from_environment()
    payload = _read_json(paths["pid"]) or {}
    payload["child_pid"] = child_pid
    payload["child_starttime"] = _process_starttime(child_pid) if child_pid else None
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
    if exit_code == 0 and setup_argv:
        stage = STAGE_SETUP
        sys.stdout.write("---- setup ----\n")
        sys.stdout.flush()
        exit_code, timed_out, _ = _run_stage(setup_argv, cwd=os.getcwd(), timeout=timeout)
    if exit_code == 0:
        stage = STAGE_COMMAND
        if not _checkout_verified(requested):
            exit_code = 1
            sys.stdout.write("source HEAD or cleanliness changed during setup; refusing command\n")
            sys.stdout.flush()
        else:
            sys.stdout.write("---- command ----\n")
            sys.stdout.flush()
            exit_code, timed_out, _ = _run_stage(argv, cwd=os.getcwd(), timeout=timeout)

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


def inspect(descriptor: dict[str, Any]) -> None:
    """Report job status from the worker's own recorded evidence."""

    job_directory = _absolute_job_directory(descriptor)
    paths = _job_paths(job_directory)
    finished = _read_json(paths["finished"])
    cancelled = _read_json(paths["cancelled"])
    pid_state = _read_json(paths["pid"]) or {}
    supervisor_pid = pid_state.get("supervisor_pid")
    child_pid = pid_state.get("child_pid")
    supervisor_alive = (
        isinstance(supervisor_pid, int)
        and _same_process(supervisor_pid, pid_state.get("supervisor_starttime"))
        and _process_alive(supervisor_pid)
    )
    child_alive = (
        isinstance(child_pid, int)
        and _same_process(child_pid, pid_state.get("child_starttime"))
        and _process_alive(child_pid)
    )

    if finished is not None:
        status = STATUS_FINISHED
    elif cancelled is not None:
        status = STATUS_CANCELLED
    elif supervisor_alive or child_alive:
        status = STATUS_RUNNING
    else:
        status = STATUS_UNKNOWN

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
    emit("started_at", (finished or cancelled or pid_state).get("started_at") or "")
    emit("finished_at", (finished or cancelled or {}).get("finished_at") or "")
    emit("executed_commit", executed or "")
    emit("log_bytes", log_bytes)


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
    """Terminate the job's own process tree without touching anything else."""

    job_directory = _absolute_job_directory(descriptor)
    paths = _job_paths(job_directory)
    pid_state = _read_json(paths["pid"]) or {}
    finished = _read_json(paths["finished"])
    supervisor_pid = pid_state.get("supervisor_pid")
    child_pid = pid_state.get("child_pid")
    child_alive = (
        isinstance(child_pid, int)
        and _same_process(child_pid, pid_state.get("child_starttime"))
        and _process_alive(child_pid)
    )
    supervisor_alive = (
        isinstance(supervisor_pid, int)
        and _same_process(supervisor_pid, pid_state.get("supervisor_starttime"))
        and _process_alive(supervisor_pid)
    )

    if finished is not None and not child_alive:
        emit("cancelled", "false")
        emit("already_finished", "true")
        emit("exit_code", finished.get("exit_code"))
        emit("pid", "")
        return

    if _read_json(paths["cancelled"]) is not None:
        emit("cancelled", "true")
        emit("already_finished", "false")
        emit("pid", supervisor_pid if isinstance(supervisor_pid, int) else "")
        return
    if not child_alive and not supervisor_alive:
        raise RunnerError(
            "no process with this job's recorded identity is running; inspect job status "
            "before claiming cancellation"
        )

    if child_alive:
        if not _is_our_child(child_pid, pid_state.get("child_starttime")):
            raise RunnerError(
                f"refusing to signal process {child_pid}: it does not match this job's "
                "recorded command"
            )
        terminate_group(child_pid, expected_starttime=pid_state.get("child_starttime"))
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
                if not child_alive:
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
        {"cancelled_at": cancelled_at, "exit_code": CANCELLED_EXIT_CODE},
    )
    emit("cancelled", "true")
    emit("already_finished", "false")
    emit("pid", supervisor_pid if isinstance(supervisor_pid, int) else "")
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
