"""End-to-end controller/worker-runner integration, executed locally and offline.

The other job tests use hand-written protocol doubles, which cannot catch a mismatch
between the descriptor the controller sends and the fields the reviewed worker runner
accepts. This module replaces only the SSH transport: the real `JobExecutor`, the real
`JobSubmitter`, the real `JobCoordinator`, and the real `worker/job_runner.py` process all
participate, with a scripted `git` stub standing in for GitHub and a recorded double
standing in for the Phase 5 artifact transfer.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from job_fakes import FakeStorage, FakeTransfer, FakeWaiter, job_context, job_spec_document
from pydantic import ValidationError

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.errors import JobExecutionError, SshCommandError
from wavcse_infra.jobs.execution import JobExecutor
from wavcse_infra.jobs.models import JobState, load_job_spec
from wavcse_infra.jobs.status import JobCoordinator
from wavcse_infra.jobs.submit import JobSubmitter
from wavcse_infra.storage.worker_transfer import validate_worker_path
from wavcse_infra.workers.ssh import SshCommandResult

REPOSITORY = "https://github.com/Synergy-io/wavCSE.git"
COMMIT = "a" * 40
RUNNER_SOURCE = Path(__file__).resolve().parents[2] / "worker" / "job_runner.py"

FAKE_GIT = """#!/usr/bin/env python3
import json, os, sys

arguments = sys.argv[3:]  # skip <git> -c advice.detachedHead=false
subcommand = arguments[0] if arguments else ""
if subcommand == "clone":
    os.makedirs(os.path.join(arguments[-1], ".git"), exist_ok=True)
elif subcommand == "rev-parse":
    sys.stdout.write("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n")
