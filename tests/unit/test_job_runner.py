"""Worker-side runner: exact-commit materialization, execution, logs, and cancellation.

The runner is worker code, so it is loaded from `worker/job_runner.py` exactly as the
controller ships it, and `git` is replaced by a scripted stub on PATH. Two tests start
real detached processes because the detached-session behaviour is the property under
test; they use `sys.executable` and finish in milliseconds.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = "https://github.com/Synergy-io/wavCSE.git"
COMMIT = "a" * 40


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "worker" / "job_runner.py"
    spec = importlib.util.spec_from_file_location("wavcse_job_runner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()

FAKE_GIT_SCRIPT = """#!/usr/bin/env python3
import json, os, sys

script = json.loads(os.environ["FAKE_GIT_SCRIPT"])
arguments = sys.argv[3:]  # skip <git> -c advice.detachedHead=false
subcommand = arguments[0] if arguments else ""
entry = script.get(subcommand, {})
if entry.get("write_dirty"):
    open(os.path.join(os.getcwd(), "dirty.txt"), "w").write("dirty\\n")
if subcommand == "clone" and int(entry.get("exit_code", 0)) == 0:
    target = arguments[-1]
    os.makedirs(os.path.join(target, ".git"), exist_ok=True)
    open(os.path.join(target, ".git", "HEAD"), "w").write("ref: refs/heads/main\\n")
sys.stdout.write(entry.get("stdout", ""))
sys.stderr.write(entry.get("stderr", ""))
sys.exit(int(entry.get("exit_code", 0)))
"""


@pytest.fixture
def fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a scripted `git` stub first on PATH and return a script setter."""

    binary = tmp_path / "fake-bin"
    binary.mkdir()
    git = binary / "git"
    git.write_text(FAKE_GIT_SCRIPT, encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")

    def install(script: dict[str, dict[str, Any]]) -> None:
        encoded = json.dumps(script)
        monkeypatch.setenv("FAKE_GIT_SCRIPT", encoded)
        monkeypatch.setattr(
            runner,
            "BASELINE_ENVIRONMENT",
            {"PATH": os.environ["PATH"], "FAKE_GIT_SCRIPT": encoded},
        )

    return install


@pytest.fixture
def worker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".local/state/wavcse-worker").mkdir(parents=True)
    (home / ".local/state/wavcse-worker/bootstrap-version").write_text("1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


def _head_script(commit: str = COMMIT, *, status: str = "", clone_fails: bool = False):
    return {
        "clone": {
            "exit_code": 1 if clone_fails else 0,
            "stderr": "fatal: repository not found\n" if clone_fails else "",
        },
        "cat-file": {"exit_code": 0},
        "rev-parse": {"stdout": f"{commit}\n"},
        "status": {"stdout": status},
        "checkout": {"exit_code": 0},
        "fetch": {"exit_code": 0},
        "remote": {"exit_code": 0},
    }


def _descriptor(job_directory: Path, **overrides: Any) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "job_id": "job-0123456789abcdef",
        "job_directory": str(job_directory),
        "source": {"repository": REPOSITORY, "commit": COMMIT},
        "expected_bootstrap_version": "1",
    }
    descriptor.update(overrides)
    return descriptor


def test_resolve_within_rejects_traversal_and_absolute_paths(tmp_path: Path) -> None:
    root = str(tmp_path / "job")

    assert runner.resolve_within(root, "outputs/a.json") == f"{root}/outputs/a.json"

    for relative in ("/etc/passwd", "../escape", "a/../../b", "", "a//b", "./a"):
        with pytest.raises(runner.RunnerInputError):
            runner.resolve_within(root, relative)


def test_repository_must_be_anonymous_https() -> None:
    assert runner._validate_repository(REPOSITORY) == REPOSITORY

    for repository in (
        "git@github.com:Synergy-io/wavCSE.git",
        "https://user@github.com/Synergy-io/wavCSE.git",
        "https://github.com/x.git?ref=y",
        " http://github.com/x.git",
    ):
        with pytest.raises(runner.RunnerInputError):
            runner._validate_repository(repository)


