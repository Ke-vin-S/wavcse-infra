import hashlib
import re
from pathlib import Path, PurePosixPath

import pytest

from wavcse_infra.app_config import (
    AppConfigEntry,
    AppConfigOutcome,
    _parse_manifest,
    app_config_digest,
    apply_worker_app_config,
    load_app_config_content,
    load_app_config_entries,
)
from wavcse_infra.errors import ConfigurationError
from wavcse_infra.models import WorkerConnectionInfo
from wavcse_infra.workers.ssh import SshCommandResult

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = "wavcse_app_config"


def _connection() -> WorkerConnectionInfo:
    return WorkerConnectionInfo(
        provider_worker_id="pod-123",
        kind="direct",
        host="203.0.113.10",
        port=22,
        username="root",
    )


class Executor:
    """Answer the inspect/install protocol and record every argv and payload."""

    def __init__(self, states: dict[str, str], *, install_state: str = "installed") -> None:
        self.states = states
        self.install_state = install_state
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run_checked(
        self,
        connection: WorkerConnectionInfo,
        remote_argv: tuple[str, ...],
        *,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
    ) -> SshCommandResult:
        assert connection.provider_worker_id == "pod-123"
        self.calls.append((tuple(remote_argv), input_text))
        mode, *rest = remote_argv[3:]
        if mode == "inspect":
            lines = [
                f"{PROTOCOL}\t{destination}\t{state}\t0"
                for destination, state in self.states.items()
            ]
            return SshCommandResult(exit_code=0, stdout="\n".join(lines) + "\n", stderr="")
        destination = rest[0]
        backup = f"{destination}.wavcse-backup-20261004T153000Z"
        detail = backup if self.install_state == "installed" else "-"
        return SshCommandResult(
            exit_code=0,
            stdout=f"{PROTOCOL}\t{destination}\t{self.install_state}\t{detail}\n",
            stderr="",
        )


