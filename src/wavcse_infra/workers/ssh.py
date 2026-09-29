"""Hardened OpenSSH execution and bounded worker SSH readiness polling."""

from __future__ import annotations

import math
import os
import shlex
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from wavcse_infra.config import SshConfig
from wavcse_infra.errors import (
    ProviderNotFoundError,
    ProviderUnavailableError,
    SshAuthenticationError,
    SshCommandError,
    SshCommandTimeoutError,
    SshConfigurationError,
    SshConnectionError,
    SshEndpointUnavailableError,
    SshError,
    SshHostKeyError,
    SshReadinessTimeoutError,
    StateError,
)
from wavcse_infra.models import Worker, WorkerConnectionInfo, WorkerState
from wavcse_infra.redaction import redact
from wavcse_infra.state import WorkerStateStore

_SSH_PROBE_MARKER = "wavcse_ssh_probe_complete\t1"

# SSH failures that leave the controller without the remote outcome. A bounded command
# timeout, a readiness timeout, and a dropped connection all mean the worker-side process
# may still be running, so no caller may record a terminal state from them.
_INTERRUPTED_SSH_ERRORS = (
    SshCommandTimeoutError,
    SshReadinessTimeoutError,
    SshConnectionError,
)
# SSH failures that are definitive because they describe the controller's own
# configuration rather than an unknown remote state. `SshCommandError` is handled
# separately, after the more specific `SshCommandTimeoutError` is ruled out.
_DEFINITIVE_SSH_ERRORS = (
    SshAuthenticationError,
    SshHostKeyError,
    SshConfigurationError,
    SshEndpointUnavailableError,
)


def remote_outcome_is_unknown(exc: SshError) -> bool:
    """Return whether one SSH failure leaves a remote operation's outcome unknown.

    Order matters: `SshAuthenticationError` is a connection error and
    `SshCommandTimeoutError` is a command error, so the specific terminal failures are
    tested before their parents. An unclassified SSH failure is reported as unknown,
    because silence is never evidence that a remote operation failed.
    """

    if isinstance(exc, _DEFINITIVE_SSH_ERRORS):
        return False
    if isinstance(exc, _INTERRUPTED_SSH_ERRORS):
        return True
    # A remote command that ran and reported its own failure is evidence; anything else is
    # silence, and silence is never evidence that a remote operation failed.
    return not isinstance(exc, SshCommandError)


class WorkerReader(Protocol):
    """Provider read surface needed while waiting for SSH."""

    def get_worker(self, worker_id: str) -> Worker: ...


@dataclass(frozen=True)
class SshCommandResult:
    """Captured result of one remote command."""

    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class SshWaitResult:
    """Provider worker and exact endpoint that passed an SSH probe."""

    worker: Worker
    connection: WorkerConnectionInfo


