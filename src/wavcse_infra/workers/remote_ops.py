"""Bounded file sync and detached commands for a named SSH-capable worker.

The caller supplies a worker identity, not an arbitrary SSH host. Endpoint,
credentials and host-key policy come from the existing SshExecutor.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath

from wavcse_infra.errors import SshConfigurationError, SshTransferError
from wavcse_infra.models import WorkerConnectionInfo
from wavcse_infra.redaction import redact
from wavcse_infra.workers.ssh import SshExecutor


def worker_path(value: str) -> str:
    """Validate an absolute worker path without ambiguous or shell-control segments."""
    if not value or not value.startswith("/") or value == "/":
        raise SshConfigurationError("worker file path must be an absolute path below /")
    if any(character in value for character in "\x00\r\n"):
        raise SshConfigurationError("worker file path contains a control character")
    segments = value.rstrip("/").split("/")[1:]
    if any(segment in ("", ".", "..") for segment in segments):
        raise SshConfigurationError("worker file path has an empty or traversal segment")
    return value


def require_within_root(path: str, root: str) -> None:
    """Keep new static-host writes beneath the declared operator-owned root."""
    worker_path(path)
    if path.rstrip("/") != root and not path.startswith(root + "/"):
        raise SshConfigurationError(
            f"worker path {path!r} lies outside the declared static-host root {root!r}"
        )


_REMOTE_ROOT_CHECK = (
    "import os,sys;"
    "root=os.path.realpath(sys.argv[1]);"
    "path=os.path.realpath(sys.argv[2]);"
    "sys.exit(0 if path==root or path.startswith(root+os.sep) else 3)"
)


def require_resolved_within_root(
    executor: SshExecutor, connection: WorkerConnectionInfo, path: str, root: str
) -> None:
    """Reject a static-host path whose existing symlink ancestors escape root."""
    require_within_root(path, root)
    result = executor.run(connection, ("python3", "-c", _REMOTE_ROOT_CHECK, root, path))
    if result.exit_code != 0:
        raise SshConfigurationError(
            f"worker path {path!r} cannot be verified beneath the declared static-host root"
        )


def detached_command(
    checkout: str, argv: Sequence[str], log_path: str, env_file: str | None = None
) -> str:
    """Build the remote shell command; only paths and argv, never secret values."""
    worker_path(checkout)
    worker_path(log_path)
    if env_file is not None:
        worker_path(env_file)
    if not argv or any(
        not isinstance(token, str) or not token or any(char in token for char in "\x00\r\n")
        for token in argv
    ):
        raise SshConfigurationError("detached worker command requires a safe non-empty argv")
    prefix = f"cd {shlex.quote(checkout)}"
    if env_file is not None:
        prefix += f" && set -a && . {shlex.quote(env_file)} && set +a"
    prefix += f" && mkdir -p {shlex.quote(str(PurePosixPath(log_path).parent))}"
    command = shlex.join(argv)
    return (
        f"{prefix} && (setsid nohup {command} > {shlex.quote(log_path)} "
        "2>&1 < /dev/null & echo started $!)"
    )


def write_file_command(path: str) -> tuple[str, ...]:
    """Feed file contents over SSH stdin, never as a shell argument."""
    worker_path(path)
    parent = str(PurePosixPath(path).parent)
    return ("sh", "-c", f"mkdir -p {shlex.quote(parent)} && cat > {shlex.quote(path)}")


class WorkerSync:
    """Transfer directory bytes via rsync over the worker's pinned SSH channel."""

    def __init__(
        self,
        executor: SshExecutor,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._executor = executor
        self._runner = runner

    def transfer(
        self,
        connection: WorkerConnectionInfo,
        *,
        direction: str,
        source: str,
        destination: str,
        timeout_seconds: float,
        static_root: str | None = None,
    ) -> str:
        """Push controller→worker or pull worker→controller; never delete destination files."""
        if direction not in ("push", "pull"):
            raise SshConfigurationError("sync direction must be push or pull")
        if timeout_seconds <= 0:
            raise SshConfigurationError("sync timeout must be positive")
        local = source if direction == "push" else destination
        remote = destination if direction == "push" else source
        worker_path(remote)
        if not os.path.isabs(local) or any(character in local for character in "\x00\r\n"):
            raise SshConfigurationError("controller sync path must be absolute and unambiguous")
        if direction == "push" and not os.path.exists(source):
            raise SshConfigurationError(f"controller sync source does not exist: {source}")
        if static_root is not None:
            require_resolved_within_root(self._executor, connection, remote, static_root)

        if direction == "push":
            parent = (
                remote.rstrip("/") if os.path.isdir(source) else str(PurePosixPath(remote).parent)
            )
            self._executor.run_checked(
                connection, ("mkdir", "-p", parent), timeout_seconds=timeout_seconds
            )

        ssh_argv = self._executor.argv(connection, ())
        # SshExecutor is the authority for key, host-key, endpoint and timeout flags.
        # Remove only its final `-- destination`; rsync appends the same worker itself.
        ssh_command = shlex.join(ssh_argv[:-2])
        host = f"[{connection.host}]" if ":" in connection.host else connection.host
        target = f"{connection.username}@{host}:{shlex.quote(remote)}"
        origin, arrival = (local, target) if direction == "push" else (target, local)
        command = ["rsync", "-az", "--info=stats1", "-e", ssh_command, "--", origin, arrival]
        try:
            completed = self._runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise SshTransferError(
                f"Worker {connection.provider_worker_id} sync {direction} exceeded "
                f"{timeout_seconds:g} seconds; remote outcome is unknown"
            ) from exc
        except OSError as exc:
            raise SshTransferError(
                f"Worker {connection.provider_worker_id} sync {direction} could not start: "
                f"{redact(exc)}"
            ) from exc
        if completed.returncode != 0:
            raise SshTransferError(
                f"Worker {connection.provider_worker_id} sync {direction} failed "
                f"(exit {completed.returncode}): {redact(completed.stderr.strip()[:400])}"
            )
        return completed.stdout
