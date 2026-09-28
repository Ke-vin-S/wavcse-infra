"""Regression coverage for the unit suite's external-access guard."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest


def test_unit_environment_hides_host_cloud_configuration(tmp_path: Path) -> None:
    assert Path.home() == tmp_path
    assert "RUNPOD_API_KEY" not in os.environ
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"
    assert not any(name.startswith("WAVCSE_INFRA_") for name in os.environ)


def test_unit_environment_rejects_network_connections() -> None:
    with pytest.raises(AssertionError, match="must not access external networks"):
        socket.create_connection(("api.runpod.io", 443))