def _manifest(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def test_every_manifest_entry_resolves_to_a_real_home_relative_file() -> None:
    entries = load_app_config_entries()

    assert entries
    destinations = [entry.destination for entry in entries]
    assert len(destinations) == len(set(destinations))
    for entry in entries:
        assert not PurePosixPath(entry.destination).is_absolute()
        assert ".." not in PurePosixPath(entry.destination).parts
        source = REPOSITORY_ROOT / entry.source
        assert source.is_file(), f"{entry.source} is missing from the repository"
        assert load_app_config_content(entry) == source.read_text(encoding="utf-8")
        assert (
            app_config_digest(load_app_config_content(entry))
            == hashlib.sha256(source.read_bytes()).hexdigest()
        )


def test_manifest_uses_the_operator_tmux_settings() -> None:
    (entry,) = load_app_config_entries(app="tmux")

    assert entry == AppConfigEntry(
        app="tmux",
        source="apps/tmux/tmux.conf",
        destination=".tmux.conf",
        mode=0o644,
    )
    assert "set-option -g prefix C-a" in load_app_config_content(entry)


@pytest.mark.parametrize(
    ("manifest", "line", "reason"),
    [
        ("tmux tmux.conf", 1, "expected <app> <source> <destination> [mode]"),
        ("tmux tmux.conf .tmux.conf 0644 extra", 1, "expected <app> <source>"),
        ("tmux /etc/tmux.conf .tmux.conf", 1, "unsafe source"),
        ("tmux ../tmux.conf .tmux.conf", 1, "unsafe source"),
        ("tmux tmux.conf /root/.tmux.conf", 1, "unsafe destination"),
        ("tmux tmux.conf ../.tmux.conf", 1, "unsafe destination"),
        ("tmux tmux.conf .tmux.conf 998", 1, "invalid mode"),
        ("TMUX tmux.conf .tmux.conf", 1, "invalid app name"),
        ("tmux a.conf .tmux.conf\ntmux b.conf .tmux.conf", 2, "duplicate destination"),
    ],
)
def test_parse_manifest_rejects_an_ambiguous_or_unsafe_registry(
    manifest: str, line: int, reason: str
) -> None:
    with pytest.raises(ConfigurationError, match=re.escape(f"apps/manifest line {line}: {reason}")):
        _parse_manifest(_manifest(*manifest.split("\n")))


def test_parse_manifest_reports_the_offending_line_number() -> None:
    with pytest.raises(ConfigurationError, match=r"apps/manifest line 3: invalid app name"):
        _parse_manifest(_manifest("tmux tmux.conf .tmux.conf", "", "BAD tmux.conf other.conf"))


def test_an_empty_manifest_is_refused_rather_than_silently_applied() -> None:
    with pytest.raises(ConfigurationError, match=r"apps/manifest has no entries to mirror"):
        _parse_manifest(_manifest("# nothing here"))


def test_unknown_app_names_the_mirrored_apps() -> None:
    with pytest.raises(ConfigurationError, match=r"unknown app 'nope'; mirrored apps are: tmux"):
        load_app_config_entries(app="nope")


def test_absent_file_is_installed_with_its_digest_mode_and_payload() -> None:
    executor = Executor({".tmux.conf": "absent"})
    entry = load_app_config_entries()[0]
    content = load_app_config_content(entry)

    (outcome,) = apply_worker_app_config(executor, _connection())

    inspect_argv, inspect_payload = executor.calls[0]
    assert inspect_argv[:4] == ("python3", "-c", inspect_argv[2], "inspect")
    assert inspect_argv[4:] == (".tmux.conf", app_config_digest(content))
    assert inspect_payload is None
    install_argv, install_payload = executor.calls[1]
    assert install_argv[3:] == ("install", ".tmux.conf", app_config_digest(content), "0644")
    assert install_payload == content
    assert content not in " ".join(install_argv)
    assert outcome.state == "installed"


def test_identical_remote_file_is_left_alone() -> None:
    executor = Executor({".tmux.conf": "matches"})

    (outcome,) = apply_worker_app_config(executor, _connection())

    assert outcome.state == "unchanged"
    assert len(executor.calls) == 1


def test_differing_file_is_preserved_without_a_confirmation_callback() -> None:
    executor = Executor({".tmux.conf": "differs"})

    (outcome,) = apply_worker_app_config(executor, _connection())

    assert outcome.state == "preserved"
    assert len(executor.calls) == 1


def test_declining_confirmation_leaves_the_remote_file_untouched() -> None:
    executor = Executor({".tmux.conf": "differs"})

    (outcome,) = apply_worker_app_config(executor, _connection(), confirm=lambda entry: False)

    assert outcome.state == "skipped"
    assert len(executor.calls) == 1


def test_confirmed_override_backs_up_the_previous_remote_file() -> None:
    executor = Executor({".tmux.conf": "differs"})

    (outcome,) = apply_worker_app_config(executor, _connection(), confirm=lambda entry: True)

    assert outcome.state == "overridden"
    assert outcome.backup == ".tmux.conf.wavcse-backup-20261004T153000Z"
    assert len(executor.calls) == 2


def test_an_identical_racing_install_reports_unchanged() -> None:
    executor = Executor({".tmux.conf": "absent"}, install_state="unchanged")

    (outcome,) = apply_worker_app_config(executor, _connection())

    assert outcome.state == "unchanged"


def test_a_missing_protocol_line_for_a_requested_destination_is_an_error() -> None:
    executor = Executor({".other.conf": "absent"})

    (outcome,) = apply_worker_app_config(executor, _connection())

    assert outcome.state == "error"
    assert outcome.detail == "worker returned no configuration state"
    assert len(executor.calls) == 1


def test_a_remote_error_state_is_never_answered_with_a_write() -> None:
    executor = Executor({".tmux.conf": "error"})

    (outcome,) = apply_worker_app_config(executor, _connection(), confirm=lambda entry: True)

    assert outcome.state == "error"
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (AppConfigOutcome("tmux", ".tmux.conf", "unchanged"), "tmux: unchanged ~/.tmux.conf"),
        (AppConfigOutcome("tmux", ".tmux.conf", "installed"), "tmux: installed ~/.tmux.conf"),
        (
            AppConfigOutcome(
                "tmux",
                ".tmux.conf",
                "overridden",
                backup=".tmux.conf.wavcse-backup-20261004T153000Z",
            ),
            "tmux: overridden ~/.tmux.conf (backup ~/.tmux.conf.wavcse-backup-20261004T153000Z)",
        ),
        (
            AppConfigOutcome("tmux", ".tmux.conf", "preserved"),
            "tmux: preserved ~/.tmux.conf (remote content differs; rerun with --yes to override)",
        ),
        (
            AppConfigOutcome("tmux", ".tmux.conf", "skipped"),
            "tmux: skipped ~/.tmux.conf (remote content differs; rerun with --yes to override)",
        ),
        (
            AppConfigOutcome(
                "tmux",
                ".tmux.conf",
                "error",
                detail="worker reported an error for this destination",
            ),
            "tmux: error ~/.tmux.conf (worker reported an error for this destination)",
        ),
    ],
)
def test_outcome_lines_are_stable_operator_facing_text(
    outcome: AppConfigOutcome, expected: str
) -> None:
    assert outcome.describe() == expected
