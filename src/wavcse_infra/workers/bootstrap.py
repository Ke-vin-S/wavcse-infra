"""Idempotent worker bootstrap and normalized readiness health checks."""

from __future__ import annotations

import csv
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Literal

from wavcse_infra.app_config import (
    APP_CONFIG_TIMEOUT_SECONDS,
    AppConfigEntry,
    AppConfigOutcome,
    apply_worker_app_config,
)
from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    SshCommandError,
    UnsupportedAcceleratorError,
    WorkerBootstrapError,
    WorkerHealthError,
)
from wavcse_infra.models import (
    HealthCheckStatus,
    ProviderKind,
    Worker,
    WorkerGpuInfo,
    WorkerHealthCheck,
    WorkerHealthReport,
    WorkerReadinessState,
)
from wavcse_infra.redaction import redact
from wavcse_infra.state import WorkerStateStore
from wavcse_infra.workers.ssh import (
    SshCommandResult,
    SshExecutor,
    SshWaitResult,
    WorkerSshWaiter,
)

BOOTSTRAP_VERSION = "1"
_BOOTSTRAP_COMPLETION_KEY = "wavcse_bootstrap_complete"
_HEALTH_SCHEMA_KEY = "wavcse_health_schema"
_HEALTH_SCHEMA_VERSION = "1"
_EPHEMERAL_DISK_PATH = "/"
_DEFAULT_VOLUME_MOUNT_PATH = "/workspace"
_EMBEDDING_SIZE_BYTES = 20 * 1024**3
_DIAGNOSTIC_LIMIT = 500


class WorkerBootstrapper:
    """Turn a provider-running NVIDIA worker into a checked READY worker.

    `static_root` selects the reviewed bootstrap profile. It is `None` for a provisioned
    worker, which runs the `system` profile and installs packages and `uv` exactly as
    before. For an operator-declared static host it names the one directory inside that
    account that this tool confines every write to, and bootstrap installs nothing.
    """

    def __init__(
        self,
        waiter: WorkerSshWaiter,
        executor: SshExecutor,
        state_store: WorkerStateStore,
        ssh_config: SshConfig,
        *,
        static_root: str | None = None,
    ) -> None:
        self._waiter = waiter
        self._executor = executor
        self._state = state_store
        self._config = ssh_config
        self._static_root = static_root

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
            result = self._executor.run_checked(
                ready.connection,
                _script_command("bootstrap.sh", BOOTSTRAP_VERSION, *self._bootstrap_profile()),
                input_text=load_worker_script("bootstrap.sh"),
                timeout_seconds=(
                    self._config.bootstrap_timeout_seconds
                    if command_timeout_seconds is None
                    else command_timeout_seconds
                ),
            )
        except SshCommandError as exc:
            raise WorkerBootstrapError(
                f"Bootstrap failed on {_provider_label(ready.worker)} worker {worker_id}: {exc}"
            ) from exc
        _require_bootstrap_completion(ready, result)
        self._state.mark_bootstrapped(worker_id, BOOTSTRAP_VERSION)
        return self._health(ready, command_timeout_seconds=command_timeout_seconds)

    def _bootstrap_profile(self) -> tuple[str, str]:
        """Return the profile and confined root `bootstrap.sh` receives as argv."""

        if self._static_root is None:
            return "system", ""
        return "static", self._static_root

    def _state_directory_argument(self) -> str:
        """Return the worker-side state directory, or empty to accept the script default.

        An empty argument is how the `system` profile keeps using
        `${HOME}/.local/state/wavcse-worker` without this controller having to guess the
        worker's home directory; the `static` profile owns a directory inside its root.
        """

        return "" if self._static_root is None else f"{self._static_root}/state"

    def install_app_config(
        self,
        worker_id: str,
        *,
        app: str | None = None,
        target: Literal["controller", "worker"] = "worker",
        confirm: Callable[[AppConfigEntry], bool] | None = None,
        wait_timeout_seconds: float | None = None,
        command_timeout_seconds: float | None = None,
    ) -> tuple[AppConfigOutcome, ...]:
        """Mirror this repository's application configuration onto one worker."""

        ready = self._waiter.wait(worker_id, timeout_seconds=wait_timeout_seconds)
        return apply_worker_app_config(
            self._executor,
            ready.connection,
            app=app,
            target=target,
            confirm=confirm,
            timeout_seconds=(
                min(self._config.command_timeout_seconds, APP_CONFIG_TIMEOUT_SECONDS)
                if command_timeout_seconds is None
                else command_timeout_seconds
            ),
        )

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
        disk_path, required_mount_path = self._health_storage_paths(ready.worker)
        try:
            result = self._executor.run_checked(
                ready.connection,
                _script_command(
                    "health-check.sh",
                    BOOTSTRAP_VERSION,
                    disk_path,
                    required_mount_path or "",
                    self._state_directory_argument(),
                ),
                input_text=load_worker_script("health-check.sh"),
                timeout_seconds=(
                    self._config.command_timeout_seconds
                    if command_timeout_seconds is None
                    else command_timeout_seconds
                ),
            )
        except SshCommandError as exc:
            raise WorkerHealthError(
                f"Health inspection failed on {_provider_label(ready.worker)} worker "
                f"{ready.worker.id}: {exc}"
            ) from exc
        try:
            report = parse_health_output(
                ready,
                result.stdout,
                uv_required=self._static_root is None,
            )
        except WorkerHealthError as exc:
            raise WorkerHealthError(f"{exc}; {_remote_diagnostics(result)}") from exc
        if report.gpu is not None and report.gpu.count > 0:
            self._state.mark_gpu_healthy(ready.worker.id)
        self._state.record_health(report)
        return report

    def _health_storage_paths(self, worker: Worker) -> tuple[str, str | None]:
        """Select execution storage and any provider-requested mount independently."""

        if self._static_root is not None:
            # A declared host has no provider-attached volume, and its whole writable extent
            # is the directory this tool confines itself to. Reporting `/` would describe a
            # filesystem the recorded job cannot write freely, and inventing a mount would
            # claim a mount that does not exist.
            return self._static_root, None
        volume_requested = (worker.volume_gb or 0) > 0 or worker.network_volume_id is not None
        if not volume_requested:
            return _EPHEMERAL_DISK_PATH, None
        mount_path = worker.volume_mount_path or _DEFAULT_VOLUME_MOUNT_PATH
        return mount_path, mount_path


