"""Concurrency windows in the worker runner and in local job state.

Sequential doubles cannot show a race: these tests deliberately interleave the events that
the Phase 6.1 review identified, using hooks, barriers, and real separate processes, and
assert the invariant that must hold for *every* interleaving rather than one lucky order.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from wavcse_infra.jobs.models import JobProvenance, JobRecord, JobState, load_job_spec
from wavcse_infra.jobs.state import JobStateStore

REPOSITORY = "https://github.com/Synergy-io/wavCSE.git"
COMMIT = "a" * 40
JOB_ID = "job-0123456789abcdef"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "worker" / "job_runner.py"
    spec = importlib.util.spec_from_file_location("wavcse_job_runner_races", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()
RUNNER_PATH = Path(__file__).resolve().parents[2] / "worker" / "job_runner.py"

FAKE_GIT_SCRIPT = """#!/usr/bin/env python3
import os, sys

arguments = sys.argv[3:]
subcommand = arguments[0] if arguments else ""
if subcommand == "clone":
    target = arguments[-1]
    os.makedirs(os.path.join(target, ".git"), exist_ok=True)
elif subcommand == "rev-parse":
    sys.stdout.write("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\n")
sys.exit(0)
"""


@pytest.fixture
def worker_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".local/state/wavcse-worker").mkdir(parents=True)
    (home / ".local/state/wavcse-worker/bootstrap-version").write_text("1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    binary = tmp_path / "fake-bin"
    binary.mkdir()
    git = binary / "git"
    git.write_text(FAKE_GIT_SCRIPT, encoding="utf-8")
    git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    return home


def _descriptor(job_directory: Path, **overrides: Any) -> dict[str, Any]:
    descriptor: dict[str, Any] = {
        "job_id": JOB_ID,
        "job_directory": str(job_directory),
        "source": {"repository": REPOSITORY, "commit": COMMIT},
        "expected_bootstrap_version": "1",
    }
    descriptor.update(overrides)
    return descriptor


def _rows(output: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("\t")
        if separator:
            rows[key] = value
    return rows


def _read_rows(module: Any, function: Any, descriptor: dict[str, Any]) -> dict[str, str]:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        function(descriptor)
    return _rows(buffer.getvalue())


def _await(predicate, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not reached in time")


def _job_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "jobs" / JOB_ID
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _dead_pid() -> int:
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait(timeout=30)
    return finished.pid


# ---------------------------------------------------------------------------------
# HIGH 1: one inconsistent snapshot must never become a terminal conclusion
# ---------------------------------------------------------------------------------


def test_inspect_reports_an_outcome_written_while_the_process_state_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor finishing during the process check must not read as 'no outcome'."""

    job_directory = _job_directory(tmp_path)
    state = job_directory / "state"
    state.mkdir(parents=True, exist_ok=True)
    (job_directory / "job.json").write_text(json.dumps({"job_id": JOB_ID}), encoding="utf-8")
    dead = _dead_pid()
    (state / "pid.json").write_text(
        json.dumps({"supervisor_pid": dead, "supervisor_starttime": 1}),
        encoding="utf-8",
    )
    reads = {"count": 0}
    original = runner._read_evidence

    def reading(paths: dict[str, str]) -> dict[str, Any]:
        reads["count"] += 1
        if reads["count"] == 2:
            # Exactly the reported window: the outcome lands after the first read and
            # before the process state has been fully observed.
            runner._atomic_write_json(
                paths["finished"],
                {
                    "exit_code": 0,
                    "stage": "command",
                    "timed_out": False,
                    "started_at": "2026-09-29T12:00:00Z",
                    "finished_at": "2026-09-29T12:05:00Z",
                    "executed_commit": COMMIT,
                },
            )
        return original(paths)

    monkeypatch.setattr(runner, "_read_evidence", reading)

    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))

    assert reads["count"] >= 2, "the snapshot must be re-read before a terminal conclusion"
    assert rows["status"] == "finished"
    assert rows["exit_code"] == "0"
    assert rows["executed_commit"] == COMMIT