def test_materialize_source_verifies_head_equals_requested_commit(tmp_path: Path, fake_git) -> None:
    fake_git(_head_script())

    head = runner.materialize_source(REPOSITORY, COMMIT, str(tmp_path / "source"))

    assert head == COMMIT


def test_materialize_source_fails_when_head_is_a_different_revision(
    tmp_path: Path, fake_git
) -> None:
    fake_git(_head_script(commit="b" * 40))

    with pytest.raises(runner.RunnerError, match="refusing to execute a different revision"):
        runner.materialize_source(REPOSITORY, COMMIT, str(tmp_path / "source"))


def test_materialize_source_refuses_a_dirty_checkout(tmp_path: Path, fake_git) -> None:
    fake_git(_head_script(status=" M train.py\n"))

    with pytest.raises(runner.RunnerError, match="not clean"):
        runner.materialize_source(REPOSITORY, COMMIT, str(tmp_path / "source"))


def test_materialize_source_reports_a_clone_failure(tmp_path: Path, fake_git) -> None:
    fake_git(_head_script(clone_fails=True))

    with pytest.raises(runner.RunnerError, match="repository not found"):
        runner.materialize_source(REPOSITORY, COMMIT, str(tmp_path / "source"))


def test_materialize_source_fetches_when_the_object_is_absent(tmp_path: Path, fake_git) -> None:
    script = _head_script()
    script["cat-file"] = {"exit_code": 1}
    script["fetch"] = {"exit_code": 0}
    fake_git(script)

    assert runner.materialize_source(REPOSITORY, COMMIT, str(tmp_path / "source")) == COMMIT


def test_materialize_source_fails_when_the_commit_does_not_exist(tmp_path: Path, fake_git) -> None:
    script = _head_script()
    script["cat-file"] = {"exit_code": 1}
    script["fetch"] = {"exit_code": 1}
    fake_git(script)

    with pytest.raises(runner.RunnerError, match="does not contain commit"):
        runner.materialize_source(REPOSITORY, COMMIT, str(tmp_path / "source"))


def test_prepare_creates_the_isolated_workspace_and_input_directories(
    tmp_path: Path, fake_git, worker_home
) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"

    output = _capture_stdout(
        runner.prepare,
        _descriptor(job_directory, input_destinations=["nested/input.tar"]),
    )

    assert "executed_commit\ta" * 1 in output
    assert (job_directory / "inputs" / "nested").is_dir()
    assert (job_directory / "outputs").is_dir()
    assert (job_directory / "state").is_dir()
    info = json.loads((job_directory / "job.json").read_text(encoding="utf-8"))
    assert info["executed_commit"] == COMMIT
    assert info["requested_commit"] == COMMIT
    assert info["repository"] == REPOSITORY


def test_prepare_requires_the_expected_bootstrap_marker(
    tmp_path: Path, fake_git, worker_home
) -> None:
    fake_git(_head_script())
    (worker_home / ".local/state/wavcse-worker/bootstrap-version").unlink()

    with pytest.raises(runner.RunnerError, match="infra worker bootstrap"):
        runner.prepare(_descriptor(tmp_path / "jobs" / "job-0123456789abcdef"))


def test_start_rejects_an_missing_workspace(tmp_path: Path, fake_git, worker_home) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"

    with pytest.raises(runner.RunnerError, match="prepare phase first"):
        runner.start(
            _descriptor(
                job_directory,
                command={"argv": [sys.executable, "-c", "print('never')"]},
                timeout_seconds=30,
            )
        )


