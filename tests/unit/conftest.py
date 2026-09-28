"""Unit-test isolation from controller credentials and external networks."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Never

import pytest


def _reject_external_network(*args: object, **kwargs: object) -> Never:
    del args, kwargs
    raise AssertionError("unit tests must not access external networks")


@pytest.fixture(autouse=True)
def isolate_unit_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Hide host cloud state and fail before any unit test opens a network socket."""

    for variable in tuple(os.environ):
        if (
            variable == "RUNPOD_API_KEY"
            or variable == "BOTO_CONFIG"
            or variable.startswith("AWS_")
            or variable.startswith("WAVCSE_INFRA_")
        ):
            monkeypatch.delenv(variable, raising=False)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setattr(socket, "create_connection", _reject_external_network)
    monkeypatch.setattr(socket.socket, "connect", _reject_external_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _reject_external_network)
