"""Detached sweep commands cannot bypass the static-host ownership gate."""

from types import SimpleNamespace
from unittest.mock import Mock

from typer.testing import CliRunner

from wavcse_infra import cli


def test_paid_worker_is_refused_before_any_ssh_call(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_load_cli_settings", lambda _: object())
    monkeypatch.setattr(cli, "_require_static_declaration", lambda *_: None)
    ssh_access = Mock(side_effect=AssertionError("SSH must not start"))
    monkeypatch.setattr(cli, "_ssh_access_for", ssh_access)

    result = CliRunner().invoke(
        cli.app,
        [
            "worker",
            "start-detached",
            "paid-pod",
            "--checkout",
            "/workspace/wavCSE",
            "--log-path",
            "/workspace/state/sweep.log",
            "--",
            "python",
            "-m",
            "runner",
        ],
    )

    assert result.exit_code != 0
    assert "requires a declared static_ssh host" in result.output
    ssh_access.assert_not_called()


def test_static_host_write_outside_declared_root_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_load_cli_settings", lambda _: object())
    monkeypatch.setattr(
        cli, "_require_static_declaration", lambda *_: SimpleNamespace(root="/home/operator/wavcse")
    )
    ssh_access = Mock(side_effect=AssertionError("SSH must not start"))
    monkeypatch.setattr(cli, "_ssh_access_for", ssh_access)

    result = CliRunner().invoke(
        cli.app,
        [
            "worker",
            "start-detached",
            "university",
            "--checkout",
            "/tmp/unowned",
            "--log-path",
            "/home/operator/wavcse/logs/sweep.log",
            "--",
            "python",
        ],
    )

    assert result.exit_code != 0
    assert "outside the declared static-host root" in result.output
    ssh_access.assert_not_called()
