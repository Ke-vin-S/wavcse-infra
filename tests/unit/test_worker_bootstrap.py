import re
from pathlib import Path

import pytest

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
    WorkerConnectionInfo,
    WorkerReadinessState,
    WorkerState,
)
from wavcse_infra.workers.bootstrap import (
    BOOTSTRAP_VERSION,
    WorkerBootstrapper,
    load_worker_script,
    parse_health_output,
)
from wavcse_infra.workers.ssh import SshCommandResult, SshWaitResult

HEALTHY_OUTPUT = """\
bootstrap_version=invalid-format
"""


def _health_output(*, gpu_rows: str = "NVIDIA RTX A4000, 16376, 550.54.15") -> str:
    return "\n".join(
        (
            "wavcse_health_schema\t1",
            f"bootstrap_version\t{BOOTSTRAP_VERSION}",
            f"expected_bootstrap_version\t{BOOTSTRAP_VERSION}",
            "git_version\tgit version 2.43.0",
            "python_version\tPython 3.12.3",
            "uv_version\tuv 0.10.9",
            "disk_path\t/workspace",
            "disk_available_bytes\t53687091200",
            "nvidia_smi_available\ttrue",
            "nvidia_smi_ok\ttrue",
            f"gpu_rows\t{gpu_rows}",
            "cuda_version\t12.8",
            "",
        )
    )


def _connection() -> WorkerConnectionInfo:
    return WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="direct",
        host="203.0.113.9",
        port=30222,
        username="root",
    )


def _worker(gpu_type: str = "NVIDIA RTX A4000") -> Worker:
    return Worker(
        id="pod-123",
        state=WorkerState.RUNNING,
        native_status="RUNNING",
        gpu_type=gpu_type,
        gpu_count=1,
        volume_mount_path="/workspace",
        ssh_direct=_connection(),
    )


class Waiter:
    def __init__(self, worker: Worker | None = None) -> None:
        self.result = SshWaitResult(worker=worker or _worker(), connection=_connection())
        self.calls: list[tuple[str, float | None]] = []

    def wait(self, worker_id: str, *, timeout_seconds: float | None = None) -> SshWaitResult:
        self.calls.append((worker_id, timeout_seconds))
        return self.result


class StateRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def mark_bootstrapped(self, worker_id: str, version: str) -> None:
        self.events.append(("bootstrapped", (worker_id, version)))

    def mark_gpu_healthy(self, worker_id: str) -> None:
        self.events.append(("gpu", worker_id))

    def record_health(self, report: object) -> None:
        self.events.append(("health", report))


class Executor:
    def __init__(self, health_output: str, *, health_stderr: str = "") -> None:
        self.health_output = health_output
        self.health_stderr = health_stderr
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        assert connection.provider_worker_id == "pod-123"
        self.calls.append((remote_argv, input_text, timeout_seconds))
        is_health = "nvidia-smi" in " ".join(remote_argv)
        stdout = (
            self.health_output if is_health else f"wavcse_bootstrap_complete\t{BOOTSTRAP_VERSION}\n"
        )
        return SshCommandResult(
            exit_code=0,
            stdout=stdout,
            stderr=self.health_stderr if is_health else "",
        )