class SshExecutor:
    """Invoke the system OpenSSH client using explicit, isolated configuration."""

    def __init__(
        self,
        config: SshConfig,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._config = config
        self._runner = runner

    def argv(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: Sequence[str],
    ) -> tuple[str, ...]:
        """Build an auditable, non-interactive argv for a true SSH endpoint."""

        if connection.kind != "direct":
            raise SshEndpointUnavailableError(
                f"RunPod worker {connection.provider_worker_id} only has the basic SSH proxy; "
                "that gateway requires a PTY and cannot run reliable non-interactive commands. "
                "Automation requires a public IP with a mapped 22/tcp direct SSH endpoint."
            )
        arguments = list(self._base_argv(connection, tty_flag="-T"))
        if remote_argv:
            arguments.append(shlex.join(remote_argv))
        return tuple(arguments)

    def interactive_argv(self, connection: WorkerConnectionInfo) -> tuple[str, ...]:
        """Build an interactive argv for either direct SSH or RunPod's PTY proxy."""

        return self._base_argv(connection, tty_flag="-tt")

    def _base_argv(
        self,
        connection: WorkerConnectionInfo,
        *,
        tty_flag: str,
    ) -> tuple[str, ...]:
        """Build common explicit OpenSSH arguments without user configuration."""

        private_key = self._validated_private_key()
        known_hosts = self._prepare_known_hosts()
        destination_host = f"[{connection.host}]" if ":" in connection.host else connection.host
        destination = f"{connection.username}@{destination_host}"
        connect_timeout = max(1, math.ceil(self._config.connect_timeout_seconds))
        arguments = [
            "ssh",
            "-F",
            "/dev/null",
            "-i",
            str(private_key),
            "-p",
            str(connection.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"ConnectTimeout={connect_timeout}",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "HashKnownHosts=yes",
            "-o",
            "LogLevel=ERROR",
            tty_flag,
            "--",
            destination,
        ]
        return tuple(arguments)

    def run(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        """Run one bounded command and capture its streams and exit status."""

        timeout = (
            self._config.command_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if timeout <= 0:
            raise SshConfigurationError("SSH command timeout must be greater than zero")
        command = self.argv(connection, remote_argv)
        try:
            completed = self._runner(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshCommandTimeoutError(
                f"SSH command for worker {connection.provider_worker_id} exceeded "
                f"{timeout:g} seconds"
            ) from exc
        except FileNotFoundError as exc:
            raise SshConfigurationError(
                "OpenSSH client was not found on PATH; install the `ssh` command"
            ) from exc
        except OSError as exc:
            raise SshConnectionError(
                f"Could not start OpenSSH for worker {connection.provider_worker_id}: {redact(exc)}"
            ) from exc
        return SshCommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        """Run a command and translate a nonzero exit into an actionable domain error."""

        result = self.run(
            connection,
            remote_argv,
            input_text=input_text,
            timeout_seconds=timeout_seconds,
        )
        if result.exit_code != 0:
            raise _command_failure(connection, result)
        return result

    def run_interactive(self, connection: WorkerConnectionInfo) -> int:
        """Attach the caller's terminal to an interactive worker session."""

        command = self.interactive_argv(connection)
        try:
            completed = self._runner(command, check=False, shell=False)
        except FileNotFoundError as exc:
            raise SshConfigurationError(
                "OpenSSH client was not found on PATH; install the `ssh` command"
            ) from exc
        except OSError as exc:
            raise SshConnectionError(
                f"Could not start OpenSSH for worker {connection.provider_worker_id}: {redact(exc)}"
            ) from exc
        return completed.returncode

    def probe(self, connection: WorkerConnectionInfo) -> None:
        """Prove that a direct endpoint executed a non-interactive remote command."""

        result = self.run_checked(
            connection,
            ("printf", "%s\\n", _SSH_PROBE_MARKER),
            timeout_seconds=self._config.connect_timeout_seconds + 5,
        )
        if _SSH_PROBE_MARKER not in result.stdout.splitlines():
            detail = redact(result.stdout.strip() or result.stderr.strip() or "no remote detail")
            raise SshConnectionError(
                f"SSH to worker {connection.provider_worker_id} returned exit 0 without the "
                f"automation probe marker; remote output: {detail[:500]}"
            )

    def _validated_private_key(self) -> Path:
        private_key = self._config.private_key
        if private_key is None:
            raise SshConfigurationError(
                "ssh.private_key is not configured; set it to the controller's dedicated "
                "worker SSH private key"
            )
        if not private_key.is_file():
            raise SshConfigurationError(f"Worker SSH private key does not exist: {private_key}")
        try:
            mode = stat.S_IMODE(private_key.stat().st_mode)
        except OSError as exc:
            raise SshConfigurationError(
                f"Could not inspect worker SSH private key {private_key}: {redact(exc)}"
            ) from exc
        if mode & 0o077:
            raise SshConfigurationError(
                f"Worker SSH private key {private_key} has mode {mode:04o}; "
                "restrict it to 0600 or stricter"
            )
        return private_key

    def _prepare_known_hosts(self) -> Path:
        known_hosts = self._config.known_hosts_file
        try:
            known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            parent_mode = stat.S_IMODE(known_hosts.parent.stat().st_mode)
            if parent_mode & 0o077:
                raise SshConfigurationError(
                    f"Dedicated SSH state directory {known_hosts.parent} has mode "
                    f"{parent_mode:04o}; restrict it to 0700 or stricter"
                )
            if known_hosts.is_symlink():
                raise SshConfigurationError(
                    f"Dedicated SSH known-hosts path must not be a symlink: {known_hosts}"
                )
            descriptor = os.open(
                known_hosts,
                os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
            )
            os.close(descriptor)
            if not known_hosts.is_file():
                raise SshConfigurationError(
                    f"Dedicated SSH known-hosts path is not a regular file: {known_hosts}"
                )
            known_hosts.chmod(0o600)
        except SshConfigurationError:
            raise
        except OSError as exc:
            raise SshConfigurationError(
                f"Could not prepare dedicated SSH known-hosts file {known_hosts}: {redact(exc)}"
            ) from exc
        return known_hosts


class WorkerSshWaiter:
    """Poll provider metadata and OpenSSH until a Pod is genuinely SSH-ready."""

    def __init__(
        self,
        provider: WorkerReader,
        executor: SshExecutor,
        state_store: WorkerStateStore,
        config: SshConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._executor = executor
        self._state = state_store
        self._config = config
        self._sleep = sleep
        self._monotonic = monotonic

    def wait(
        self,
        worker_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> SshWaitResult:
        """Wait for a current provider endpoint and a successful authenticated probe."""

        timeout = (
            self._config.readiness_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if timeout <= 0:
            raise SshConfigurationError("SSH readiness timeout must be greater than zero")
        deadline = self._monotonic() + timeout
        delay = self._config.poll_interval_seconds
        last_state = WorkerState.UNKNOWN
        last_failure = "provider state has not been read"

        while True:
            try:
                worker = self._provider.get_worker(worker_id)
                last_state = worker.state
                with suppress(StateError):
                    self._state.observe(worker)
                if worker.state in {
                    WorkerState.STOPPING,
                    WorkerState.STOPPED,
                    WorkerState.TERMINATING,
                    WorkerState.DESTROYED,
                    WorkerState.ERROR,
                }:
                    raise SshEndpointUnavailableError(
                        f"RunPod worker {worker_id} reached {worker.state.value} while waiting "
                        "for SSH"
                    )
                if worker.state is WorkerState.RUNNING:
                    connections = worker_connections(worker)
                    if not connections:
                        if worker.ssh_proxy is not None:
                            last_failure = (
                                "RunPod published only its basic PTY proxy; automation requires "
                                "a public IP with a mapped 22/tcp direct SSH endpoint. "
                                "--start-ssh and exposing 22/tcp do not by themselves guarantee "
                                "a public IP on Community Cloud"
                            )
                        else:
                            last_failure = (
                                "RunPod has not published a direct SSH endpoint; ensure the Pod "
                                "has a public IP, mapped 22/tcp, sshd, and the registered "
                                "public key"
                            )
                    else:
                        endpoint_failures: list[str] = []
                        for connection in connections:
                            try:
                                self._executor.probe(connection)
                            except (
                                SshAuthenticationError,
                                SshHostKeyError,
                                SshConfigurationError,
                            ):
                                raise
                            except (SshConnectionError, SshCommandTimeoutError) as exc:
                                endpoint_failures.append(f"{connection.kind}: {redact(exc)}")
                            else:
                                with suppress(StateError):
                                    self._state.mark_ssh_ready(worker, connection)
                                return SshWaitResult(worker=worker, connection=connection)
                        last_failure = "; ".join(endpoint_failures)
                else:
                    last_failure = f"provider state is {worker.state.value}"
            except ProviderUnavailableError as exc:
                last_failure = redact(exc)
            except ProviderNotFoundError:
                raise

            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise SshReadinessTimeoutError(
                    f"RunPod worker {worker_id} did not become SSH-ready within {timeout:g} "
                    f"seconds; last provider state: {last_state.value}; "
                    f"last failure: {last_failure}"
                )
            self._sleep(min(delay, remaining))
            delay = min(delay * 2, self._config.max_poll_interval_seconds)


def select_worker_connection(worker: Worker) -> WorkerConnectionInfo | None:
    """Prefer direct SSH for an interactive shell, with the PTY proxy as fallback."""

    return worker.ssh_direct or worker.ssh_proxy


def worker_connections(worker: Worker) -> tuple[WorkerConnectionInfo, ...]:
    """Return endpoints capable of reliable non-interactive automation."""

    return (worker.ssh_direct,) if worker.ssh_direct is not None else ()


def _command_failure(
    connection: WorkerConnectionInfo,
    result: SshCommandResult,
) -> SshError:
    detail = redact(result.stderr.strip() or result.stdout.strip() or "no remote detail")[:500]
    lowered = detail.casefold()
    prefix = f"SSH to worker {connection.provider_worker_id}"
    if "permission denied" in lowered or "authentication failed" in lowered:
        return SshAuthenticationError(
            f"{prefix} rejected the configured worker key: {detail}. Verify that its public "
            "key is registered in the RunPod account and was injected when the Pod was created."
        )
    if (
        "host key verification failed" in lowered
        or "remote host identification has changed" in lowered
    ):
        return SshHostKeyError(
            f"{prefix} failed dedicated known-hosts verification: {detail}. Inspect the exact "
            "Pod endpoint before replacing its stored host key."
        )
    if result.exit_code == 255:
        return SshConnectionError(f"{prefix} could not connect: {detail}")
    return SshCommandError(
        f"Remote command on worker {connection.provider_worker_id} exited "
        f"{result.exit_code}: {detail}"
    )
