"""Controller-side exact-commit job execution over direct, non-interactive SSH.

The reviewed worker-side runner (`worker/job_runner.py`) is installed once on a
bootstrapped worker, verified by SHA-256, and then invoked once per job phase. Phase 6
adds no worker daemon and no controller-side scheduler: every phase is one bounded SSH
execution, and the detached job process is owned by the worker's own session.

Secret environment values travel only inside the descriptor JSON on the SSH stdin
stream, exactly like a Phase 5 presigned URL, so they never appear in a process argument
list on either side or in a durable record.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.errors import JobExecutionError, SshCommandError
from wavcse_infra.redaction import redact
from wavcse_infra.workers.bootstrap import BOOTSTRAP_VERSION
from wavcse_infra.workers.ssh import SshCommandResult, SshExecutor, WorkerSshWaiter

SCHEMA_KEY = "wavcse_job_schema"
SCHEMA_VERSION = "1"
INSTALL_KEY = "wavcse_job_install"
DIAGNOSTIC_LIMIT = 500
_RUNNER_MODULE = "job_runner.py"
_INSTALL_TIMEOUT_SECONDS = 120.0

# Installed on the worker to write the reviewed runner atomically after checking its
# digest. It is a constant reviewed program: the destination and digest are passed as
# argv and the payload arrives on stdin, so nothing untrusted is interpolated into it.
_INSTALL_PROGRAM = (
    "import hashlib, os, sys\n"
    "sys.stdout.write('wavcse_job_schema\\t1\\n')\n"
    "destination, expected = sys.argv[1], sys.argv[2]\n"
    "payload = sys.stdin.buffer.read()\n"
    "digest = hashlib.sha256(payload).hexdigest()\n"
    "if digest != expected:\n"
    "    sys.stderr.write('worker runner payload digest does not match the reviewed digest\\n')\n"
    "    raise SystemExit(1)\n"
    "if not os.path.isabs(destination):\n"
    "    sys.stderr.write('worker runner destination must be an absolute path\\n')\n"
    "    raise SystemExit(1)\n"
    "current = None\n"
    "try:\n"
    "    with open(destination, 'rb') as handle:\n"
    "        current = hashlib.sha256(handle.read()).hexdigest()\n"
    "except OSError:\n"
    "    current = None\n"
    "if current == digest:\n"
    "    sys.stdout.write('wavcse_job_install\\tunchanged\\n')\n"
    "    raise SystemExit(0)\n"
    "os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)\n"
    "temporary = destination + '.partial.' + str(os.getpid())\n"
    "with open(temporary, 'wb') as handle:\n"
    "    handle.write(payload)\n"
    "    handle.flush()\n"
    "    os.fsync(handle.fileno())\n"
    "os.chmod(temporary, 0o600)\n"
    "os.replace(temporary, destination)\n"
    "sys.stdout.write('wavcse_job_install\\tinstalled\\n')\n"
)

_REQUIRED_INSPECT_FIELDS = frozenset(
    {
        "status",
        "pid",
        "exit_code",
        "stage",
        "timed_out",
        "cancelled",
        "started_at",
        "finished_at",
        "executed_commit",
        "log_bytes",
    }
)


class PrepareResult(BaseModel):
    """Verified source materialization for one job."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    executed_commit: str
    job_directory: str
    source_directory: str


class StartResult(BaseModel):
    """Detached job start acknowledgement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pid: int = Field(ge=1)
    started_at: datetime
    executed_commit: str


class RemoteJobStatus(BaseModel):
    """Worker-reported job status derived from the worker's own recorded evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["running", "finished", "cancelled", "unknown"]
    pid: int | None = Field(default=None, ge=1)
    exit_code: int | None = None
    stage: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    executed_commit: str | None = None
    log_bytes: int | None = Field(default=None, ge=0)


class CancelResult(BaseModel):
    """Outcome of one idempotent job cancellation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cancelled: bool
    already_finished: bool
    pid: int | None = Field(default=None, ge=1)
    cancelled_at: datetime | None = None
    exit_code: int | None = None


def load_worker_job_runner_source() -> str:
    """Return the reviewed worker-side job runner shipped with this repository."""

    repository_module = Path(__file__).resolve().parents[3] / "worker" / _RUNNER_MODULE
    if repository_module.is_file():
        return repository_module.read_text(encoding="utf-8")
    packaged = resources.files("wavcse_infra").joinpath("worker", _RUNNER_MODULE)
    return packaged.read_text(encoding="utf-8")


def worker_job_runner_digest(source: str) -> str:
    """Return the SHA-256 digest used to verify the worker's installed runner."""

    return hashlib.sha256(source.encode("utf-8")).hexdigest()