sys.exit(0)
"""


class LocalRunnerExecutor:
    """Run the reviewed worker runner in this container, exactly as direct SSH would."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(
        self,
        connection,
        remote_argv,
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        argv = tuple(remote_argv)
        self.calls.append((argv, input_text))
        completed = subprocess.run(
            [shutil.which(argv[0]) or argv[0], *argv[1:]],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=dict(os.environ),
            check=False,
            shell=False,
        )
        return SshCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def run_checked(
        self,
        connection,
        remote_argv,
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        result = self.run(
            connection,
            remote_argv,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )
        if result.exit_code != 0:
            raise SshCommandError(
                f"Remote command on worker {connection.provider_worker_id} exited "
                f"{result.exit_code}: {result.stderr.strip()}"
            )
        return result


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> JobsConfig:
    """Provide a worker-like HOME, a scripted git, and an isolated runner location."""

    home = tmp_path / "home"
    (home / ".local/state/wavcse-worker").mkdir(parents=True)
    (home / ".local/state/wavcse-worker/bootstrap-version").write_text("1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    git = binary / "git"
    git.write_text(FAKE_GIT, encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    return JobsConfig(
        worker_root=str(tmp_path / "jobs-root"),
        runner_path=str(home / ".local/state/wavcse-worker/job_runner.py"),
        default_timeout_seconds=60,
        log_tail_bytes=65536,
    )


def _spec(**overrides):
    document = job_spec_document(
        source={"repository": REPOSITORY, "commit": COMMIT},
        **overrides,
    )
    return load_job_spec(json.dumps(document))


def _context(tmp_path: Path, jobs_config: JobsConfig, transfer: FakeTransfer, storage: FakeStorage):
    """Real controller executor and real worker runner, with SSH replaced locally."""

    return job_context(
        tmp_path,
        executor=JobExecutor(FakeWaiter(), LocalRunnerExecutor(), SshConfig(), jobs_config),
        transfer=transfer,
        storage=storage,
        jobs_config=jobs_config,
    )


def _await_terminal(context, job_id: str, timeout_seconds: float = 60.0):
    """Poll quickly; the detached job may not have finished when submit returns."""

    return JobCoordinator(context, poll_interval_seconds=0.1).wait(
        job_id, timeout_seconds=timeout_seconds
    )


def test_submission_and_status_run_the_reviewed_runner_end_to_end(
    tmp_path: Path, workspace: JobsConfig
) -> None:
    storage = FakeStorage()
    storage.objects["embeddings/v1/probe.txt"] = 18
    transfer = FakeTransfer(materialize=True)
    spec = _spec(
        name="phase6-probe",
        command={
            "argv": [
                "python3",
                "-c",
                (
                    "import json, os, pathlib, sys;"
                    "root = pathlib.Path(os.environ['WAVCSE_JOB_DIRECTORY']);"
                    "payload = (root / sys.argv[1]).read_bytes();"
                    "target = root / sys.argv[2];"
                    "target.parent.mkdir(parents=True, exist_ok=True);"
                    "target.write_text(json.dumps({'bytes': len(payload),"
                    " 'job': os.environ['INFRA_JOB_ID'],"
                    " 'commit': os.environ['INFRA_GIT_COMMIT']}));"
                    "print('probe ok')"
                ),
                "inputs/probe.txt",
                "outputs/probe.json",
            ]
        },
        inputs=[
            {
                "artifact": "embeddings/v1/probe.txt",
                "destination": "probe.txt",
                "sha256": hashlib.sha256(b"x" * 18).hexdigest(),
            }
        ],
        outputs=[{"path": "outputs/probe.json", "artifact": "jobs/probe/probe.json"}],
        runtime={"timeout_seconds": 60},
    )
    context = _context(tmp_path, workspace, transfer, storage)

    record = JobSubmitter(context).submit(spec, worker_id="pod-123")

    assert record.state is JobState.RUNNING
    assert record.executed_commit == COMMIT
    assert record.pid is not None
    assert transfer.downloads[0]["destination"] == (
        f"{workspace.worker_root}/{record.job_id}/inputs/probe.txt"
    )
    assert transfer.downloads[0]["expected_size"] == 18
    assert transfer.uploads == []

    result = _await_terminal(context, record.job_id)

    assert result.record.state is JobState.SUCCEEDED, result.record.failure_reason
    assert result.record.exit_code == 0
    assert transfer.uploads[0]["key"] == "jobs/probe/probe.json"
    assert transfer.uploads[0]["source"] == (
        f"{workspace.worker_root}/{record.job_id}/outputs/probe.json"
    )
    assert result.record.outputs[0].persisted is True
    local_log = context.job_store.read_log(record.job_id)
    assert local_log is not None and "probe ok" in local_log
    assert f"job {record.job_id}" in local_log
    assert COMMIT in local_log


def test_end_to_end_failure_preserves_the_exit_code(tmp_path: Path, workspace: JobsConfig) -> None:
    context = _context(tmp_path, workspace, FakeTransfer(), FakeStorage())
    spec = _spec(
        command={"argv": ["python3", "-c", "import sys; print('failing'); sys.exit(9)"]},
        runtime={"timeout_seconds": 60},
    )

    record = JobSubmitter(context).submit(spec, worker_id="pod-123")
    result = _await_terminal(context, record.job_id)

    assert result.record.state is JobState.FAILED
    assert result.record.exit_code == 9
    assert "9" in (result.record.state_reason or "")
    assert "failing" in (context.job_store.read_log(record.job_id) or "")


def test_end_to_end_cancellation_leaves_the_worker_untouched(
    tmp_path: Path, workspace: JobsConfig
) -> None:
    context = _context(tmp_path, workspace, FakeTransfer(), FakeStorage())
    spec = _spec(
        command={
            "argv": [
                "python3",
                "-c",
                (
                    "import os, pathlib, time;"
                    "root = pathlib.Path(os.environ['WAVCSE_JOB_DIRECTORY']);"
                    "(root / 'outputs').mkdir(parents=True, exist_ok=True);"
                    "(root / 'outputs' / 'up.txt').write_text('up');"
                    "time.sleep(120)"
                ),
            ]
        },
        runtime={"timeout_seconds": 300},
    )
    record = JobSubmitter(context).submit(spec, worker_id="pod-123")
    marker = Path(record.job_directory) / "outputs" / "up.txt"
    for _ in range(200):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), "the detached job never started"

    cancelled = JobCoordinator(context).cancel(record.job_id)

    assert cancelled.state is JobState.CANCELLED
    assert _await_terminal(context, record.job_id).record.state is JobState.CANCELLED
    assert validate_worker_path(str(marker), label="marker") == str(marker)
    for _ in range(200):
        finished = Path(record.job_directory) / "state" / "finished.json"
        if finished.exists():
            break
        time.sleep(0.05)
    assert (
        json.loads((Path(record.job_directory) / "state" / "finished.json").read_text())[
            "exit_code"
        ]
        != 0
    )


def test_runner_side_rejection_is_surfaced_to_the_controller(
    tmp_path: Path, workspace: JobsConfig
) -> None:
    """A runner that refuses a descriptor must become an actionable controller error."""

    executor = JobExecutor(FakeWaiter(), LocalRunnerExecutor(), SshConfig(), workspace)

    with pytest.raises(JobExecutionError, match="failed on RunPod worker pod-123"):
        executor.prepare(
            "pod-123",
            job_id="not-a-canonical-job-id",
            job_directory=str(tmp_path / "jobs-root" / "job-0123456789abcdef"),
            repository=REPOSITORY,
            commit=COMMIT,
        )


def test_jobs_configuration_rejects_unsafe_worker_paths() -> None:
    with pytest.raises(ValidationError):
        JobsConfig(worker_root="relative/path")
    with pytest.raises(ValidationError):
        JobsConfig(runner_path="/root/../etc/job_runner.py")
    with pytest.raises(ValidationError):
        JobsConfig(worker_root="/workspace/jobs/")
