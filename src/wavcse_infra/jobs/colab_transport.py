"""Colab transport for the *same* reviewed job and artifact runners used over SSH.

Only a fixed launcher appears in Colab CLI exec history. Its per-operation input is
uploaded via ContentsClient and consumed once from an ephemeral 0600 file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.errors import (
    JobExecutionError,
    ProviderError,
    ProviderResponseError,
    SshCommandError,
    SshConnectionError,
)
from wavcse_infra.jobs.execution import JobExecutor
from wavcse_infra.models import Worker, WorkerReadinessState
from wavcse_infra.providers.colab import ColabClient
from wavcse_infra.redaction import redact
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.ssh import SshCommandResult

# The launcher validates schema, session and expiry, unlinks the envelope immediately,
# then invokes exactly one reviewed Python runner with stdin from the uploaded envelope.
_LAUNCHER = """import datetime, json, os, subprocess
p = '/content/wavcse-envelope-REQUEST.json'
try:
    with open(p, encoding='utf-8') as f: e = json.load(f)
    os.chmod(p, 0o600)
    os.unlink(p)
    assert e['schema_version'] == 1 and e['session'] == 'SESSION'
    assert datetime.datetime.now(datetime.timezone.utc).timestamp() < e['expires_at']
    assert e['job_id'] == 'JOBID'
    assert isinstance(e['argv'], list) and e['argv'][0] == 'python3'
    result = subprocess.run(e['argv'], input=e['stdin'], text=True,
                            capture_output=True, timeout=TIMEOUT)
    print(result.stdout, end='')
    print(result.stderr, end='')
    print('wavcse_colab_exit\\t' + str(result.returncode))
except Exception:
    try: os.unlink(p)
    except FileNotFoundError: pass
    print('wavcse_colab_transport_failed')
