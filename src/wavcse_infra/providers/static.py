"""Operator-declared static SSH hosts: a provider-shaped read surface, nothing more.

This provider has no create, start, stop, or destroy operation, and its client makes no
HTTP request and starts no subprocess. The declarations in the controller configuration
are the entire provider state, which is why a static host is reported exactly as the
operator wrote it and why no lifecycle command can reach it.
"""

from __future__ import annotations

from wavcse_infra.config import SshConfig, StaticConfig, StaticWorkerConfig
from wavcse_infra.errors import ProviderNotFoundError
from wavcse_infra.models import (
    ExecutionTransport,
    ProviderKind,
    Worker,
    WorkerConnectionInfo,
    WorkerState,
)


class StaticClient:
    """Resolve declared static hosts without contacting them."""

    def __init__(self, config: StaticConfig) -> None:
        self.config = config

    def list_workers(self) -> list[Worker]:
        """Report every declared host in declaration order."""

        return [self._worker(worker) for worker in self.config.workers]

    def get_worker(self, worker_id: str) -> Worker:
        """Report one declared host, or refuse if it was never declared."""

        return self._worker(self.config_for(worker_id))

    def config_for(self, worker_id: str) -> StaticWorkerConfig:
        """Return the declaration for one name; an undeclared name has no provider state."""

        for worker in self.config.workers:
            if worker.name == worker_id:
                return worker
        raise ProviderNotFoundError(
            f"Static host {worker_id} is not declared under [[static.workers]] in the "
            "controller configuration; that declaration is its registration, so nothing "
            "was looked up remotely and nothing was changed locally"
        )

    @staticmethod
    def connection(config: StaticWorkerConfig) -> WorkerConnectionInfo:
        """Describe the one endpoint the operator declared."""

        return WorkerConnectionInfo(
            provider_worker_id=config.name,
            kind="direct",
            host=config.host,
            port=config.port,
            username=config.username,
        )

    def ssh_config(self, config: StaticWorkerConfig, base: SshConfig) -> SshConfig:
        """Return the SSH settings for one host, falling back to the global identity.

        The controller passes `-F /dev/null` and an explicit `-i` for every worker, so the
        operator's own `~/.ssh/config` never applies and a per-host key has to be written
        into this configuration to be used.
        """

        if config.private_key is None:
            return base
        return base.model_copy(update={"private_key": config.private_key})

    @staticmethod
    def _worker(config: StaticWorkerConfig) -> Worker:
        return Worker(
            provider=ProviderKind.STATIC_SSH,
            execution_transport=ExecutionTransport.SSH,
            id=config.name,
            name=config.name,
            # `RUNNING` is unconditional by design. The SSH waiter polls until the provider
            # reports RUNNING and only then probes the endpoint, so a client that answered
            # `UNKNOWN` until a probe had already succeeded could never be probed at all.
            # Declaring the host is what makes it exist; whether it is *reachable* is
            # `WorkerReadinessState`, which is what job placement actually gates on.
            state=WorkerState.RUNNING,
            native_status="registered",
            gpu_type=config.gpu_type,
            gpu_count=config.gpu_count,
            # A static host has no USD/hour and no compute-unit rate, so no cost is
            # reported: the cost guards apply to provisioned capacity, not to a machine
            # the operator already pays for by other means.
            hourly_cost=None,
            ssh_port=config.port,
            ssh_direct=StaticClient.connection(config),
        )
