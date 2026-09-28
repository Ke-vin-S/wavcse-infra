import subprocess
from pathlib import Path

import pytest

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    ProviderUnavailableError,
    SshAuthenticationError,
    SshCommandError,
    SshCommandTimeoutError,
    SshConfigurationError,
    SshConnectionError,
    SshEndpointUnavailableError,
    SshHostKeyError,
    SshReadinessTimeoutError,
)
from wavcse_infra.models import Worker, WorkerConnectionInfo, WorkerState
from wavcse_infra.workers.ssh import SshExecutor, WorkerSshWaiter


class StateRecorder:
    def __init__(self) -> None:
        self.observed: list[Worker] = []
        self.ssh_ready: list[tuple[Worker, WorkerConnectionInfo]] = []

    def observe(self, worker: Worker):
        self.observed.append(worker)

    def mark_ssh_ready(self, worker: Worker, connection: WorkerConnectionInfo):
        self.ssh_ready.append((worker, connection))


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _connection(**overrides: object) -> WorkerConnectionInfo:
    values: dict[str, object] = {
        "provider_worker_id": "pod-123",
        "kind": "direct",
        "host": "203.0.113.9",
        "port": 30222,
        "username": "root",
    }
    values.update(overrides)
    return WorkerConnectionInfo.model_validate(values)


def _worker(state: WorkerState = WorkerState.RUNNING, *, connection: bool = True) -> Worker:
    return Worker(
        id="pod-123",
        state=state,
        native_status=state.value,
        gpu_type="NVIDIA RTX A4000",
        gpu_count=1,
        ssh_direct=_connection() if connection else None,
    )


def _config(tmp_path: Path) -> SshConfig:
    private_key = tmp_path / "worker-key"
    private_key.write_text("unit-test-key", encoding="utf-8")
    private_key.chmod(0o600)
    return SshConfig(
        private_key=private_key,
        known_hosts_file=tmp_path / "state" / "known_hosts",
        connect_timeout_seconds=3,
        command_timeout_seconds=10,
        readiness_timeout_seconds=10,
        poll_interval_seconds=1,
        max_poll_interval_seconds=2,
    )


def test_ssh_argv_uses_mapped_port_identity_and_dedicated_known_hosts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    executor = SshExecutor(config)

    argv = executor.argv(_connection(), ("printf", "%s", "hello world"))

    assert argv[:5] == ("ssh", "-F", "/dev/null", "-i", str(config.private_key))
    assert argv[argv.index("-p") + 1] == "30222"
    assert "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert f"UserKnownHostsFile={config.known_hosts_file}" in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv
    assert argv[-2] == "root@203.0.113.9"
    assert argv[-1] == "printf %s 'hello world'"
    assert config.known_hosts_file.stat().st_mode & 0o777 == 0o600


def test_ssh_run_is_argv_based_captures_streams_and_exit_code(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 7, stdout="standard output", stderr="standard error"
        )

    result = SshExecutor(_config(tmp_path), runner=runner).run(
        _connection(),
        ("false",),
    )

    assert result.exit_code == 7
    assert result.stdout == "standard output"
    assert result.stderr == "standard error"
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["timeout"] == 10


def test_ssh_rejects_insecure_private_key_permissions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.private_key is not None
    config.private_key.chmod(0o644)

    with pytest.raises(SshConfigurationError, match="0600 or stricter"):
        SshExecutor(config).argv(_connection(), ("true",))


def test_ssh_rejects_symlink_known_hosts_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.known_hosts_file.parent.mkdir(mode=0o700)
    target = tmp_path / "unrelated-file"
    target.write_text("preserve me", encoding="utf-8")
    config.known_hosts_file.symlink_to(target)

    with pytest.raises(SshConfigurationError, match="must not be a symlink"):
        SshExecutor(config).argv(_connection(), ("true",))

    assert target.read_text(encoding="utf-8") == "preserve me"


def test_ssh_command_timeout_is_actionable(tmp_path: Path) -> None:
    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.TimeoutExpired(argv, 4)

    with pytest.raises(SshCommandTimeoutError, match="exceeded 4 seconds"):
        SshExecutor(_config(tmp_path), runner=runner).run(
            _connection(),
            ("sleep", "10"),
            timeout_seconds=4,
        )


@pytest.mark.parametrize(
    ("stderr", "expected_error"),
    [
        ("Permission denied (publickey).", SshAuthenticationError),
        ("Host key verification failed.", SshHostKeyError),
    ],
)
def test_ssh_classifies_authentication_and_host_key_failures(
    tmp_path: Path,
    stderr: str,
    expected_error: type[Exception],
) -> None:
    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(argv, 255, stdout="", stderr=stderr)

    with pytest.raises(expected_error):
        SshExecutor(_config(tmp_path), runner=runner).run_checked(
            _connection(),
            ("true",),
        )