def test_inspect_reports_a_launch_record_written_while_the_process_state_was_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A launch appearing during inspection must not be reported as 'never started'."""

    job_directory = _job_directory(tmp_path)
    state = job_directory / "state"
    state.mkdir(parents=True, exist_ok=True)
    (job_directory / "job.json").write_text(json.dumps({"job_id": JOB_ID}), encoding="utf-8")
    reads = {"count": 0}
    original = runner._read_evidence

    def reading(paths: dict[str, str]) -> dict[str, Any]:
        reads["count"] += 1
        if reads["count"] == 2:
            runner._atomic_write_json(
                state / "start.lock",
                {
                    "schema_version": 1,
                    "pid": os.getpid(),
                    "starttime": runner._process_starttime(os.getpid()),
                    "started_at": "2026-09-29T12:00:00Z",
                },
            )
        return original(paths)

    monkeypatch.setattr(runner, "_read_evidence", reading)

    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))

    assert reads["count"] >= 2
    assert rows["started"] == "true"
    assert rows["launch_alive"] == "true"
    assert rows["status"] == "unknown"


def test_inspect_concludes_unknown_only_after_two_identical_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_directory = _job_directory(tmp_path)
    reads = {"count": 0}
    original = runner._read_evidence

    def reading(paths: dict[str, str]) -> dict[str, Any]:
        reads["count"] += 1
        return original(paths)

    monkeypatch.setattr(runner, "_read_evidence", reading)

    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))

    assert reads["count"] >= 2
    assert rows["status"] == "unknown"


# ---------------------------------------------------------------------------------
# HIGH 2: a launch and a pre-start cancellation can never both win
# ---------------------------------------------------------------------------------


_RACE_WRAPPER = """
import importlib.util, sys, time