"""
# Colab mounts the NVIDIA driver libraries (libnvidia-ml, libcuda) here instead of a
# default loader directory. A recorded job's minimal environment must carry this path or
# `nvidia-smi` and `torch.cuda` cannot see the allocated accelerator.
_COLAB_GPU_LIBRARY_PATH = "/usr/lib64-nvidia"
_JOB_ID = re.compile(r"^job-[0-9a-f]{16}$")


def validate_envelope(envelope: dict[str, object], session: str, job_id: str) -> None:
    """Reject mismatched session, stale capability, and malformed command before upload."""
    if envelope.get("schema_version") != 1 or envelope.get("session") != session:
        raise ProviderResponseError("Colab envelope schema/session identity mismatch")
    if envelope.get("job_id") != job_id or not _JOB_ID.fullmatch(job_id):
        raise ProviderResponseError("Colab envelope job identity mismatch")
    expires = envelope.get("expires_at")
    if not isinstance(expires, (float, int)) or expires <= datetime.now(UTC).timestamp():
        raise ProviderResponseError("Colab envelope has expired")
    if not isinstance(envelope.get("stdin"), str):
        raise ProviderResponseError("Colab envelope has no request body")
    argv = envelope.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != "python3"
        or not all(isinstance(arg, str) for arg in argv)
    ):
        raise ProviderResponseError("Colab envelope contains an invalid worker command")


@dataclass(frozen=True)
class ColabConnection:
    provider_worker_id: str


@dataclass(frozen=True)
class ColabReady:
    worker: Worker
    connection: ColabConnection


class ColabWaiter:
    """Check authoritative provider presence and local readiness, not an SSH endpoint."""

    def __init__(
        self, client: ColabClient, state: WorkerStateStore, *, require_ready: bool = True
    ) -> None:
        self.client, self.state, self.require_ready = client, state, require_ready

    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> ColabReady:
        del timeout_seconds
        worker = self.client.get_worker(worker_id)
        record = self.state.get(worker_id)
        if (
            record is None
            or record.provider_absent
            or record.create_pending
            or (self.require_ready and record.readiness_state is not WorkerReadinessState.READY)
        ):
            raise SshConnectionError(f"Colab session {worker_id} is not an owned READY worker")
        return ColabReady(worker, ColabConnection(worker_id))


class ColabExecutor:
    """Implement the worker command interface with upload + fixed Colab exec, never SSH."""

    def __init__(self, client: ColabClient) -> None:
        self.client = client

    def run_checked(
        self,
        connection: ColabConnection,
        remote_argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        session = connection.provider_worker_id
        argv = list(remote_argv)
        if not argv or argv[0] != "python3":
            raise ProviderResponseError("Colab accepts only the reviewed Python worker runners")
        descriptor = None
        if input_text and not (len(argv) > 2 and argv[1] == "-"):
            try:
                descriptor = json.loads(input_text)
            except ValueError:
                descriptor = None  # Reviewed runner installation carries source, not JSON.
        job_id = descriptor.get("job_id") if isinstance(descriptor, dict) else None
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            # Transfer phases and runner installation have no job descriptor: give each
            # envelope a random scoped identity. The envelope is not durable job state.
            job_id = f"job-{uuid4().hex[:16]}"
        token = uuid4().hex
        lifetime = min(3600, max(60, int(timeout_seconds or 300) + 60))
        envelope: dict[str, object] = {
            "schema_version": 1,
            "session": session,
            "job_id": job_id,
            "expires_at": (datetime.now(UTC) + timedelta(seconds=lifetime)).timestamp(),
            "argv": argv,
            "stdin": input_text or "",
        }
        validate_envelope(envelope, session, job_id)
        remote = f"/content/wavcse-envelope-{token}.json"
        descriptor_file, filename = tempfile.mkstemp(prefix="wavcse-envelope-", suffix=".json")
        os.fchmod(descriptor_file, 0o600)
        try:
            with os.fdopen(descriptor_file, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            self.client.upload_file(session, Path(filename), remote)
        finally:
            Path(filename).unlink(missing_ok=True)
        code = (
            _LAUNCHER.replace("REQUEST", token)
            .replace("SESSION", session)
            .replace("JOBID", job_id)
            .replace("TIMEOUT", str(int(timeout_seconds or 300)))
        )
        try:
            output = self.client.exec_code(session, code, timeout=(timeout_seconds or 300) + 5)
        except ProviderError as exc:
            with suppress(ProviderError):
                self.client.remove_file(session, remote)
            # The kernel may still be running; do not infer that the job failed.
            raise SshConnectionError(
                f"Colab exec transport for {session} lost its response"
            ) from exc
        lines = output.splitlines()
        markers = [line for line in lines if line.startswith("wavcse_colab_exit\t")]
        if len(markers) != 1:
            with suppress(ProviderError):
                self.client.remove_file(session, remote)
            raise SshConnectionError(
                f"Colab exec transport for {session} returned no completion evidence"
            )
        try:
            exit_code = int(markers[0].split("\t", 1)[1])
        except ValueError as exc:
            raise SshConnectionError(
                f"Colab exec transport for {session} returned invalid status"
            ) from exc
        stdout = output.replace(markers[0], "", 1).strip() + "\n"
        result = SshCommandResult(exit_code, stdout, "")
        if exit_code != 0:
            raise SshCommandError(
                f"Colab worker {session} command exited {exit_code}: {redact(stdout)[:500]}"
            )
        return result


class ColabJobExecutor(JobExecutor):
    """Use the common runner, but retrieve logs by file transfer, not exec output."""

    def __init__(
        self,
        client: ColabClient,
        waiter: ColabWaiter,
        executor: ColabExecutor,
        ssh_config: SshConfig,
        jobs_config: JobsConfig,
    ) -> None:
        super().__init__(waiter, executor, ssh_config, jobs_config)
        self._colab_client = client
        self._colab_waiter = waiter

    def transport_environment(self) -> dict[str, str]:
        """Expose Colab's NVIDIA driver library directory to the recorded job."""

        return {"LD_LIBRARY_PATH": _COLAB_GPU_LIBRARY_PATH}

    def logs(self, worker_id: str, *, job_id: str, job_directory: str, tail_bytes: int) -> str:
        if (
            not _JOB_ID.fullmatch(job_id)
            or job_directory != f"{self._config.worker_root}/{job_id}"
            or not 0 < tail_bytes <= 16 * 1024 * 1024
        ):
            raise JobExecutionError(f"Colab log request for {job_id} is not a bounded job path")
        self._colab_waiter.wait(worker_id)
        remote = f"/content/wavcse-log-{job_id}-{uuid4().hex[:12]}.txt"
        source = f"{job_directory}/logs/job.log"
        code = (
            "import os\n"
            "try:\n"
            f"    with open({source!r}, 'rb') as f:\n"
            "        f.seek(0, 2)\n"
            f"        f.seek(max(0, f.tell() - {tail_bytes}))\n"
            "        data = f.read()\n"
            f"    with open({remote!r}, 'wb') as f: f.write(data)\n"
            f"    os.chmod({remote!r}, 0o600)\n"
            "    print('wavcse_colab_log_ready')\n"
            "except Exception:\n"
            "    print('wavcse_colab_log_failed')\n"
        )
        result = self._colab_client.exec_code(worker_id, code)
        if result.splitlines() != ["wavcse_colab_log_ready"]:
            raise JobExecutionError(f"Colab {worker_id} could not stage log for {job_id}")
        descriptor, filename = tempfile.mkstemp(prefix="wavcse-log-", suffix=".txt")
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        try:
            self._colab_client.download_file(worker_id, remote, Path(filename))
            return Path(filename).read_text(encoding="utf-8", errors="replace")
        finally:
            Path(filename).unlink(missing_ok=True)
            with suppress(ProviderError):
                self._colab_client.remove_file(worker_id, remote)
