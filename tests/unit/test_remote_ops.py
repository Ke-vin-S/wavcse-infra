"""Named-worker remote file operations preserve safety without a provider call."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wavcse_infra.errors import SshCommandError, SshConfigurationError, SshTransferError
from wavcse_infra.models import WorkerConnectionInfo
from wavcse_infra.workers.remote_ops import (
    WorkerSync,
    detached_command,
    require_resolved_within_root,
    require_within_root,
    write_file_command,
)


def connection() -> WorkerConnectionInfo:
    return WorkerConnectionInfo(
        provider_worker_id="static-lab",
        kind="direct",
        host="203.0.113.9",
        port=30222,
        username="researcher",
    )


def executor() -> Mock:
    fake = Mock()
    fake.argv.return_value = (
        "ssh",
        "-F",
        "/dev/null",
        "-i",
        "/tmp/pinned-key",
        "-p",
        "30222",
        "-o",
        "UserKnownHostsFile=/tmp/pinned-hosts",
        "-T",
        "--",
        "researcher@203.0.113.9",
    )
    return fake


def test_push_and_pull_use_the_pinned_endpoint_without_deletion(tmp_path: Path) -> None:
    source = tmp_path / "config directory"
    source.mkdir()
    completed = subprocess.CompletedProcess([], 0, "synced", "")
    runner = Mock(return_value=completed)
    ssh = executor()
    transport = WorkerSync(ssh, runner=runner)

    assert (
        transport.transfer(
            connection(),
            direction="push",
            source=str(source) + "/",
            destination="/workspace/study files/",
            timeout_seconds=30,
        )
        == "synced"
    )
    push = runner.call_args.args[0]
    assert push[:4] == ["rsync", "-az", "--info=stats1", "-e"]
    assert "UserKnownHostsFile=/tmp/pinned-hosts" in push[4]
    assert "-i /tmp/pinned-key" in push[4]
    assert push[-2] == str(source) + "/"
    assert push[-1] == "researcher@203.0.113.9:'/workspace/study files/'"
    assert "--delete" not in push
    ssh.run_checked.assert_called_once_with(
        connection(), ("mkdir", "-p", "/workspace/study files"), timeout_seconds=30
    )

    transport.transfer(
        connection(),
        direction="pull",
        source="/workspace/study files/",
        destination=str(tmp_path / "collected") + "/",
        timeout_seconds=30,
    )
    pull = runner.call_args.args[0]
    assert pull[-2] == "researcher@203.0.113.9:'/workspace/study files/'"
    assert pull[-1] == str(tmp_path / "collected") + "/"


def test_failed_remote_destination_preparation_never_starts_transfer(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.mkdir()
    ssh = executor()
    ssh.run_checked.side_effect = SshCommandError("remote directory refused")
    runner = Mock()
    with pytest.raises(SshCommandError, match="remote directory refused"):
        WorkerSync(ssh, runner=runner).transfer(
            connection(),
            direction="push",
            source=str(source) + "/",
            destination="/workspace/study/",
            timeout_seconds=10,
        )
    runner.assert_not_called()


def test_untrusted_paths_fail_before_a_remote_command(tmp_path: Path) -> None:
    ssh = executor()
    runner = Mock()
    transport = WorkerSync(ssh, runner=runner)
    for remote in ("relative/path", "/../etc/passwd", "/workspace//bad"):
        with pytest.raises(SshConfigurationError):
            transport.transfer(
                connection(),
                direction="push",
                source=str(tmp_path),
                destination=remote,
                timeout_seconds=10,
            )
    ssh.argv.assert_not_called()
    runner.assert_not_called()


def test_static_host_paths_cannot_escape_the_declared_root() -> None:
    require_within_root("/home/research/wavcse/sweeps/DG-0007/", "/home/research/wavcse")
    for path in ("/home/research/wavcse-other/run", "/tmp/run", "/home/research/wavcse/../run"):
        with pytest.raises(SshConfigurationError):
            require_within_root(path, "/home/research/wavcse")


def test_static_host_symlink_ancestors_cannot_redirect_a_write(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "data").symlink_to(outside, target_is_directory=True)
    ssh = Mock()

    def run(_connection, argv):
        completed = subprocess.run(argv, capture_output=True, check=False)
        return SimpleNamespace(exit_code=completed.returncode)

    ssh.run.side_effect = run
    require_resolved_within_root(ssh, connection(), str(root / "safe"), str(root))
    with pytest.raises(SshConfigurationError, match="cannot be verified beneath"):
        require_resolved_within_root(ssh, connection(), str(root / "data" / "run"), str(root))


def test_transport_failure_names_worker_and_ambiguous_outcome(tmp_path: Path) -> None:
    source = tmp_path / "input"
    source.write_text("evidence", encoding="utf-8")
    runner = Mock(side_effect=subprocess.TimeoutExpired("rsync", timeout=5))
    with pytest.raises(SshTransferError, match="static-lab sync push exceeded"):
        WorkerSync(executor(), runner=runner).transfer(
            connection(),
            direction="push",
            source=str(source),
            destination="/workspace/input",
            timeout_seconds=5,
        )


def test_detached_start_checks_checkout_before_claiming_success(tmp_path: Path) -> None:
    absent = tmp_path / "missing checkout"
    script = detached_command(str(absent), ["true"], str(tmp_path / "job.log"))
    completed = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode != 0
    assert "started" not in completed.stdout


def test_detached_start_reads_an_env_file_without_exposing_it(tmp_path: Path) -> None:
    checkout = tmp_path / "study checkout"
    checkout.mkdir()
    environment = tmp_path / "worker.env"
    environment.write_text("SWEEP_SAMPLE=present\n", encoding="utf-8")
    log = tmp_path / "logs" / "supervisor.log"
    script = detached_command(
        str(checkout),
        [sys.executable, "-c", "print('started by worker')"],
        str(log),
        str(environment),
    )
    assert "SWEEP_SAMPLE=present" not in script
    completed = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout.startswith("started ")


def test_write_file_streams_bytes_without_interpreting_content(tmp_path: Path) -> None:
    target = tmp_path / "state" / "DRAIN"
    marker = tmp_path / "not-executed"
    payload = f"stop; $(touch {marker})\n"
    completed = subprocess.run(
        write_file_command(str(target)),
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0
    assert target.read_text(encoding="utf-8") == payload
    assert not marker.exists()