def test_start_and_inspect_record_a_successful_detached_execution(
    tmp_path: Path, fake_git, worker_home
) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    output_path = job_directory / "outputs" / "answer.txt"
    runner.prepare(_descriptor(job_directory))

    started = _capture_stdout(
        runner.start,
        _descriptor(
            job_directory,
            command={
                "argv": [
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('42'); "
                    "print('ran', sys.argv[2])",
                    str(output_path),
                    "now",
                ]
            },
            environment={},
            secrets={"MLFLOW_TRACKING_PASSWORD": "super-secret-value"},
            timeout_seconds=60,
            infra={"INFRA_JOB_ID": "job-0123456789abcdef"},
        ),
    )

    assert "pid\t" in started
    rows = _await_terminal(job_directory, timeout=60)

    assert rows["status"] == "finished"
    assert rows["exit_code"] == "0"
    assert output_path.read_text(encoding="utf-8") == "42"
    log = (job_directory / "logs" / "job.log").read_text(encoding="utf-8")
    assert "==== wavcse job job-0123456789abcdef started" in log
    assert f"(commit {COMMIT})" in log
    assert "ran now" in log
    # The secret environment value is delivered on stdin and never persisted.
    assert "super-secret-value" not in log
    descriptor_text = (job_directory / "state" / "descriptor.json").read_text(encoding="utf-8")
    assert "super-secret-value" not in descriptor_text
    assert json.loads(descriptor_text)["secret_names"] == ["MLFLOW_TRACKING_PASSWORD"]


def test_start_records_a_nonzero_exit_code(tmp_path: Path, fake_git, worker_home) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            command={"argv": [sys.executable, "-c", "import sys; sys.exit(7)"]},
            timeout_seconds=60,
        )
    )

    rows = _await_terminal(job_directory, timeout=60)

    assert rows["status"] == "finished"
    assert rows["exit_code"] == "7"
    assert rows["stage"] == "command"


def test_failing_setup_prevents_the_command_stage(tmp_path: Path, fake_git, worker_home) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    marker = job_directory / "outputs" / "command-ran.txt"
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            setup_argv=[sys.executable, "-c", "import sys; sys.exit(3)"],
            command={
                "argv": [
                    sys.executable,
                    "-c",
                    "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                    str(marker),
                ]
            },
            timeout_seconds=60,
        )
    )

    rows = _await_terminal(job_directory, timeout=60)

    assert rows["exit_code"] == "3"
    assert rows["stage"] == "setup"
    assert not marker.exists()


def test_timeout_kills_the_job_and_is_reported(tmp_path: Path, fake_git, worker_home) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            command={"argv": [sys.executable, "-c", "import time; time.sleep(120)"]},
            timeout_seconds=1,
        )
    )

    rows = _await_terminal(job_directory, timeout=60)

    assert rows["exit_code"] == str(runner.TIMEOUT_EXIT_CODE)
    assert rows["timed_out"] == "true"


def test_cancel_terminates_only_the_job_process_tree(tmp_path: Path, fake_git, worker_home) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    marker = job_directory / "outputs" / "started.txt"
    runner.prepare(_descriptor(job_directory))
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        runner.start(
            _descriptor(
                job_directory,
                command={
                    "argv": [
                        sys.executable,
                        "-c",
                        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('up');"
                        " time.sleep(120)",
                        str(marker),
                    ]
                },
                timeout_seconds=None,
            )
        )
        _await_marker(marker, timeout=30)

        first = _capture_stdout(runner.cancel, _descriptor(job_directory))
        second = _capture_stdout(runner.cancel, _descriptor(job_directory))

        assert "cancelled\ttrue" in first
        assert "already_finished\ttrue" in second
        assert unrelated.poll() is None
        rows = _await_terminal(job_directory, timeout=60)
        assert rows["status"] == "finished"
        assert rows["cancelled"] == "true"
        assert rows["exit_code"] != "0"
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=30)


def test_inspect_reports_unknown_without_recorded_state(tmp_path: Path) -> None:
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    job_directory.mkdir(parents=True)

    rows = _rows(_capture_stdout(runner.inspect, _descriptor(job_directory)))

    assert rows["status"] == "unknown"
    assert rows["exit_code"] == ""


def test_cancel_rejects_another_jobs_directory_without_signalling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "jobs" / "job-ffffffffffffffff"
    other.mkdir(parents=True)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(runner.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signalled.append((pid, sig)))

    with pytest.raises(runner.RunnerInputError, match="does not belong"):
        runner.cancel(_descriptor(other))

    assert signalled == []


def test_working_directory_symlink_cannot_leave_checkout(
    tmp_path: Path, fake_git, worker_home
) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    runner.prepare(_descriptor(job_directory))
    (job_directory / "source" / "external").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(runner.RunnerError, match="resolves outside"):
        runner.start(
            _descriptor(
                job_directory,
                command={"argv": [sys.executable, "-c", "pass"], "working_directory": "external"},
            )
        )


