"""The declared static SSH provider: a config-shaped read surface with no lifecycle."""

from pathlib import Path

import pytest

from wavcse_infra.config import SshConfig, StaticConfig, StaticWorkerConfig
from wavcse_infra.errors import ProviderNotFoundError
from wavcse_infra.models import (
    ExecutionTransport,
    ProviderKind,
    WorkerReadinessState,
    WorkerState,
)
from wavcse_infra.providers.static import StaticClient
from wavcse_infra.state import WorkerStateStore


def _declaration(name: str = "university") -> StaticWorkerConfig:
    return StaticWorkerConfig(
        name=name,
        host="hpc.example.edu",
        port=2222,
        username="operator",
        gpu_type="NVIDIA A100-SXM4-80GB",
        gpu_count=2,
        root="/home/operator/wavcse",
        private_key=Path("/home/operator/.ssh/university"),
    )


def test_a_declared_host_is_reported_as_running_with_no_cost() -> None:
    client = StaticClient(StaticConfig(workers=(_declaration(),)))

    (worker,) = client.list_workers()

    assert worker.provider is ProviderKind.STATIC_SSH
    assert worker.execution_transport is ExecutionTransport.SSH
    assert worker.id == worker.name == "university"
    assert worker.state is WorkerState.RUNNING
    assert worker.native_status == "registered"
    assert worker.gpu_type == "NVIDIA A100-SXM4-80GB"
    assert worker.gpu_count == 2
    assert worker.hourly_cost is None
    assert worker.ssh_port == 2222
    assert worker.ssh_direct is not None
    assert (worker.ssh_direct.host, worker.ssh_direct.port) == ("hpc.example.edu", 2222)
    assert worker.ssh_direct.username == "operator"
    assert worker.ssh_direct.kind == "direct"


def test_an_undeclared_host_has_no_provider_state_and_changes_nothing() -> None:
    client = StaticClient(StaticConfig(workers=(_declaration(),)))

    with pytest.raises(ProviderNotFoundError, match=r"not declared under \[\[static\.workers\]\]"):
        client.get_worker("some-pod")

    assert client.list_workers()[0].id == "university"


def test_a_declared_host_uses_its_own_private_key_over_the_global_one(tmp_path: Path) -> None:
    client = StaticClient(StaticConfig(workers=(_declaration(),)))
    base = SshConfig(private_key=tmp_path / "global-key")

    overridden = client.ssh_config(_declaration(), base)
    inherited = client.ssh_config(_declaration().model_copy(update={"private_key": None}), base)

    assert overridden.private_key == Path("/home/operator/.ssh/university")
    # Every other SSH setting, including the dedicated known-hosts file, is shared.
    assert overridden.known_hosts_file == base.known_hosts_file
    assert inherited.private_key == tmp_path / "global-key"


def test_a_declared_host_is_tracked_locally_without_any_provider_call(tmp_path: Path) -> None:
    store = WorkerStateStore(tmp_path / "workers.json")
    client = StaticClient(StaticConfig(workers=(_declaration(),)))

    record = store.record_registered(
        client.get_worker("university"),
        cache_root="/home/operator/wavcse/cache",
    )

    assert record.provider is ProviderKind.STATIC_SSH
    assert record.readiness_state is WorkerReadinessState.NOT_READY
    assert store.provider_for("university") is ProviderKind.STATIC_SSH