runner_path, subcommand, release_at = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("race_runner", runner_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
delay = float(release_at) - time.time()
if delay > 0:
    time.sleep(delay)
raise SystemExit(module.main([subcommand]))
"""


def _race_subprocess(
    subcommand: str,
    descriptor_path: Path,
    release_at: float,
) -> subprocess.Popen[str]:
    """Start one runner process that blocks until the shared release instant.

    The descriptor arrives as a file on stdin, so neither process has to be fed by the
    parent: both are running and released by the clock alone, which is what makes the
    interleaving real rather than a consequence of how the test drives them.
    """

    return subprocess.Popen(
        [sys.executable, "-c", _RACE_WRAPPER, str(RUNNER_PATH), subcommand, str(release_at)],
        stdin=descriptor_path.open(encoding="utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def _run_race(
    job_directory: Path,
    *,
    command: list[str],
    cancel_head_start: float = 0.0,
) -> tuple[dict[str, str], dict[str, str]]:
    descriptor = _descriptor(job_directory, command={"argv": command}, timeout_seconds=30)
    descriptor_path = job_directory / "race-descriptor.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    release_at = time.time() + 0.6
    starter = _race_subprocess("start", descriptor_path, release_at)
    canceller = _race_subprocess("cancel", descriptor_path, release_at - cancel_head_start)
    starter_out, starter_err = starter.communicate(timeout=60)
    canceller_out, canceller_err = canceller.communicate(timeout=60)
    for process, err in ((starter, starter_err), (canceller, canceller_err)):
        assert process.returncode in {0, 1}, f"{process.args} failed: {err}"
    return _rows(starter_out), _rows(canceller_out)


def _kill_recorded_processes(job_directory: Path) -> None:
    """Leave no process of a raced job behind, whatever happened."""

    state_path = job_directory / "state" / "pid.json"
    if not state_path.exists():
        return
    pid_state = json.loads(state_path.read_text(encoding="utf-8"))
    child_pid = pid_state.get("child_pid")
    supervisor_pid = pid_state.get("supervisor_pid")
    if isinstance(supervisor_pid, int):
        with suppress(ProcessLookupError):
            os.kill(supervisor_pid, signal.SIGKILL)
    if isinstance(child_pid, int):
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(child_pid, signal.SIGKILL)


def test_a_launch_and_a_cancellation_never_both_win(tmp_path: Path, worker_home: Path) -> None:
    """Every interleaving ends with either an excluded launch or a terminated command.

    The two deterministic tests below pin each outcome on its own; this one runs both
    processes against the same job at the same instant, from both biases, and asserts the
    invariant that must hold for whichever one wins.
    """

    command = [sys.executable, "-c", "import time; time.sleep(0.5)"]
    outcomes: set[str] = set()
    for iteration in range(6):
        job_directory = tmp_path / f"jobs-{iteration}" / JOB_ID
        runner.prepare(_descriptor(job_directory))
        started, cancelled = _run_race(
            job_directory,
            command=command,
            # Bias half the iterations towards each side, so both decisions are exercised
            # by a genuine race rather than by one side happening to be scheduled first.
            cancel_head_start=0.08 if iteration % 2 else -0.08,
        )
        cancelled_state = job_directory / "state" / "cancelled.json"
        launched = (job_directory / "state" / "pid.json").exists()

        if cancelled_state.exists():
            recorded = json.loads(cancelled_state.read_text(encoding="utf-8"))
            if recorded.get("pre_start") is True:
                # Cancellation won the decision: no launch may exist, ever.
                outcomes.add("cancel-won")
                assert not launched, "a pre-start cancellation must exclude the launch"
                assert "pid" not in started
            else:
                # The launch happened and was terminated through its own identity.
                outcomes.add("launch-terminated")
                assert launched
                assert cancelled.get("cancelled") == "true"
        else:
            # The launch owns the job; the operator is told to retry against the process.
            outcomes.add("launch-in-progress")
            assert cancelled.get("launch_in_progress") == "true"
            assert "pid" in started
        _kill_recorded_processes(job_directory)

    assert outcomes <= {"cancel-won", "launch-terminated", "launch-in-progress"}
    assert "cancel-won" in outcomes, "the race did not exercise the cancellation path"
    assert outcomes & {"launch-terminated", "launch-in-progress"}, (
        "the race did not exercise a winning launch"
    )


def test_cancel_first_makes_every_later_launch_impossible(
    tmp_path: Path, worker_home: Path
) -> None:
    job_directory = _job_directory(tmp_path)
    runner.prepare(_descriptor(job_directory))
    command = [sys.executable, "-c", "print('must not run')"]

    cancelled = _read_rows(runner, runner.cancel, _descriptor(job_directory))
    with pytest.raises(runner.RunnerError, match="cancelled before it started"):
        runner.start(_descriptor(job_directory, command={"argv": command}, timeout_seconds=30))

    assert cancelled["pre_start"] == "true"
    assert not (job_directory / "state" / "pid.json").exists()
    assert not (job_directory / "logs" / "job.log").exists()


def test_launch_first_is_targeted_by_a_later_cancellation(
    tmp_path: Path, worker_home: Path
) -> None:
    job_directory = _job_directory(tmp_path)
    marker = job_directory / "outputs" / "started.txt"
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            command={
                "argv": [
                    sys.executable,
                    "-c",
                    "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('up'); "
                    "time.sleep(120)",
                    str(marker),
                ]
            },
            timeout_seconds=None,
        )
    )
    _await(marker.exists)

    cancelled = _read_rows(runner, runner.cancel, _descriptor(job_directory))

    assert cancelled["cancelled"] == "true"
    assert cancelled["pre_start"] == "false"
    _await(lambda: not runner._group_alive(_recorded_child_pid(job_directory)))


def _group_members(pgid: int) -> list[int]:
    """Return the live process ids currently in one process group."""

    members: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdecimal():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as handle:
                fields = handle.read().rsplit(b")", 1)[1].split()
            if int(fields[2]) == pgid and fields[0] != b"Z":
                members.append(int(entry.name))
        except (OSError, IndexError, ValueError):
            continue
    return members


def _recorded_child_pid(job_directory: Path) -> int:
    """Wait for the detached supervisor to publish the stage group it is running."""

    path = job_directory / "state" / "pid.json"

    def recorded() -> bool:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(payload.get("child_pid"), int)

    _await(recorded)
    return int(json.loads(path.read_text(encoding="utf-8"))["child_pid"])


# ---------------------------------------------------------------------------------
# MEDIUM 9: a descendant that outlives its leader is still this job's execution
# ---------------------------------------------------------------------------------


_BACKGROUND_COMMAND = (
    "import subprocess,sys,time;"
    "child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']);"
    "time.sleep(%s)"
)


def test_a_descendant_that_outlives_its_leader_keeps_the_job_running(
    tmp_path: Path, worker_home: Path
) -> None:
    job_directory = _job_directory(tmp_path)
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            command={
                "argv": [sys.executable, "-c", _BACKGROUND_COMMAND % "300"],
            },
            timeout_seconds=None,
        )
    )
    child_pid = _recorded_child_pid(job_directory)
    # Wait for the descendant itself, not merely for the leader that creates it.
    _await(lambda: len(_group_members(child_pid)) >= 2)
    # The supervisor and the stage leader die; the descendant keeps the group alive.
    pid_state = json.loads((job_directory / "state" / "pid.json").read_text(encoding="utf-8"))
    os.kill(pid_state["supervisor_pid"], signal.SIGKILL)
    os.kill(child_pid, signal.SIGKILL)
    _await(lambda: not runner._process_alive(child_pid))
    _await(lambda: runner._group_alive(child_pid))

    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))

    assert rows["status"] == "running"
    assert rows["group_pid"] == str(child_pid)
    # The recorded supervisor and stage leader are both dead: the live group is the only
    # reason this job is still reported as executing.
    assert not runner._process_alive(int(rows["pid"]))

    cancelled = _read_rows(runner, runner.cancel, _descriptor(job_directory))

    assert cancelled["cancelled"] == "true"
    _await(lambda: not runner._group_alive(child_pid))


def test_finished_jobs_leave_no_processes_behind(tmp_path: Path, worker_home: Path) -> None:
    """A terminal record means nothing of the job is still running."""

    job_directory = _job_directory(tmp_path)
    runner.prepare(_descriptor(job_directory))

    runner.start(
        _descriptor(
            job_directory,
            command={"argv": [sys.executable, "-c", _BACKGROUND_COMMAND % "0"]},
            timeout_seconds=60,
        )
    )
    child_pid = _recorded_child_pid(job_directory)
    _await(lambda: runner._group_alive(child_pid))
    _await(lambda: (job_directory / "state" / "finished.json").exists())
    _await(lambda: not runner._group_alive(child_pid), timeout=30)

    log = (job_directory / "logs" / "job.log").read_text(encoding="utf-8")
    assert "outlived stage group" in log
    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))
    assert rows["status"] == "finished"
    assert "group_pid" not in rows


# ---------------------------------------------------------------------------------
# Local state: one controller machine, no lost updates
# ---------------------------------------------------------------------------------


def _record(store: JobStateStore) -> JobRecord:
    spec = load_job_spec(
        json.dumps(
            {
                "schema_version": 1,
                "name": "race-probe",
                "source": {"repository": REPOSITORY, "commit": COMMIT},
                "command": {"argv": ["uv", "run", "python", "train.py"]},
            }
        )
    )
    return JobRecord(
        job_id=JOB_ID,
        name=spec.name,
        spec=spec,
        state=JobState.PREPARING,
        worker_id="pod-123",
        job_directory=f"/workspace/wavcse-jobs/{JOB_ID}",
        log_path=f"/workspace/wavcse-jobs/{JOB_ID}/logs/job.log",
        requested_commit=COMMIT,
        created_at=NOW,
        updated_at=NOW,
        provenance=JobProvenance(worker_id="pod-123", infra_version="0.1.0"),
    )


def test_the_local_job_lock_is_reentrant_within_one_process(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "jobs", now=lambda: NOW)
    store.create(_record(store))

    with store.locked(JOB_ID), store.locked(JOB_ID):
        store.save(store.require(JOB_ID).model_copy(update={"state_reason": "nested"}))

    assert store.require(JOB_ID).state_reason == "nested"


_LOCK_HOLDER = """
import sys, time
from pathlib import Path
from wavcse_infra.jobs.state import JobStateStore

directory, job_id, path = sys.argv[1:4]
store = JobStateStore(Path(directory))
with store.locked(job_id):
    # Read, announce, then decide and write: the waiter that starts on the announcement
    # must not be able to read the record before this write lands.
    record = store.require(job_id)
    Path(path).write_text("1")
    time.sleep(1.0)
    store.save(record.model_copy(update={"state_reason": "held-by-first"}))
"""

_LOCK_WAITER = """
import json, sys
from pathlib import Path
from wavcse_infra.jobs.state import JobStateStore

directory, job_id, evidence = sys.argv[1:4]
store = JobStateStore(Path(directory))
with store.locked(job_id):
    record = store.require(job_id)
    Path(evidence).write_text(json.dumps({"reason": record.state_reason}))
"""


def test_a_second_controller_process_reads_state_that_the_first_already_wrote(
    tmp_path: Path,
) -> None:
    """Without the lock the waiter would read the record the holder is about to replace."""

    directory = tmp_path / "jobs"
    store = JobStateStore(directory, now=lambda: NOW)
    store.create(_record(store))
    inside = tmp_path / "inside"
    evidence = tmp_path / "evidence.json"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(directory), JOB_ID, str(inside)]
    )
    try:
        _await(inside.exists, timeout=30)
        waiter = subprocess.run(
            [sys.executable, "-c", _LOCK_WAITER, str(directory), JOB_ID, str(evidence)],
            check=True,
            timeout=60,
        )
        assert waiter.returncode == 0
    finally:
        holder.wait(timeout=30)

    observed = json.loads(evidence.read_text())
    assert observed["reason"] == "held-by-first"
    assert store.require(JOB_ID).state_reason == "held-by-first"


def test_inspect_reports_a_durable_pre_start_cancellation(
    tmp_path: Path, worker_home: Path
) -> None:
    """Durable exclusion evidence survives a lost acknowledgement and is reported later."""

    job_directory = _job_directory(tmp_path)
    runner.prepare(_descriptor(job_directory))
    # The cancellation wins and writes its durable decision; the controller never receives
    # this acknowledgement.
    acknowledged = _read_rows(runner, runner.cancel, _descriptor(job_directory))
    assert acknowledged["pre_start"] == "true"

    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))

    assert rows["status"] == "cancelled"
    assert rows["pre_start"] == "true"
    assert rows["executed_commit"] == ""


def test_inspect_does_not_claim_a_pre_start_cancellation_for_a_killed_command(
    tmp_path: Path, worker_home: Path
) -> None:
    """A command that did run keeps its executed commit and is not called pre-start."""

    job_directory = _job_directory(tmp_path)
    marker = job_directory / "outputs" / "started.txt"
    runner.prepare(_descriptor(job_directory))
    runner.start(
        _descriptor(
            job_directory,
            command={
                "argv": [
                    sys.executable,
                    "-c",
                    "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('up'); "
                    "time.sleep(120)",
                    str(marker),
                ]
            },
            timeout_seconds=None,
            infra={"INFRA_GIT_COMMIT": COMMIT},
        )
    )
    _await(marker.exists)
    acknowledged = _read_rows(runner, runner.cancel, _descriptor(job_directory))
    assert acknowledged["pre_start"] == "false"
    # The durable decision itself is not a pre-start exclusion.
    recorded = json.loads((job_directory / "state" / "cancelled.json").read_text(encoding="utf-8"))
    assert recorded["pre_start"] is False

    rows = _read_rows(runner, runner.inspect, _descriptor(job_directory))

    # The supervisor may already have recorded the terminated command's outcome, so either
    # terminal status is correct; what must never happen is a pre-start claim or a lost
    # commit for a command that really ran.
    assert rows["status"] in {"cancelled", "finished"}
    assert rows["pre_start"] == "false"
    assert rows["executed_commit"] == COMMIT


def test_a_second_process_cannot_enter_a_locked_job(tmp_path: Path) -> None:
    """The lock is what makes the read-modify-write above safe across processes."""

    directory = tmp_path / "jobs"
    store = JobStateStore(directory, now=lambda: NOW)
    store.create(_record(store))
    inside = tmp_path / "inside"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(directory), JOB_ID, str(inside)]
    )
    try:
        _await(inside.exists, timeout=30)
        started = time.monotonic()
        subprocess.run(
            [sys.executable, "-c", _LOCK_WAITER, str(directory), JOB_ID, str(tmp_path / "e.json")],
            check=True,
            timeout=60,
        )
        waited = time.monotonic() - started
    finally:
        holder.wait(timeout=30)

    # The waiter could only have read the final state by waiting for the holder to finish.
    assert waited >= 0.5


# ---------------------------------------------------------------------------------
# HIGH 5 / MEDIUM 1: an unverified process group is never signalled
# ---------------------------------------------------------------------------------


def _live_unrelated_process() -> subprocess.Popen[bytes]:
    """Start a live process in its own session to stand in for an unrelated workload."""

    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        start_new_session=True,
    )
    _await(lambda: runner._process_starttime(process.pid) is not None, timeout=30)
    return process


def _refusal_probe(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every signal attempt so a refusal can be proven to signal nothing."""

    attempts: list[object] = []

    def record(*args: object, **kwargs: object) -> bool:
        attempts.append(args)
        return True

    monkeypatch.setattr(runner, "_signal_group", record)
    monkeypatch.setattr(runner, "terminate_group", record)
    return attempts


def test_a_reused_process_group_id_is_refused_without_signalling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live group whose recorded identity does not match is not this job's group."""

    unrelated = _live_unrelated_process()
    attempts = _refusal_probe(monkeypatch)
    try:
        observed = runner._process_starttime(unrelated.pid)
        assert isinstance(observed, int)
        with pytest.raises(runner.RunnerError, match="reused"):
            runner.terminate_job_group(unrelated.pid, expected_starttime=observed + 12345)

        assert attempts == []
        assert runner._process_alive(unrelated.pid)
    finally:
        unrelated.kill()
        unrelated.wait(timeout=30)


def test_a_group_without_a_verifiable_identity_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing identity is refused outright, because the number may be anything."""

    unrelated = _live_unrelated_process()
    attempts = _refusal_probe(monkeypatch)
    try:
        with pytest.raises(runner.RunnerError, match="identity cannot be verified"):
            runner.terminate_job_group(unrelated.pid, expected_starttime=None)

        assert attempts == []
        assert runner._process_alive(unrelated.pid)
    finally:
        unrelated.kill()
        unrelated.wait(timeout=30)


def test_a_live_group_without_a_recorded_identity_is_reported_as_unknown() -> None:
    """Inspection says "not provably this job's" rather than naming an unrelated group."""

    unrelated = _live_unrelated_process()
    try:
        assert runner._group_alive(unrelated.pid)
        # A record that names the pid but not its start ticks cannot prove continuity.
        assert runner._live_job_group({"child_pid": unrelated.pid}) is None
        # With the start ticks recorded, the same live group is provably this job's.
        assert (
            runner._live_job_group(
                {
                    "child_pid": unrelated.pid,
                    "child_starttime": runner._process_starttime(unrelated.pid),
                }
            )
            == unrelated.pid
        )
    finally:
        unrelated.kill()
        unrelated.wait(timeout=30)


def test_cancelling_a_job_with_an_unverifiable_identity_records_nothing(
    tmp_path: Path, worker_home: Path
) -> None:
    """An unprovable record must not produce a pre-start cancellation it cannot prove."""

    job_directory = _job_directory(tmp_path)
    runner.prepare(_descriptor(job_directory))
    state = job_directory / "state"
    # A launch slot was claimed and a child PID was recorded, but without the start ticks
    # that would prove whether that process is still the job's command.
    (state / "start.lock").write_text(
        json.dumps({"pid": os.getpid(), "starttime": 1}), encoding="utf-8"
    )
    (state / "pid.json").write_text(
        json.dumps({"supervisor_pid": 2**22, "child_pid": 2**22 - 1}), encoding="utf-8"
    )

    with pytest.raises(runner.RunnerError, match="identity cannot be verified"):
        runner.cancel(_descriptor(job_directory))

    assert not (state / "cancelled.json").exists()


def test_a_surviving_job_group_prevents_a_terminal_record(
    tmp_path: Path, worker_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Terminal means nothing of the job runs: an unkillable survivor is reported instead."""

    job_directory = _job_directory(tmp_path)
    runner.prepare(_descriptor(job_directory))
    state = job_directory / "state"
    descriptor = _descriptor(
        job_directory,
        command={"argv": [sys.executable, "-c", _BACKGROUND_COMMAND % "0"]},
        timeout_seconds=60,
        infra={"INFRA_GIT_COMMIT": COMMIT},
    )
    (state / "descriptor.json").write_text(json.dumps(descriptor), encoding="utf-8")
    (state / "pid.json").write_text(
        json.dumps(
            {
                "supervisor_pid": os.getpid(),
                "supervisor_starttime": runner._process_starttime(os.getpid()),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WAVCSE_JOB_DIRECTORY", str(job_directory))
    # The checkout is not the subject here, and the bounded termination policy is made to
    # fail so a genuinely surviving descendant is the only thing left to observe.
    monkeypatch.setattr(runner, "_checkout_verified", lambda requested: True)
    monkeypatch.setattr(runner, "terminate_job_group", lambda *args, **kwargs: False)

    try:
        exit_code = runner.supervise(str(job_directory))
        output = capsys.readouterr().out

        assert exit_code == 1
        assert "refusing to record a terminal outcome" in output
        assert not (state / "finished.json").exists()
        rows = _read_rows(runner, runner.inspect, descriptor)
        assert rows["status"] == "running"
        assert "group_pid" in rows
    finally:
        # Only the surviving stage group is killed here: the "supervisor" of this
        # in-process run is the test process itself, which must not be signalled.
        pid_state = json.loads((state / "pid.json").read_text(encoding="utf-8"))
        if isinstance(pid_state.get("child_pid"), int):
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(pid_state["child_pid"], signal.SIGKILL)