def load_worker_script(name: str) -> str:
    """Load a reviewed worker script from a checkout or an installed wheel."""

    if name not in {"bootstrap.sh", "health-check.sh"}:
        raise ValueError(f"Unknown worker script: {name}")
    repository_script = Path(__file__).resolve().parents[3] / "worker" / name
    if repository_script.is_file():
        return repository_script.read_text(encoding="utf-8")
    packaged_script = resources.files("wavcse_infra").joinpath("worker", name)
    return packaged_script.read_text(encoding="utf-8")


def _script_command(name: str, *arguments: str) -> tuple[str, ...]:
    """Run a reviewed script supplied on stdin over direct, non-interactive SSH."""

    return ("bash", "-s", "--", *arguments)


def _require_bootstrap_completion(ready: SshWaitResult, result: SshCommandResult) -> None:
    expected = f"{_BOOTSTRAP_COMPLETION_KEY}\t{BOOTSTRAP_VERSION}"
    if expected not in result.stdout.splitlines():
        raise WorkerBootstrapError(
            f"Bootstrap command on {_provider_label(ready.worker)} worker {ready.worker.id} "
            f"exited successfully without completion marker version {BOOTSTRAP_VERSION}; "
            f"{_remote_diagnostics(result)}"
        )


def parse_health_output(
    ready: SshWaitResult,
    output: str,
    *,
    uv_required: bool = True,
) -> WorkerHealthReport:
    """Parse the small tab-separated worker protocol into normalized health data.

    `uv_required` is `False` on a declared static host, where this tool installs nothing:
    `uv` is consumed only by a job specification's own argv, never by the reviewed runner,
    so its absence there is a warning rather than a failure to become READY.
    """

    lines = output.splitlines()
    schema_lines = [
        (index, line.partition("\t")[2])
        for index, line in enumerate(lines)
        if line.partition("\t")[0] == _HEALTH_SCHEMA_KEY
    ]
    if not schema_lines:
        raise WorkerHealthError(
            f"Worker {ready.worker.id} returned health output without schema version 1"
        )
    if len(schema_lines) != 1:
        raise WorkerHealthError(
            f"Worker {ready.worker.id} returned multiple health schema declarations"
        )
    protocol_start, schema_version = schema_lines[0]
    if schema_version != _HEALTH_SCHEMA_VERSION:
        raise WorkerHealthError(
            f"Worker {ready.worker.id} returned unsupported health schema version "
            f"{schema_version or 'missing'}"
        )

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
    disk_inspection_ok = _optional_bool(values.get("disk_inspection_ok"))
    gpu = _parse_nvidia_gpu(values)
    checks = [
        WorkerHealthCheck(
            name="provider",
            status=HealthCheckStatus.PASS,
            # The worker's own provider, not a hardcoded RunPod: a declared static host must
            # never report a provider it does not belong to.
            detail=f"{_provider_label(ready.worker)} state is {ready.worker.state.value}",
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
            warn_when_missing=not uv_required,
        ),
        _disk_check(disk_path, disk_available, disk_inspection_ok),
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
    ]
    volume_check = _volume_mount_check(ready.worker, values)
    if volume_check is not None:
        checks.append(volume_check)
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
        checks=tuple(checks),
    )


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
    *,
    warn_when_missing: bool = False,
) -> WorkerHealthCheck:
    """Report a tool's presence, or its absence as a warning where it is not required."""

    if successful:
        return WorkerHealthCheck(
            name=name,
            status=HealthCheckStatus.PASS,
            detail=success_detail,
        )
    return WorkerHealthCheck(
        name=name,
        status=HealthCheckStatus.WARN if warn_when_missing else HealthCheckStatus.FAIL,
        detail=failure_detail,
    )


