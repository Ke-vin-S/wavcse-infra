"""Idempotent worker bootstrap and normalized readiness health checks."""

from __future__ import annotations

import csv
from importlib import resources
from pathlib import Path

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    SshCommandError,
    UnsupportedAcceleratorError,
    WorkerBootstrapError,
    WorkerHealthError,
)
from wavcse_infra.models import (
    HealthCheckStatus,
    Worker,
    WorkerGpuInfo,
    WorkerHealthCheck,
    WorkerHealthReport,
    WorkerReadinessState,
)
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.ssh import SshExecutor, SshWaitResult, WorkerSshWaiter

BOOTSTRAP_VERSION = "1"
_DEFAULT_DISK_PATH = "/workspace"
_EMBEDDING_SIZE_BYTES = 20 * 1024**3


class WorkerBootstrapper:
    """Turn a provider-running NVIDIA worker into a checked READY worker."""

    def __init__(
        self,
        waiter: WorkerSshWaiter,
        executor: SshExecutor,
        state_store: WorkerStateStore,
        ssh_config: SshConfig,
    ) -> None:
        self._waiter = waiter
        self._executor = executor
        self._state = state_store
        self._config = ssh_config

    def bootstrap(
        self,
        worker_id: str,
        *,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> WorkerHealthReport:
        """Wait for SSH, run the versioned script, then require every health check."""

        ready = self._waiter.wait(worker_id, timeout_seconds=wait_timeout_seconds)
        _require_supported_accelerator(ready.worker)
        try:
            self._executor.run_checked(
                ready.connection,
                ("bash", "-s", "--", BOOTSTRAP_VERSION),
                input_text=load_worker_script("bootstrap.sh"),
                timeout_seconds=(
                    self._config.bootstrap_timeout_seconds
                    if command_timeout_seconds is None
                    else command_timeout_seconds
                ),
            )
        except SshCommandError as exc:
            raise WorkerBootstrapError(
                f"Bootstrap failed on RunPod worker {worker_id}: {exc}"
            ) from exc
        self._state.mark_bootstrapped(worker_id, BOOTSTRAP_VERSION)
        return self._health(ready, command_timeout_seconds=command_timeout_seconds)

    def health(
        self,
        worker_id: str,
        *,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> WorkerHealthReport:
        """Inspect a running worker without installing or changing worker packages."""

        ready = self._waiter.wait(worker_id, timeout_seconds=wait_timeout_seconds)
        _require_supported_accelerator(ready.worker)
        return self._health(ready, command_timeout_seconds=command_timeout_seconds)

    def _health(
        self,
        ready: SshWaitResult,
        *,
        command_timeout_seconds: float | None,
    ) -> WorkerHealthReport:
        disk_path = ready.worker.volume_mount_path or _DEFAULT_DISK_PATH
        try:
            result = self._executor.run_checked(
                ready.connection,
                ("bash", "-s", "--", BOOTSTRAP_VERSION, disk_path),
                input_text=load_worker_script("health-check.sh"),
                timeout_seconds=(
                    self._config.command_timeout_seconds
                    if command_timeout_seconds is None
                    else command_timeout_seconds
                ),
            )
        except SshCommandError as exc:
            raise WorkerHealthError(
                f"Health inspection failed on RunPod worker {ready.worker.id}: {exc}"
            ) from exc
        report = parse_health_output(ready, result.stdout)
        if report.gpu is not None and report.gpu.count > 0:
            self._state.mark_gpu_healthy(ready.worker.id)
        self._state.record_health(report)
        return report


def load_worker_script(name: str) -> str:
    """Load a reviewed worker script from a checkout or an installed wheel."""

    if name not in {"bootstrap.sh", "health-check.sh"}:
        raise ValueError(f"Unknown worker script: {name}")
    repository_script = Path(__file__).resolve().parents[3] / "worker" / name
    if repository_script.is_file():
        return repository_script.read_text(encoding="utf-8")
    packaged_script = resources.files("wavcse_infra").joinpath("worker", name)
    return packaged_script.read_text(encoding="utf-8")


def parse_health_output(ready: SshWaitResult, output: str) -> WorkerHealthReport:
    """Parse the small tab-separated worker protocol into normalized health data."""

    lines = output.splitlines()
    try:
        protocol_start = lines.index("wavcse_health_schema\t1")
    except ValueError as exc:
        raise WorkerHealthError(
            f"Worker {ready.worker.id} returned health output without schema version 1"
        ) from exc

    values: dict[str, str] = {}
    for line in lines[protocol_start + 1 :]:
        if not line:
            continue
        key, separator, value = line.partition("\t")
        if not separator or not key or key in values:
            raise WorkerHealthError(f"Worker {ready.worker.id} returned malformed health output")
        values[key] = value

    observed_bootstrap = values.get("bootstrap_version") or None
    disk_path = values.get("disk_path") or None
    disk_available = _optional_nonnegative_int(values.get("disk_available_bytes"))
    gpu = _parse_nvidia_gpu(values)
    checks = (
        WorkerHealthCheck(
            name="provider",
            status=HealthCheckStatus.PASS,
            detail=f"RunPod state is {ready.worker.state.value}",
        ),
        WorkerHealthCheck(
            name="ssh",
            status=HealthCheckStatus.PASS,
            detail=(
                f"authenticated through {ready.connection.kind} endpoint "
                f"{ready.connection.host}:{ready.connection.port}"
            ),
        ),
        _presence_check(
            "bootstrap",
            observed_bootstrap == BOOTSTRAP_VERSION,
            (
                f"version {observed_bootstrap} installed"
                if observed_bootstrap
                else "bootstrap marker is missing"
            ),
            f"expected version {BOOTSTRAP_VERSION}, observed {observed_bootstrap or 'missing'}",
        ),
        _presence_check(
            "git",
            bool(values.get("git_version")),
            values.get("git_version") or "Git is missing",
            "Git is missing",
        ),
        _presence_check(
            "python",
            bool(values.get("python_version")),
            values.get("python_version") or "Python is missing",
            "Python is missing",
        ),
        _presence_check(
            "uv",
            bool(values.get("uv_version")),
            values.get("uv_version") or "uv is missing",
            "uv is missing",
        ),
        _disk_check(disk_path, disk_available),
        _presence_check(
            "gpu",
            gpu is not None and gpu.count > 0,
            (
                f"{gpu.count} NVIDIA GPU(s): {', '.join(gpu.models)}"
                if gpu is not None and gpu.count > 0
                else "nvidia-smi is missing, failed, or reported no GPU"
            ),
            "nvidia-smi is missing, failed, or reported no GPU",
        ),
    )
    readiness = (
        WorkerReadinessState.READY
        if all(check.status is not HealthCheckStatus.FAIL for check in checks)
        else WorkerReadinessState.FAILED
    )
    return WorkerHealthReport(
        provider_worker_id=ready.worker.id,
        provider_state=ready.worker.state,
        readiness_state=readiness,
        connection=ready.connection,
        bootstrap_version_expected=BOOTSTRAP_VERSION,
        bootstrap_version_observed=observed_bootstrap,
        disk_path=disk_path,
        disk_available_bytes=disk_available,
        git_version=values.get("git_version") or None,
        python_version=values.get("python_version") or None,
        uv_version=values.get("uv_version") or None,
        gpu=gpu,
        checks=checks,
    )


def _parse_nvidia_gpu(values: dict[str, str]) -> WorkerGpuInfo | None:
    if values.get("nvidia_smi_available") != "true" or values.get("nvidia_smi_ok") != "true":
        return None
    rows = [row for row in values.get("gpu_rows", "").split("||") if row]
    models: list[str] = []
    memory_mib: list[int] = []
    drivers: list[str] = []
    for row in rows:
        parsed = next(csv.reader([row], skipinitialspace=True), [])
        if len(parsed) != 3:
            raise WorkerHealthError("Worker returned malformed nvidia-smi GPU data")
        model, memory, driver = (part.strip() for part in parsed)
        try:
            memory_value = int(memory)
        except ValueError as exc:
            raise WorkerHealthError("Worker returned malformed NVIDIA memory data") from exc
        models.append(model)
        memory_mib.append(memory_value)
        drivers.append(driver)
    if not rows:
        return WorkerGpuInfo(count=0)
    unique_drivers = tuple(dict.fromkeys(drivers))
    return WorkerGpuInfo(
        count=len(rows),
        models=tuple(models),
        memory_mib=tuple(memory_mib),
        driver_version=", ".join(unique_drivers),
        cuda_version=values.get("cuda_version") or None,
    )


def _presence_check(
    name: str,
    successful: bool,
    success_detail: str,
    failure_detail: str,
) -> WorkerHealthCheck:
    return WorkerHealthCheck(
        name=name,
        status=HealthCheckStatus.PASS if successful else HealthCheckStatus.FAIL,
        detail=success_detail if successful else failure_detail,
    )


def _disk_check(path: str | None, available_bytes: int | None) -> WorkerHealthCheck:
    displayed_path = path or _DEFAULT_DISK_PATH
    if available_bytes is None or available_bytes <= 0:
        return WorkerHealthCheck(
            name="disk",
            status=HealthCheckStatus.FAIL,
            detail=f"could not inspect {displayed_path}",
        )
    if available_bytes < _EMBEDDING_SIZE_BYTES:
        return WorkerHealthCheck(
            name="disk",
            status=HealthCheckStatus.WARN,
            detail=(
                f"{available_bytes} bytes available at {displayed_path}; this is less than the "
                "approximately 20 GiB expected embedding set before environments/checkpoints"
            ),
        )
    return WorkerHealthCheck(
        name="disk",
        status=HealthCheckStatus.PASS,
        detail=f"{available_bytes} bytes available at {displayed_path}",
    )


def _optional_nonnegative_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise WorkerHealthError("Worker returned malformed disk availability data") from exc
    if parsed < 0:
        raise WorkerHealthError("Worker returned negative disk availability")
    return parsed


def _require_supported_accelerator(worker: Worker) -> None:
    gpu_type = worker.gpu_type or "unknown"
    if "NVIDIA" not in gpu_type.upper():
        raise UnsupportedAcceleratorError(
            f"Worker {worker.id} uses unsupported accelerator {gpu_type!r}; Phase 4 readiness "
            "supports NVIDIA workers through nvidia-smi and will not mark this worker READY"
        )
