"""Exercise the reviewed job runner through the actual uploaded Colab launcher locally."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from wavcse_infra.config import JobsConfig, SshConfig
from wavcse_infra.jobs.colab_transport import ColabExecutor, ColabJobExecutor, ColabWaiter
from wavcse_infra.jobs.execution import load_worker_job_runner_source
from wavcse_infra.models import ExecutionTransport, ProviderKind, Worker, WorkerState
from wavcse_infra.state import WorkerStateStore

SESSION = "wavcse-abcdef123456"
JOB_ID = "job-1234567890abcdef"


class LocalColab:
    """Replace only the CLI contents/kernel boundary with local file execution."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.uploaded: list[Path] = []
        self.executed: list[str] = []

    def get_worker(self, name: str) -> Worker:
        assert name == SESSION
        return Worker(
            provider=ProviderKind.COLAB,
            execution_transport=ExecutionTransport.COLAB_EXEC,
            id=name,
            name=name,
            state=WorkerState.RUNNING,
            gpu_type="T4",
            gpu_count=1,
        )

    def upload_file(self, name: str, local: Path, remote: str) -> None:
        assert name == SESSION
        destination = self.root / Path(remote).name
        destination.write_bytes(local.read_bytes())
        self.uploaded.append(destination)

    def exec_code(self, name: str, code: str, *, timeout: float | None = None) -> str:
        assert name == SESSION
        self.executed.append(code)
        code = code.replace("/content/wavcse-envelope-", str(self.root / "wavcse-envelope-"))
        code = code.replace("/content/wavcse-log-", str(self.root / "wavcse-log-"))
        output = io.StringIO()
        with redirect_stdout(output):
            exec(code)
        return output.getvalue()

    def download_file(self, name: str, remote: str, local: Path) -> None:
        assert name == SESSION
        local.write_bytes((self.root / Path(remote).name).read_bytes())

    def remove_file(self, name: str, remote: str) -> None:
        (self.root / Path(remote).name).unlink(missing_ok=True)


def test_colab_job_environment_exposes_the_gpu_driver_library_path(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    client = LocalColab(tmp_path)
    job_executor = ColabJobExecutor(
        client,  # type: ignore[arg-type]
        ColabWaiter(client, store),  # type: ignore[arg-type]
        ColabExecutor(client),  # type: ignore[arg-type]
        SshConfig(),
        JobsConfig(runner_path=str(tmp_path / "runner"), worker_root=str(tmp_path / "jobs")),
    )
    # Colab mounts libnvidia-ml outside the loader default; recorded jobs need the path.
    assert job_executor.transport_environment() == {"LD_LIBRARY_PATH": "/usr/lib64-nvidia"}


def test_same_sha_verified_runner_installs_and_inspects_over_colab_upload(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    store.record_colab_intent(SESSION, "T4")
    client = LocalColab(tmp_path)
    store.record_colab_created(client.get_worker(SESSION))
    from decimal import Decimal

    store.record_colab_ready(SESSION, gpu_model="Tesla T4", rate=Decimal("1.8"), disk_bytes=10**9)
    runner_path = tmp_path / "runner" / "job_runner.py"
    job_executor = ColabJobExecutor(
        client,  # type: ignore[arg-type]
        ColabWaiter(client, store),  # type: ignore[arg-type]
        ColabExecutor(client),  # type: ignore[arg-type]
        SshConfig(),
        JobsConfig(runner_path=str(runner_path), worker_root=str(tmp_path / "jobs")),
    )
    assert job_executor.install_runner(SESSION) == "installed"
    assert runner_path.read_text() == load_worker_job_runner_source()
    status = job_executor.inspect(
        SESSION, job_id=JOB_ID, job_directory=str(tmp_path / "jobs" / JOB_ID)
    )
    assert status.job_directory_exists is False
    assert all(not path.exists() for path in client.uploaded)
    assert all("X-Amz-Signature" not in source for source in client.executed)
    job_directory = tmp_path / "jobs" / JOB_ID
    log_path = job_directory / "logs" / "job.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("private-output\nlast-line\n")
    assert (
        job_executor.logs(SESSION, job_id=JOB_ID, job_directory=str(job_directory), tail_bytes=10)
        == "last-line\n"
    )
    assert all("private-output" not in code and "last-line" not in code for code in client.executed)
    assert not list(tmp_path.glob("wavcse-log-*"))