def test_repeated_start_refuses_to_launch_the_same_job_twice(
    tmp_path: Path, fake_git, worker_home
) -> None:
    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    descriptor = _descriptor(job_directory, command={"argv": [sys.executable, "-c", "pass"]})
    runner.prepare(descriptor)
    runner.start(descriptor)

    with pytest.raises(runner.RunnerError, match="already started"):
        runner.start(descriptor)


def test_cancel_refuses_to_claim_a_vanished_job_was_cancelled(tmp_path: Path) -> None:
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    (job_directory / "state").mkdir(parents=True)

    with pytest.raises(runner.RunnerError, match="no process"):
        runner.cancel(_descriptor(job_directory))

    assert not (job_directory / "state" / "cancelled.json").exists()


def test_logs_returns_a_bounded_tail(tmp_path: Path) -> None:
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    (job_directory / "logs").mkdir(parents=True)
    (job_directory / "logs" / "job.log").write_text("first\nsecond\nthird\n", encoding="utf-8")

    descriptor = _descriptor(job_directory, tail_bytes=13)

    assert _capture_stdout(runner.logs, descriptor) == "second\nthird\n"
    assert _capture_stdout(runner.logs, _descriptor(job_directory, tail_bytes=7)) == "\nthird\n"
    assert _capture_stdout(runner.logs, _descriptor(job_directory, tail_bytes=0)) == ""


def test_logs_through_main_is_raw_output_without_protocol_framing(tmp_path: Path) -> None:
    """`infra job logs` streams the job's own output; a protocol row would corrupt it."""

    import contextlib
    import io

    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    (job_directory / "logs").mkdir(parents=True)
    (job_directory / "logs" / "job.log").write_text("raw line\n", encoding="utf-8")
    buffer = io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(_descriptor(job_directory, tail_bytes=4096)))
    try:
        with contextlib.redirect_stdout(buffer):
            code = runner.main(["logs"])
    finally:
        sys.stdin = old_stdin

    assert code == 0
    assert buffer.getvalue() == "raw line\n"
    assert runner.SCHEMA_KEY not in buffer.getvalue()


def test_logs_validates_the_tail_bound(tmp_path: Path) -> None:
    with pytest.raises(runner.RunnerInputError):
        runner.logs(_descriptor(tmp_path, tail_bytes=-1))


def test_main_requires_a_subcommand() -> None:
    assert runner.main([]) == 2


def _capture_stdout(function, descriptor) -> str:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        function(descriptor)
    return buffer.getvalue()


def _rows(output: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("\t")
        if key == runner.SCHEMA_KEY:
            continue
        rows[key] = value
    return rows


def _await_terminal(job_directory: Path, *, timeout: float) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _rows(_capture_stdout(runner.inspect, _descriptor(job_directory)))
        if rows["status"] != "running":
            return rows
        time.sleep(0.05)
    raise AssertionError(f"job in {job_directory} did not finish within {timeout} seconds")


def _await_marker(path: Path, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} was never created")


@pytest.mark.parametrize("payload", ["first; touch {canary}", "$(touch {canary})"])
def test_shell_metacharacters_in_argv_are_never_interpreted(
    tmp_path: Path, fake_git, worker_home, payload: str
) -> None:
    """The command is executed as an argv: a shell cannot expand or chain it."""

    fake_git(_head_script())
    job_directory = tmp_path / "jobs" / "job-0123456789abcdef"
    marker = job_directory / "outputs" / "marker.txt"
    canary = job_directory / "outputs" / "pwned.txt"
    literal = payload.format(canary=canary)
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            command={
                "argv": [
                    sys.executable,
                    "-c",
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])",
                    str(marker),
                    literal,
                ]
            },
            timeout_seconds=60,
        )
    )

    rows = _await_terminal(job_directory, timeout=60)

    assert rows["exit_code"] == "0"
    assert marker.read_text(encoding="utf-8") == literal
    assert not canary.exists()
    assert not list(job_directory.glob("outputs/pwned*"))