class JobExecutor:
    """Run job phases on one explicit worker through the reviewed worker runner."""

    def __init__(
        self,
        waiter: WorkerSshWaiter,
        executor: SshExecutor,
        ssh_config: SshConfig,
        jobs_config: JobsConfig,
    ) -> None:
        self._waiter = waiter
        self._executor = executor
        self._ssh = ssh_config
        self._config = jobs_config

    @property
    def runner_path(self) -> str:
        return self._config.runner_path

    def install_runner(self, worker_id: str) -> str:
        """Install the reviewed runner atomically; report `installed` or `unchanged`."""

        source = load_worker_job_runner_source()
        digest = worker_job_runner_digest(source)
        ready = self._waiter.wait(worker_id)
        try:
            result = self._executor.run_checked(
                ready.connection,
                ("python3", "-c", _INSTALL_PROGRAM, self._config.runner_path, digest),
                input_text=source,
                timeout_seconds=min(self._ssh.bootstrap_timeout_seconds, _INSTALL_TIMEOUT_SECONDS),
            )
        except SshCommandError as exc:
            raise JobExecutionError(
                f"Could not install the reviewed job runner on worker {worker_id}: {redact(exc)}"
            ) from exc
        values = _parse_runner_output(result, worker_id)
        status = values.get(INSTALL_KEY)
        if status not in {"installed", "unchanged"}:
            raise JobExecutionError(
                f"Worker {worker_id} did not confirm the job runner installation; "
                f"{_remote_diagnostics(result)}"
            )
        return status

    def prepare(
        self,
        worker_id: str,
        *,
        job_id: str,
        job_directory: str,
        repository: str,
        commit: str,
        name: str | None = None,
        input_destinations: tuple[str, ...] = (),
    ) -> PrepareResult:
        """Create the isolated workspace and verify the exact checked-out commit."""

        descriptor = {
            "job_id": job_id,
            "job_directory": job_directory,
            "name": name,
            "source": {"repository": repository, "commit": commit},
            "expected_bootstrap_version": BOOTSTRAP_VERSION,
            "input_destinations": list(input_destinations),
        }
        values = self._run_phase(worker_id, "prepare", descriptor, required={"executed_commit"})
        executed = values["executed_commit"]
        if executed.lower() != commit.lower():
            raise JobExecutionError(
                f"Worker {worker_id} reported executed commit {executed or 'unknown'}, but "
                f"{commit} was requested; refusing to run a different revision"
            )
        return PrepareResult(
            executed_commit=executed,
            job_directory=values.get("job_directory", job_directory),
            source_directory=values.get("source_directory", ""),
        )

    def start(
        self,
        worker_id: str,
        *,
        job_id: str,
        job_directory: str,
        repository: str,
        commit: str,
        argv: list[str],
        setup_argv: list[str] | None,
        working_directory: str | None,
        environment: dict[str, str],
        secrets: dict[str, str],
        timeout_seconds: int | None,
        infra_environment: dict[str, str],
    ) -> StartResult:
        """Start the job detached in the worker's own session and return its PID."""

        descriptor = {
            "job_id": job_id,
            "job_directory": job_directory,
            "source": {"repository": repository, "commit": commit},
            "command": {"argv": argv, "working_directory": working_directory},
            "setup_argv": setup_argv,
            "environment": environment,
            "secrets": secrets,
            "timeout_seconds": timeout_seconds,
            "infra": infra_environment,
        }
        values = self._run_phase(
            worker_id, "start", descriptor, required={"pid", "started_at", "executed_commit"}
        )
        try:
            pid = int(values["pid"])
            started_at = values["started_at"]
            return StartResult(
                pid=pid,
                started_at=_timestamp(started_at, worker_id, "started_at"),
                executed_commit=values["executed_commit"],
            )
        except (ValueError, ValidationError) as exc:
            raise JobExecutionError(
                f"Worker {worker_id} returned an unusable start acknowledgement: {exc}"
            ) from exc

    def inspect(self, worker_id: str, *, job_id: str, job_directory: str) -> RemoteJobStatus:
        """Read the worker's recorded job evidence."""

        values = self._run_phase(
            worker_id,
            "inspect",
            {"job_id": job_id, "job_directory": job_directory},
            required=_REQUIRED_INSPECT_FIELDS,
        )
        try:
            return RemoteJobStatus(
                status=values["status"],
                pid=_optional_int(values["pid"]),
                exit_code=_optional_int(values["exit_code"]),
                stage=values["stage"] or None,
                timed_out=values["timed_out"] == "true",
                cancelled=values["cancelled"] == "true",
                started_at=_optional_timestamp(values["started_at"]),
                finished_at=_optional_timestamp(values["finished_at"]),
                executed_commit=values["executed_commit"] or None,
                log_bytes=_optional_int(values["log_bytes"]),
            )
        except ValidationError as exc:
            raise JobExecutionError(
                f"Worker {worker_id} returned an unusable job status: {exc}"
            ) from exc

    def logs(self, worker_id: str, *, job_id: str, job_directory: str, tail_bytes: int) -> str:
        """Return a bounded tail of the job's combined stdout/stderr log."""

        descriptor = {
            "job_id": job_id,
            "job_directory": job_directory,
            "tail_bytes": tail_bytes,
        }
        ready = self._waiter.wait(worker_id)
        try:
            result = self._executor.run_checked(
                ready.connection,
                ("python3", self._config.runner_path, "logs"),
                input_text=json.dumps(descriptor),
                timeout_seconds=self._ssh.transfer_timeout_seconds,
            )
        except SshCommandError as exc:
            raise JobExecutionError(
                f"Could not read logs for job {job_id} on worker {worker_id}: {redact(exc)}"
            ) from exc
        return result.stdout

    def cancel(self, worker_id: str, *, job_id: str, job_directory: str) -> CancelResult:
        """Terminate the job's own process tree, leaving the worker untouched."""

        values = self._run_phase(
            worker_id,
            "cancel",
            {"job_id": job_id, "job_directory": job_directory},
            required={"cancelled", "already_finished"},
        )
        try:
            return CancelResult(
                cancelled=values["cancelled"] == "true",
                already_finished=values["already_finished"] == "true",
                pid=_optional_int(values.get("pid", "")),
                cancelled_at=_optional_timestamp(values.get("cancelled_at", "")),
                exit_code=_optional_int(values.get("exit_code", "")),
            )
        except ValidationError as exc:
            raise JobExecutionError(
                f"Worker {worker_id} returned an unusable cancellation result: {exc}"
            ) from exc

    def _run_phase(
        self,
        worker_id: str,
        subcommand: str,
        descriptor: dict[str, Any],
        *,
        required: frozenset[str],
    ) -> dict[str, str]:
        """Run one synchronous, bounded job phase and parse its protocol strictly."""

        ready = self._waiter.wait(worker_id)
        try:
            result = self._executor.run_checked(
                ready.connection,
                ("python3", self._config.runner_path, subcommand),
                input_text=json.dumps(descriptor),
                timeout_seconds=self._ssh.bootstrap_timeout_seconds,
            )
        except SshCommandError as exc:
            raise JobExecutionError(
                f"Job {subcommand} failed on RunPod worker {worker_id}: {redact(exc)}"
            ) from exc
        values = _parse_runner_output(result, worker_id)
        missing = required - set(values)
        if missing:
            raise JobExecutionError(
                f"Worker {worker_id} returned an incomplete job {subcommand} result "
                f"(missing {', '.join(sorted(missing))}); {_remote_diagnostics(result)}"
            )
        return values