def _provider_label(worker: Worker) -> str:
    """Name the worker's provider the way the operator configured it."""

    return "RunPod" if worker.provider is ProviderKind.RUNPOD else worker.provider.value


def _volume_mount_check(
    worker: Worker,
    values: dict[str, str],
) -> WorkerHealthCheck | None:
    kind: str | None = None
    if worker.network_volume_id is not None:
        kind = "network volume"
    elif (worker.volume_gb or 0) > 0:
        kind = "persistent volume"
    if kind is None:
        return None

    expected_path = worker.volume_mount_path or _DEFAULT_VOLUME_MOUNT_PATH
    observed_path = values.get("required_mount_path") or None
    present = _optional_bool(values.get("required_mount_present"))
    if observed_path != expected_path:
        return WorkerHealthCheck(
            name="volume",
            status=HealthCheckStatus.FAIL,
            detail=f"could not inspect required {kind} mount at {expected_path}",
        )
    if present is not True:
        return WorkerHealthCheck(
            name="volume",
            status=HealthCheckStatus.FAIL,
            detail=f"required {kind} is not mounted at {expected_path}",
        )
    return WorkerHealthCheck(
        name="volume",
        status=HealthCheckStatus.PASS,
        detail=f"required {kind} is mounted at {expected_path}",
    )


def _disk_check(
    path: str | None,
    available_bytes: int | None,
    inspection_ok: bool | None,
) -> WorkerHealthCheck:
    displayed_path = path or _EPHEMERAL_DISK_PATH
    if inspection_ok is not True or available_bytes is None or available_bytes <= 0:
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


def _optional_bool(value: str | None) -> bool | None:
    if value is None or not value.strip():
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise WorkerHealthError("Worker returned malformed boolean health data")


def _require_supported_accelerator(worker: Worker) -> None:
    gpu_type = worker.gpu_type or "unknown"
    if "NVIDIA" not in gpu_type.upper():
        raise UnsupportedAcceleratorError(
            f"Worker {worker.id} uses unsupported accelerator {gpu_type!r}; Phase 4 readiness "
            "supports NVIDIA workers through nvidia-smi and will not mark this worker READY"
        )