class ProxyDropsStdinExecutor:
    """Model a successful RunPod proxy exec channel that does not forward stdin."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        del timeout_seconds
        assert connection.kind == "proxy"
        self.calls.append((remote_argv, input_text))
        if input_text is not None:
            return SshCommandResult(exit_code=0, stdout="", stderr="")
        stdout = (
            _health_output()
            if "nvidia-smi" in " ".join(remote_argv)
            else f"wavcse_bootstrap_complete\t{BOOTSTRAP_VERSION}\n"
        )
        return SshCommandResult(exit_code=0, stdout=stdout, stderr="")


def _config(tmp_path: Path) -> SshConfig:
    return SshConfig(
        private_key=tmp_path / "not-used-by-fake",
        known_hosts_file=tmp_path / "known_hosts",
        command_timeout_seconds=30,
        bootstrap_timeout_seconds=120,
    )


def test_bootstrap_runs_versioned_script_then_health_and_reaches_ready(tmp_path: Path) -> None:
    waiter = Waiter()
    executor = Executor(_health_output())
    state = StateRecorder()
    bootstrapper = WorkerBootstrapper(waiter, executor, state, _config(tmp_path))

    report = bootstrapper.bootstrap("pod-123", wait_timeout_seconds=20)

    assert report.ready
    assert report.readiness_state is WorkerReadinessState.READY
    assert report.gpu is not None
    assert report.gpu.count == 1
    assert report.gpu.models == ("NVIDIA RTX A4000",)
    assert report.gpu.memory_mib == (16376,)
    assert report.gpu.driver_version == "550.54.15"
    assert report.gpu.cuda_version == "12.8"
    assert report.disk_available_bytes == 53687091200
    assert waiter.calls == [("pod-123", 20)]
    assert executor.calls[0][0][:2] == ("bash", "-c")
    assert "set -Eeuo pipefail" in executor.calls[0][0][2]
    assert executor.calls[0][0][3:] == ("wavcse-bootstrap.sh", BOOTSTRAP_VERSION)
    assert executor.calls[0][1] is None
    assert executor.calls[0][2] == 120
    assert executor.calls[1][0][:2] == ("bash", "-c")
    assert "wavcse_health_schema" in executor.calls[1][0][2]
    assert executor.calls[1][0][3:] == (
        "wavcse-health-check.sh",
        BOOTSTRAP_VERSION,
        "/workspace",
    )
    assert executor.calls[1][1] is None
    assert [event[0] for event in state.events] == ["bootstrapped", "gpu", "health"]


def test_bootstrap_does_not_depend_on_proxy_forwarding_stdin(tmp_path: Path) -> None:
    proxy = _connection().model_copy(
        update={"kind": "proxy", "host": "ssh.runpod.io", "port": 22, "username": "pod-route"}
    )
    worker = _worker().model_copy(update={"ssh_direct": None, "ssh_proxy": proxy})
    waiter = Waiter(worker)
    waiter.result = SshWaitResult(worker=worker, connection=proxy)
    executor = ProxyDropsStdinExecutor()
    state = StateRecorder()

    report = WorkerBootstrapper(waiter, executor, state, _config(tmp_path)).bootstrap("pod-123")

    assert report.ready
    assert all(input_text is None for _, input_text in executor.calls)
    assert [event[0] for event in state.events] == ["bootstrapped", "gpu", "health"]


def test_bootstrap_failure_does_not_run_health_or_mark_ready(tmp_path: Path) -> None:
    class FailingExecutor(Executor):
        def run_checked(self, *args: object, **kwargs: object) -> SshCommandResult:
            del args, kwargs
            raise SshCommandError("apt-get failed")

    state = StateRecorder()
    bootstrapper = WorkerBootstrapper(
        Waiter(), FailingExecutor(_health_output()), state, _config(tmp_path)
    )

    with pytest.raises(WorkerBootstrapError, match="apt-get failed"):
        bootstrapper.bootstrap("pod-123")

    assert state.events == []


def test_bootstrap_requires_explicit_remote_completion_marker(tmp_path: Path) -> None:
    class MissingMarkerExecutor(Executor):
        def run_checked(self, *args: object, **kwargs: object) -> SshCommandResult:
            del args, kwargs
            return SshCommandResult(exit_code=0, stdout="", stderr="proxy diagnostic")

    state = StateRecorder()
    bootstrapper = WorkerBootstrapper(
        Waiter(), MissingMarkerExecutor(_health_output()), state, _config(tmp_path)
    )

    with pytest.raises(WorkerBootstrapError, match="without completion marker") as captured:
        bootstrapper.bootstrap("pod-123")

    assert "proxy diagnostic" in str(captured.value)
    assert state.events == []


def test_remote_health_nonzero_does_not_mark_ready(tmp_path: Path) -> None:
    class HealthFailureExecutor(Executor):
        def run_checked(
            self,
            connection: WorkerConnectionInfo,
            remote_argv: tuple[str, ...],
            **kwargs: object,
        ) -> SshCommandResult:
            if "nvidia-smi" in " ".join(remote_argv):
                raise SshCommandError("health script exited 7: driver unavailable")
            return super().run_checked(connection, remote_argv, **kwargs)

    state = StateRecorder()
    bootstrapper = WorkerBootstrapper(
        Waiter(), HealthFailureExecutor(_health_output()), state, _config(tmp_path)
    )

    with pytest.raises(WorkerHealthError, match="health script exited 7"):
        bootstrapper.bootstrap("pod-123")

    assert [event[0] for event in state.events] == ["bootstrapped"]


def test_health_parse_failure_does_not_mark_ready_and_reports_streams(tmp_path: Path) -> None:
    state = StateRecorder()
    executor = Executor("not a health protocol\n", health_stderr="remote health diagnostic")
    bootstrapper = WorkerBootstrapper(Waiter(), executor, state, _config(tmp_path))

    with pytest.raises(WorkerHealthError, match="without schema version 1") as captured:
        bootstrapper.bootstrap("pod-123")

    assert "remote stdout='not a health protocol'" in str(captured.value)
    assert "remote stderr='remote health diagnostic'" in str(captured.value)
    assert [event[0] for event in state.events] == ["bootstrapped"]


def test_health_parse_diagnostics_are_redacted(tmp_path: Path) -> None:
    secret = "remote-secret-that-must-not-escape"
    executor = Executor(
        "not a health protocol\n",
        health_stderr=f"Authorization: Bearer {secret}",
    )

    with pytest.raises(WorkerHealthError) as captured:
        WorkerBootstrapper(Waiter(), executor, StateRecorder(), _config(tmp_path)).health("pod-123")

    assert secret not in str(captured.value)
    assert "<redacted>" in str(captured.value)


def test_health_stderr_diagnostics_do_not_corrupt_stdout_protocol(tmp_path: Path) -> None:
    report = WorkerBootstrapper(
        Waiter(),
        Executor(_health_output(), health_stderr="non-fatal remote diagnostic"),
        StateRecorder(),
        _config(tmp_path),
    ).health("pod-123")

    assert report.ready


def test_health_with_no_visible_gpu_is_failed_and_never_ready(tmp_path: Path) -> None:
    executor = Executor(_health_output(gpu_rows=""))
    state = StateRecorder()

    report = WorkerBootstrapper(Waiter(), executor, state, _config(tmp_path)).health("pod-123")

    assert not report.ready
    assert report.readiness_state is WorkerReadinessState.FAILED
    gpu_check = next(check for check in report.checks if check.name == "gpu")
    assert gpu_check.status is HealthCheckStatus.FAIL
    assert [event[0] for event in state.events] == ["health"]


def test_health_requires_expected_bootstrap_version() -> None:
    output = _health_output().replace(
        f"bootstrap_version\t{BOOTSTRAP_VERSION}",
        "bootstrap_version\told-version",
        1,
    )

    report = parse_health_output(
        SshWaitResult(worker=_worker(), connection=_connection()),
        output,
    )

    assert not report.ready
    bootstrap_check = next(check for check in report.checks if check.name == "bootstrap")
    assert bootstrap_check.status is HealthCheckStatus.FAIL
    assert "expected version" in bootstrap_check.detail


def test_health_reports_missing_nvidia_smi() -> None:
    output = (
        _health_output(gpu_rows="")
        .replace("nvidia_smi_available\ttrue", "nvidia_smi_available\tfalse")
        .replace("nvidia_smi_ok\ttrue", "nvidia_smi_ok\tfalse")
    )

    report = parse_health_output(
        SshWaitResult(worker=_worker(), connection=_connection()),
        output,
    )

    gpu_check = next(check for check in report.checks if check.name == "gpu")
    assert gpu_check.status is HealthCheckStatus.FAIL
    assert "nvidia-smi is missing" in gpu_check.detail


def test_low_disk_is_warning_but_is_visible_and_does_not_invent_failure() -> None:
    output = _health_output().replace(
        "disk_available_bytes\t53687091200",
        "disk_available_bytes\t10737418240",
    )

    report = parse_health_output(
        SshWaitResult(worker=_worker(), connection=_connection()),
        output,
    )

    disk_check = next(check for check in report.checks if check.name == "disk")
    assert disk_check.status is HealthCheckStatus.WARN
    assert "20 GiB" in disk_check.detail
    assert report.ready


def test_non_nvidia_worker_is_never_treated_as_ready(tmp_path: Path) -> None:
    state = StateRecorder()
    with pytest.raises(UnsupportedAcceleratorError, match="supports NVIDIA workers"):
        WorkerBootstrapper(
            Waiter(_worker("AMD Instinct MI300X OAM")),
            Executor(_health_output()),
            state,
            _config(tmp_path),
        ).health("pod-123")

    assert state.events == []


def test_health_parser_accepts_valid_schema_v1() -> None:
    report = parse_health_output(
        SshWaitResult(worker=_worker(), connection=_connection()),
        _health_output(),
    )

    assert report.ready


def test_health_parser_allows_only_preamble_before_valid_protocol() -> None:
    report = parse_health_output(
        SshWaitResult(worker=_worker(), connection=_connection()),
        "RunPod proxy session ready\n" + _health_output(),
    )

    assert report.ready


def test_health_parser_rejects_missing_schema_version() -> None:
    with pytest.raises(WorkerHealthError, match="without schema version 1"):
        parse_health_output(
            SshWaitResult(worker=_worker(), connection=_connection()),
            HEALTHY_OUTPUT,
        )


def test_health_parser_rejects_unsupported_schema_version() -> None:
    with pytest.raises(WorkerHealthError, match="unsupported health schema version 2"):
        parse_health_output(
            SshWaitResult(worker=_worker(), connection=_connection()),
            _health_output().replace("wavcse_health_schema\t1", "wavcse_health_schema\t2"),
        )


def test_health_parser_rejects_malformed_protocol_field() -> None:
    with pytest.raises(WorkerHealthError, match="malformed health output"):
        parse_health_output(
            SshWaitResult(worker=_worker(), connection=_connection()),
            _health_output() + "malformed-field\n",
        )


def test_worker_scripts_are_strict_idempotent_and_contain_no_controller_secrets() -> None:
    bootstrap = load_worker_script("bootstrap.sh")
    health = load_worker_script("health-check.sh")

    assert bootstrap.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail")
    assert health.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail")
    assert "dpkg-query" in bootstrap
    assert "command -v uv" in bootstrap
    assert "mktemp" in bootstrap and "bootstrap-version" in bootstrap
    assert "wavcse_bootstrap_complete" in bootstrap
    assert "nvidia-smi --query-gpu" in health
    for forbidden in (
        "RUNPOD_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        ".ssh/id_",
    ):
        assert forbidden not in bootstrap.casefold()
        assert forbidden not in health.casefold()
    for controller_tool in ("codex", "omp", "agf"):
        assert re.search(rf"\b{controller_tool}\b", bootstrap, re.IGNORECASE) is None
        assert re.search(rf"\b{controller_tool}\b", health, re.IGNORECASE) is None