def _parse_runner_output(result: SshCommandResult, worker_id: str) -> dict[str, str]:
    """Parse the worker runner's tab-separated protocol, requiring schema version 1."""

    lines = result.stdout.splitlines()
    if not lines or lines[0] != f"{SCHEMA_KEY}\t{SCHEMA_VERSION}":
        raise JobExecutionError(
            f"Worker {worker_id} returned job output without schema version 1; "
            f"{_remote_diagnostics(result)}"
        )
    values: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, separator, value = line.partition("\t")
        if not separator or not key:
            raise JobExecutionError(
                f"Worker {worker_id} returned malformed job output; {_remote_diagnostics(result)}"
            )
        if key in values:
            raise JobExecutionError(f"Worker {worker_id} repeated a job result field")
        values[key] = value
    return values


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _optional_timestamp(value: str) -> datetime | None:
    text = value.strip()
    return _timestamp(text, "worker", "timestamp") if text else None


def _timestamp(value: str, worker_id: str, label: str) -> datetime:
    text = value.strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise JobExecutionError(
            f"Worker {worker_id} returned an unparseable {label}: {text or 'empty'}"
        ) from exc


def _remote_diagnostics(result: SshCommandResult) -> str:
    return "; ".join(
        (
            _stream_diagnostic("stdout", result.stdout),
            _stream_diagnostic("stderr", result.stderr),
        )
    )


def _stream_diagnostic(name: str, value: str) -> str:
    cleaned = re.sub(r"\s+", " ", redact(value.strip()))
    if not cleaned:
        return f"remote {name}=<empty>"
    return f"remote {name}={cleaned[:DIAGNOSTIC_LIMIT]!r}"