def test_ssh_failure_redacts_authorization_header(tmp_path: Path) -> None:
    secret = "ssh-error-secret"

    def runner(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=f"Authorization: Bearer {secret}",
        )

    with pytest.raises(SshCommandError) as captured:
        SshExecutor(_config(tmp_path), runner=runner).run_checked(
            _connection(),
            ("false",),
        )

    assert secret not in str(captured.value)
    assert "<redacted>" in str(captured.value)


def test_wait_ssh_refreshes_endpoint_and_succeeds(tmp_path: Path) -> None:
    workers = iter((_worker(connection=False), _worker()))

    class Provider:
        def get_worker(self, worker_id: str) -> Worker:
            assert worker_id == "pod-123"
            return next(workers)

    probes: list[WorkerConnectionInfo] = []

    class Executor:
        def probe(self, connection: WorkerConnectionInfo) -> None:
            probes.append(connection)

    clock = Clock()
    state = StateRecorder()
    result = WorkerSshWaiter(
        Provider(),
        Executor(),
        state,
        _config(tmp_path),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    ).wait("pod-123")

    assert result.connection.port == 30222
    assert probes == [result.connection]
    assert state.ssh_ready == [(result.worker, result.connection)]


def test_wait_ssh_falls_back_to_proxy_when_direct_connection_is_not_ready(
    tmp_path: Path,
) -> None:
    proxy = _connection(
        kind="proxy",
        host="ssh.runpod.io",
        port=22,
        username="pod-123-route",
    )
    worker = _worker().model_copy(update={"ssh_proxy": proxy})

    class Provider:
        def get_worker(self, worker_id: str) -> Worker:
            assert worker_id == "pod-123"
            return worker

    probes: list[str] = []

    class Executor:
        def probe(self, connection: WorkerConnectionInfo) -> None:
            probes.append(connection.kind)
            if connection.kind == "direct":
                raise SshConnectionError("connection refused")

    result = WorkerSshWaiter(Provider(), Executor(), StateRecorder(), _config(tmp_path)).wait(
        "pod-123"
    )

    assert probes == ["direct", "proxy"]
    assert result.connection.kind == "proxy"


def test_wait_ssh_times_out_with_last_missing_endpoint_failure(tmp_path: Path) -> None:
    class Provider:
        def get_worker(self, worker_id: str) -> Worker:
            del worker_id
            return _worker(connection=False)

    class Executor:
        def probe(self, connection: WorkerConnectionInfo) -> None:
            raise AssertionError(f"unexpected probe: {connection}")

    clock = Clock()
    with pytest.raises(SshReadinessTimeoutError, match="has not published an SSH endpoint"):
        WorkerSshWaiter(
            Provider(),
            Executor(),
            StateRecorder(),
            _config(tmp_path),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ).wait("pod-123", timeout_seconds=3)


def test_wait_ssh_stops_when_worker_terminates(tmp_path: Path) -> None:
    workers = iter((_worker(WorkerState.STARTING, connection=False), _worker(WorkerState.STOPPED)))

    class Provider:
        def get_worker(self, worker_id: str) -> Worker:
            del worker_id
            return next(workers)

    class Executor:
        def probe(self, connection: WorkerConnectionInfo) -> None:
            raise AssertionError(f"unexpected probe: {connection}")

    clock = Clock()
    with pytest.raises(SshEndpointUnavailableError, match="reached STOPPED"):
        WorkerSshWaiter(
            Provider(),
            Executor(),
            StateRecorder(),
            _config(tmp_path),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        ).wait("pod-123")


def test_wait_ssh_tolerates_transient_provider_read_failure(tmp_path: Path) -> None:
    calls = 0

    class Provider:
        def get_worker(self, worker_id: str) -> Worker:
            nonlocal calls
            assert worker_id == "pod-123"
            calls += 1
            if calls == 1:
                raise ProviderUnavailableError("temporary catalog failure")
            return _worker()

    class Executor:
        def probe(self, connection: WorkerConnectionInfo) -> None:
            assert connection.port == 30222

    clock = Clock()
    result = WorkerSshWaiter(
        Provider(),
        Executor(),
        StateRecorder(),
        _config(tmp_path),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    ).wait("pod-123")

    assert result.worker.state is WorkerState.RUNNING
    assert calls == 2
