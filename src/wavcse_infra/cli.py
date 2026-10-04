"""Public `infra` command-line interface."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Never
from uuid import uuid4

import typer
from pydantic import ValidationError

from wavcse_infra import __version__
from wavcse_infra.app_config import AppConfigEntry, load_app_config_entries
from wavcse_infra.config import Settings, load_settings, resolved_config_path
from wavcse_infra.doctor import CheckStatus, DoctorReport, run_doctor
from wavcse_infra.errors import (
    ConfigurationError,
    CostGuardError,
    InfraError,
    JobError,
    JobSpecError,
    ProviderError,
    ProviderNotFoundError,
    ProviderOperationAmbiguousError,
    ResourceUnavailableError,
    SshEndpointUnavailableError,
    StateError,
    StorageError,
    StorageObjectNotFoundError,
    StorageVerificationError,
)
from wavcse_infra.jobs.colab_transport import ColabExecutor, ColabJobExecutor, ColabWaiter
from wavcse_infra.jobs.collect import resolve_output_source
from wavcse_infra.jobs.context import JobContext
from wavcse_infra.jobs.execution import JobExecutor
from wavcse_infra.jobs.models import (
    JobRecord,
    JobSpec,
    JobState,
    load_job_spec,
    validate_job_id,
)
from wavcse_infra.jobs.state import JobStateStore
from wavcse_infra.jobs.status import JobCoordinator
from wavcse_infra.jobs.submit import JobSubmitter
from wavcse_infra.models import (
    CloudType,
    ColabBillingMode,
    CostUnit,
    NetworkVolume,
    NetworkVolumeCreationPlan,
    NetworkVolumeSpec,
    ProviderKind,
    VolumeType,
    Worker,
    WorkerConnectionInfo,
    WorkerCreationPlan,
    WorkerHealthReport,
    WorkerReadinessState,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.providers.colab import ColabClient
from wavcse_infra.providers.runpod import RunPodClient, network_volume_create_payload
from wavcse_infra.redaction import redact
from wavcse_infra.state import (
    VolumeLifecycleState,
    VolumeRecord,
    VolumeStateStore,
    WorkerRecord,
    WorkerStateStore,
)
from wavcse_infra.storage.cache import CacheStats, WorkerArtifactCache
from wavcse_infra.storage.manifests import ArtifactManifest, load_manifest_json
from wavcse_infra.storage.s3 import (
    MAX_MANIFEST_BYTES,
    MAX_PRESIGN_EXPIRY_SECONDS,
    MAX_READABLE_EVIDENCE_BYTES,
    MIN_PRESIGN_EXPIRY_SECONDS,
    S3Storage,
    StorageVerification,
)
from wavcse_infra.storage.transfer import (
    ArtifactTransferResult,
    WorkerArtifactTransfer,
)
from wavcse_infra.storage.worker_transfer import MAX_DOWNLOAD_CONCURRENCY
from wavcse_infra.volumes.lifecycle import (
    VolumeLifecycle,
    constrain_worker_spec_to_volume,
    volume_placement_failure_message,
)
from wavcse_infra.workers.bootstrap import WorkerBootstrapper
from wavcse_infra.workers.colab import INTENT_ABANDON_AFTER_HOURS, ColabLifecycle
from wavcse_infra.workers.lifecycle import WorkerLifecycle
from wavcse_infra.workers.ssh import SshExecutor, WorkerSshWaiter, select_worker_connection

app = typer.Typer(
    name="infra",
    help="Operate reproducible wavCSE infrastructure.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Validate controller configuration.", no_args_is_help=True)
worker_app = typer.Typer(help="Manage RunPod and Colab workers.", no_args_is_help=True)
volume_app = typer.Typer(
    help="Manage persistent RunPod network volumes.",
    no_args_is_help=True,
)
volume_cache_app = typer.Typer(
    help="Inspect the rebuildable artifact cache on a mounted network volume.",
    no_args_is_help=True,
)
storage_app = typer.Typer(help="Inspect and transfer canonical S3 artifacts.", no_args_is_help=True)
job_app = typer.Typer(help="Submit and inspect recorded exact-commit jobs.", no_args_is_help=True)
provider_app = typer.Typer(help="Inspect available execution providers.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(worker_app, name="worker")
app.add_typer(volume_app, name="volume")
volume_app.add_typer(volume_cache_app, name="cache")
app.add_typer(storage_app, name="storage")
app.add_typer(job_app, name="job")
app.add_typer(provider_app, name="provider")


@dataclass(frozen=True)
class CliContext:
    """Root options shared by subcommands."""

    config_path: Path | None
    cli_overrides: dict[str, object]
    verbose: bool


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"infra {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    context: typer.Context,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="User TOML configuration file.",
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    runpod_api_url: Annotated[
        str | None,
        typer.Option("--runpod-api-url", help="Override the RunPod REST API base URL."),
    ] = None,
    runpod_timeout: Annotated[
        float | None,
        typer.Option("--runpod-timeout", min=0.1, help="Override API timeout in seconds."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Enable diagnostic logging.")
    ] = False,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the installed version and exit.",
        ),
    ] = None,
) -> None:
    """Configure options shared by all infrastructure commands."""

    del version
    context.obj = CliContext(
        config_path=config_path,
        cli_overrides={
            "runpod.api_url": runpod_api_url,
            "runpod.request_timeout_seconds": runpod_timeout,
        },
        verbose=verbose,
    )


@config_app.command("validate")
def validate_config(context: typer.Context) -> None:
    """Parse and validate configuration without contacting external services."""

    cli_context = _context(context)
    _load_cli_settings(cli_context)
    path = resolved_config_path(cli_context.config_path)
    source = str(path) if path.exists() else "defaults and environment"
    typer.echo(f"Configuration valid ({source}).")


@provider_app.command("list")
def list_providers(context: typer.Context) -> None:
    """Show native cost and transport semantics without allocating capacity."""

    settings = _load_cli_settings(_context(context))
    typer.echo("PROVIDER\tSTATUS\tEXECUTION\tCOST MODEL\tPREFERRED")
    preferred_found = False
    for kind in settings.placement.preferred_providers:
        if kind is ProviderKind.COLAB:
            status = "DISABLED"
            if settings.colab.enabled:
                try:
                    client = _colab_client(settings)
                    client.list_workers()
                    client.usage_snapshot()
                    status = "READY"
                except ProviderError:
                    status = "UNAVAILABLE"
            transport, cost = "COLAB_EXEC", CostUnit.COMPUTE_UNITS.value
        else:
            status, transport, cost = "CONFIGURED", "SSH", CostUnit.USD_PER_HOUR.value
        preferred = not preferred_found and status in {"READY", "CONFIGURED"}
        preferred_found |= preferred
        typer.echo(
            f"{kind.value}\t{status}\t{transport}\t{cost}\t"
            f"{'yes' if preferred else 'fallback' if status != 'DISABLED' else 'no'}"
        )


@app.command("doctor")
def doctor_command(context: typer.Context) -> None:
    """Check controller prerequisites and configured external connectivity."""

    cli_context = _context(context)
    settings = _load_cli_settings(cli_context)
    report = run_doctor(
        settings,
        config_path=resolved_config_path(cli_context.config_path),
    )
    _print_doctor_report(report)
    if not report.successful:
        raise typer.Exit(code=1)


@worker_app.command("list")
def list_workers(
    context: typer.Context,
    provider: Annotated[
        ProviderKind | None,
        typer.Option("--provider", case_sensitive=False, help="Inspect only one provider."),
    ] = None,
    read_only: Annotated[
        bool,
        typer.Option("--read-only", help="Inspect provider state without updating local records."),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Reconcile workers against the selected provider or all enabled providers."""

    settings = _load_cli_settings(_context(context))
    workers: list[Worker] = []
    active_provider = "Colab" if provider is ProviderKind.COLAB else "RunPod"
    try:
        if provider is not ProviderKind.COLAB:
            with RunPodClient.from_settings(settings) as client:
                runpod_workers = client.list_workers()
            workers.extend(runpod_workers)
            if not read_only:
                _reconcile_state(runpod_workers)
        if provider is ProviderKind.COLAB or (provider is None and settings.colab.enabled):
            active_provider = "Colab"
            colab_client = _colab_client(settings)
            colab_workers = colab_client.list_workers()
            workers.extend(colab_workers)
            if not read_only:
                _reconcile_colab_state(colab_workers)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc, provider=active_provider)
    if json_output:
        _print_json([worker.model_dump(mode="json") for worker in workers])
        return
    if not workers:
        typer.echo("No workers found." if settings.colab.enabled else "No RunPod workers found.")
        return

    if settings.colab.enabled:
        typer.echo("PROVIDER\tID\tSTATE\tGPU\tCOUNT\tCLOUD\tCOST/HR\tNAME")
    else:
        typer.echo("ID\tSTATE\tGPU\tCOUNT\tCLOUD\tCOST/HR\tNAME")
    for worker in workers:
        values = (
            worker.id,
            worker.state.value,
            worker.gpu_type or "-",
            str(worker.gpu_count) if worker.gpu_count is not None else "-",
            worker.cloud_type.value if worker.cloud_type is not None else "-",
            _money(worker.hourly_cost),
            worker.name or "-",
        )
        typer.echo(
            "\t".join((worker.provider.value, *values) if settings.colab.enabled else values)
        )


@worker_app.command("show")
def show_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact worker ID or Colab session name.")],
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Inspect without updating local records.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Show one tracked worker from its authoritative provider."""

    settings = _load_cli_settings(_context(context))
    record = _state_record(worker_id)
    active_provider = (
        "Colab" if record is not None and record.provider is ProviderKind.COLAB else "RunPod"
    )
    try:
        if record is not None and record.provider is ProviderKind.COLAB:
            worker = _colab_client(settings).get_worker(worker_id)
        else:
            with RunPodClient.from_settings(settings) as client:
                worker = client.get_worker(worker_id)
        if not read_only:
            _observe_state(worker)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderNotFoundError as exc:
        if not read_only:
            _mark_state_destroyed(worker_id)
        _provider_failure(exc, provider=active_provider)
    except ProviderError as exc:
        _provider_failure(exc, provider=active_provider)
    if json_output:
        _print_json(worker.model_dump(mode="json"))
    else:
        _print_worker(worker)
        _print_colab_billing(record)


@worker_app.command("gpu-types")
def list_gpu_types(
    context: typer.Context,
    cloud: Annotated[
        CloudType,
        typer.Option("--cloud", case_sensitive=False, help="RunPod cloud tier."),
    ] = CloudType.SECURE,
    gpu_count: Annotated[
        int,
        typer.Option("--gpu-count", min=1, help="GPU count used for price and availability."),
    ] = 1,
    data_center: Annotated[
        list[str] | None,
        typer.Option("--data-center", help="Restrict displayed availability to an exact ID."),
    ] = None,
    require_direct_ssh: Annotated[
        bool,
        typer.Option(
            "--require-direct-ssh",
            help="Show only capacity that the RunPod scheduler confirms supports a public IP.",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Discover current GPU prices and Pod capacity without provisioning."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            offers = client.list_gpu_offers(
                cloud,
                gpu_count,
                data_center_ids=tuple(data_center or ()),
                require_public_ip=require_direct_ssh,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    if require_direct_ssh:
        offers.sort(
            key=lambda offer: (
                offer.total_price_per_hour is None,
                offer.total_price_per_hour or Decimal("Infinity"),
                offer.gpu_type_id,
            )
        )
    if json_output:
        _print_json([offer.model_dump(mode="json") for offer in offers])
        return
    typer.echo("GPU TYPE ID\tVRAM\tAVAILABILITY\tPUBLIC IP\tMAX COUNT\tPRICE/GPU-HR\tTOTAL/HR")
    for offer in offers:
        typer.echo(
            "\t".join(
                (
                    offer.gpu_type_id,
                    f"{offer.memory_gb} GB" if offer.memory_gb is not None else "-",
                    offer.availability.value,
                    "YES" if offer.public_ip_capable is True else "-",
                    str(offer.maximum_gpu_count) if offer.maximum_gpu_count is not None else "-",
                    _money(offer.price_per_gpu_hour),
                    _money(offer.total_price_per_hour),
                )
            )
        )


@worker_app.command("create")
def create_worker(
    context: typer.Context,
    gpu: Annotated[
        str | None, typer.Option("--gpu", help="Exact GPU; Colab defaults to configured T4.")
    ] = None,
    cloud: Annotated[
        CloudType | None,
        typer.Option("--cloud", case_sensitive=False, help="Required RunPod cloud tier."),
    ] = None,
    provider: Annotated[
        ProviderKind,
        typer.Option("--provider", case_sensitive=False, help="Execution provider."),
    ] = ProviderKind.RUNPOD,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Container image; mutually exclusive with --template."),
    ] = None,
    template: Annotated[
        str | None,
        typer.Option("--template", help="Pod template ID; mutually exclusive with --image."),
    ] = None,
    gpu_count: Annotated[
        int, typer.Option("--gpu-count", min=1, help="Number of identical GPUs.")
    ] = 1,
    container_disk: Annotated[
        int, typer.Option("--container-disk", min=1, help="Ephemeral container disk in GB.")
    ] = 20,
    volume_gb: Annotated[
        int,
        typer.Option("--volume", min=0, help="Host-local persistent volume in GB; 0 disables."),
    ] = 0,
    volume_mount_path: Annotated[
        str | None,
        typer.Option(
            "--volume-mount-path",
            help=(
                "Persistent/network volume mount path; defaults to volumes.mount_path for a "
                "network volume and /workspace otherwise."
            ),
        ),
    ] = None,
    network_volume_id: Annotated[
        str | None,
        typer.Option("--network-volume-id", help="Existing RunPod network volume ID."),
    ] = None,
    data_center: Annotated[
        list[str] | None,
        typer.Option("--data-center", help="Allowed exact RunPod data-center ID; repeatable."),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Human prefix; an infra-unique suffix is always added."),
    ] = None,
    interruptible: Annotated[
        bool,
        typer.Option(
            "--interruptible",
            help="Request spot capacity (currently rejected because REST v2 lacks support).",
        ),
    ] = False,
    start_ssh: Annotated[
        bool,
        typer.Option("--start-ssh", help="Ask RunPod to inject registered SSH keys and port 22."),
    ] = False,
    require_direct_ssh: Annotated[
        bool,
        typer.Option(
            "--require-direct-ssh",
            help=("Require scheduler placement on public-IP capacity; also requires --start-ssh."),
        ),
    ] = False,
    max_price: Annotated[
        str | None,
        typer.Option("--max-price", help="Maximum accepted total GPU price in USD/hour."),
    ] = None,
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Lifecycle polling timeout in seconds."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive creation confirmation.")
    ] = False,
) -> None:
    """Plan and confirm one provider resource without guessing its price."""

    settings = _load_cli_settings(_context(context))
    if provider is ProviderKind.COLAB:
        try:
            client = _colab_client(settings)
            gpu = gpu or settings.colab.default_gpu
            if (
                cloud is not None
                or image is not None
                or template is not None
                or gpu_count != 1
                or container_disk != 20
                or volume_gb != 0
                or volume_mount_path is not None
                or network_volume_id is not None
                or data_center
                or interruptible
                or start_ssh
                or require_direct_ssh
            ):
                raise ConfigurationError(
                    "Colab does not accept RunPod cloud, image, disk, volume, "
                    "interruptibility, or SSH placement options"
                )
            if max_price is not None:
                raise ConfigurationError(
                    "Colab uses compute units, not --max-price USD/hour; "
                    "configure colab.max_incremental_rate_cu_per_hour"
                )
            if name is not None:
                raise ConfigurationError(
                    "Colab creates unique infra-owned session identities; --name is unsupported"
                )
            before = client.usage_snapshot()
            mode = before.billing_mode
            typer.echo(
                f"Colab billing mode: {mode.value}; paid CU balance: "
                f"{before.paid_balance_cu} CU; observed usage rate: "
                f"{before.rate_cu_per_hour} CU/hour; active assignments: {before.assignments}"
            )
            if mode is ColabBillingMode.PAID_CU:
                typer.echo(
                    f"Requested Colab GPU: {gpu}; maximum incremental rate: "
                    f"{settings.colab.max_incremental_rate_cu_per_hour} CU/hour; minimum "
                    f"paid balance: {settings.colab.minimum_balance_cu} CU. A small amount "
                    "of CU may be consumed before the post-allocation guard rejects."
                )
            else:
                typer.echo(
                    f"Requested Colab GPU: {gpu}; free-tier allocation is best-effort, "
                    "interruptible, and capacity is not guaranteed. The reported usage "
                    "rate is provider metering, not a paid CU cost."
                )
            if not yes and not typer.confirm("Allocate one ephemeral Colab session?"):
                typer.echo("Allocation cancelled; no session was created.")
                return
            worker, _, after = ColabLifecycle(client, _state_store(), settings.colab).create(gpu)
            typer.echo(
                f"Colab session {worker.id} READY; billing mode {mode.value}; observed "
                f"usage rate {after.rate_cu_per_hour} CU/hour (incremental "
                f"{after.rate_cu_per_hour - before.rate_cu_per_hour} CU/hour); paid CU "
                f"balance {after.paid_balance_cu} CU."
            )
            _print_worker(worker)
            _print_colab_billing(_state_store().get(worker.id))
            return
        except ConfigurationError as exc:
            _configuration_failure(exc)
        except ProviderError as exc:
            _provider_failure(exc, provider="Colab")
        except InfraError as exc:
            _operation_failure(exc)
    if gpu is None:
        raise typer.BadParameter("RunPod --gpu is required")
    if cloud is None:
        raise typer.BadParameter("RunPod --cloud is required")
    try:
        with RunPodClient.from_settings(settings) as client:
            volume = (
                _volume_lifecycle(client, settings).show(network_volume_id)
                if network_volume_id is not None
                else None
            )
            try:
                spec = WorkerSpec(
                    name=_infra_worker_name(name),
                    gpu_type=gpu,
                    gpu_count=gpu_count,
                    cloud_type=cloud,
                    image=image,
                    template_id=template,
                    container_disk_gb=container_disk,
                    volume_gb=volume_gb,
                    volume_mount_path=_resolve_volume_mount_path(
                        volume_mount_path,
                        volume=volume,
                        settings=settings,
                    ),
                    network_volume_id=network_volume_id,
                    data_center_ids=tuple(data_center or ()),
                    interruptible=interruptible,
                    start_ssh=start_ssh,
                    require_direct_ssh=require_direct_ssh,
                )
            except ValidationError as exc:
                _configuration_failure(exc)
            if volume is not None:
                spec = constrain_worker_spec_to_volume(spec, volume)

            lifecycle = _lifecycle(client, settings)
            try:
                plan = lifecycle.plan_create(
                    spec,
                    max_hourly_price=_parse_price(max_price),
                )
            except ResourceUnavailableError as exc:
                if volume is None:
                    raise
                raise ResourceUnavailableError(
                    volume_placement_failure_message(spec, volume, reason=str(exc))
                ) from exc
            _print_creation_plan(plan, volume=volume)
            if not yes and not typer.confirm("Create this paid RunPod Pod?"):
                typer.echo("Creation cancelled; no Pod was created.")
                return
            worker = lifecycle.create(plan, timeout_seconds=wait_timeout)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)

    typer.echo("RunPod worker created and reached RUNNING.")
    _print_worker(worker)


@worker_app.command("wait-ssh")
def wait_for_worker_ssh(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="SSH readiness timeout in seconds."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Wait until a provider-running worker accepts an authenticated SSH command."""

    settings = _load_cli_settings(_context(context))
    try:
        _require_runpod_transport(worker_id, "wait for SSH")
        with RunPodClient.from_settings(settings) as client:
            _, waiter = _worker_access(client, settings)
            result = waiter.wait(worker_id, timeout_seconds=wait_timeout)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if json_output:
        _print_json(result.connection.model_dump(mode="json"))
        return
    typer.echo(f"RunPod worker {worker_id} is SSH READY.")
    typer.echo(f"Connection: {result.connection.kind}")
    typer.echo(f"Host: {result.connection.host}")
    typer.echo(f"Port: {result.connection.port}")
    typer.echo(f"Username: {result.connection.username}")


@worker_app.command("ssh")
def ssh_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
) -> None:
    """Open an interactive PTY session, using the RunPod proxy only as fallback."""

    settings = _load_cli_settings(_context(context))
    try:
        _require_runpod_transport(worker_id, "open an SSH session")
        with RunPodClient.from_settings(settings) as client:
            worker = client.get_worker(worker_id)
        _observe_state(worker)
        if worker.state is not WorkerState.RUNNING:
            raise SshEndpointUnavailableError(
                f"RunPod worker {worker_id} is {worker.state.value}; an interactive SSH "
                "session requires provider state RUNNING"
            )
        connection = select_worker_connection(worker)
        if connection is None:
            raise SshEndpointUnavailableError(
                f"RunPod worker {worker_id} has no published SSH endpoint"
            )
        exit_code = SshExecutor(settings.ssh).run_interactive(connection)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@worker_app.command(
    "exec",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def exec_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Direct SSH readiness timeout."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Remote command timeout."),
    ] = None,
) -> None:
    """Run one argv-based command over direct, non-interactive SSH."""

    remote_argv = tuple(context.args)
    if not remote_argv:
        raise typer.BadParameter("a remote command is required after `--`")
    settings = _load_cli_settings(_context(context))
    try:
        _require_runpod_transport(worker_id, "run an arbitrary SSH command")
        with RunPodClient.from_settings(settings) as client:
            executor, waiter = _ssh_access(client, settings)
            ready = waiter.wait(worker_id, timeout_seconds=wait_timeout)
            result = executor.run(
                ready.connection,
                remote_argv,
                timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if result.stdout:
        typer.echo(result.stdout, nl=False)
    if result.stderr:
        typer.echo(result.stderr, err=True, nl=False)
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


@worker_app.command("bootstrap")
def bootstrap_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="SSH readiness timeout in seconds."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Bootstrap timeout in seconds."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Idempotently bootstrap a running NVIDIA worker and require READY health."""

    settings = _load_cli_settings(_context(context))
    record = _state_record(worker_id)
    if record is not None and record.provider is ProviderKind.COLAB:
        try:
            model = ColabLifecycle(
                _colab_client(settings), _state_store(), settings.colab
            ).bootstrap(worker_id)
        except ConfigurationError as exc:
            _configuration_failure(exc)
        except ProviderError as exc:
            _provider_failure(exc, provider="Colab")
        except InfraError as exc:
            _operation_failure(exc)
        typer.echo(f"Colab worker {worker_id} READY; observed physical GPU: {model}")
        return
    try:
        _require_runpod_transport(worker_id, "run the RunPod bootstrap")
        with RunPodClient.from_settings(settings) as client:
            bootstrapper, _ = _worker_access(client, settings)
            report = bootstrapper.bootstrap(
                worker_id,
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
            outcomes = bootstrapper.install_app_config(
                worker_id,
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    _print_health(report, json_output=json_output)
    for outcome in outcomes:
        typer.echo(f"App config {outcome.describe()}", err=json_output)
    if not report.ready:
        typer.echo(
            f"Worker {worker_id} bootstrap completed, but required health checks failed; "
            "local readiness is FAILED.",
            err=True,
        )
        raise typer.Exit(code=1)


@worker_app.command("apply-config")
def apply_worker_config(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    app: Annotated[
        str | None,
        typer.Option("--app", help="Limit to one mirrored application."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help=(
                "Override a differing remote file without prompting; the previous file is "
                "backed up."
            ),
        ),
    ] = False,
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Direct SSH readiness timeout."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Remote command timeout."),
    ] = None,
) -> None:
    """Apply the repository's mirrored application configuration to one worker."""

    settings = _load_cli_settings(_context(context))
    try:
        load_app_config_entries(app=app)
        _require_runpod_transport(worker_id, "apply the worker application configuration")
        with RunPodClient.from_settings(settings) as client:
            bootstrapper, _ = _worker_access(client, settings)
            outcomes = bootstrapper.install_app_config(
                worker_id,
                app=app,
                confirm=(lambda entry: True) if yes else _app_config_confirmation(worker_id),
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    for outcome in outcomes:
        typer.echo(outcome.describe())


@worker_app.command("reconcile")
def reconcile_worker(
    context: typer.Context,
    worker_id: Annotated[
        str,
        typer.Argument(
            help="Exact infra-owned identity of an unresolved Colab allocation intent.",
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive confirmation.")
    ] = False,
) -> None:
    """Retire one unresolved Colab allocation intent the provider proves absent.

    This is the supported recovery for a create whose session never appeared: without it an
    abandoned intent blocks every later Colab allocation. It reads the provider and changes
    only local bookkeeping, and it refuses unless the identity is this controller's own
    allocation, still unresolved, past the abandonment bound, and repeatedly absent from
    successful listings that agree with the account's own assignment count.
    """

    settings = _load_cli_settings(_context(context))
    record = _state_record(worker_id)
    if record is None:
        typer.echo(
            f"No tracked worker record has infra identity {worker_id!r}; "
            "`infra worker list` shows the tracked identities."
        )
        return
    if record.provider is not ProviderKind.COLAB:
        _configuration_failure(
            ConfigurationError(
                f"Worker {worker_id} is a {record.provider.value} record; only an unresolved "
                "Colab allocation intent is reconciled this way"
            )
        )
    lifecycle = ColabLifecycle(_colab_client(settings), _state_store(), settings.colab)
    try:
        assessment = lifecycle.assess_intent(worker_id)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc, provider="Colab")
    except InfraError as exc:
        _operation_failure(exc)

    typer.echo("Unresolved Colab allocation intent")
    typer.echo(f"Infra identity: {assessment.identity}")
    typer.echo(f"Age: {assessment.age_hours} h (abandonment bound {INTENT_ABANDON_AFTER_HOURS} h)")
    typer.echo(f"Provider evidence: {assessment.detail}")
    if assessment.observations:
        typer.echo(
            f"Consecutive successful listings: {assessment.observations}, exact identity "
            f"absent in each; account assignments observed: {assessment.observed_assignments}"
        )
    if not assessment.retirable:
        typer.echo("Nothing to do; the local record is already terminal and provider-absent.")
        return
    typer.echo(
        "No provider resource is created, changed or deleted by this command; only the local "
        "booking of this intent is retired."
    )
    typer.echo(
        "A later `infra worker create --provider colab` is allowed again; the provider "
        "listing showed no session with this identity."
    )
    if not yes and not typer.confirm(
        f"Retire the local allocation intent {assessment.identity!r}?"
    ):
        typer.echo("Reconciliation cancelled; the record is unchanged.")
        return
    try:
        lifecycle.retire_intent(assessment)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    typer.echo(
        f"Colab allocation intent {assessment.identity} is now terminal and provider-absent."
    )


@worker_app.command("health")
def health_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="SSH readiness timeout in seconds."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Health-command timeout in seconds."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Inspect provider, SSH, bootstrap, tools, disk, and NVIDIA GPU health."""

    settings = _load_cli_settings(_context(context))
    record = _state_record(worker_id)
    if record is not None and record.provider is ProviderKind.COLAB:
        try:
            model = ColabLifecycle(
                _colab_client(settings), _state_store(), settings.colab
            ).bootstrap(worker_id)
        except ConfigurationError as exc:
            _configuration_failure(exc)
        except ProviderError as exc:
            _provider_failure(exc, provider="Colab")
        except InfraError as exc:
            _operation_failure(exc)
        typer.echo(f"Colab worker {worker_id} READY; observed physical GPU: {model}")
        return
    try:
        _require_runpod_transport(worker_id, "run RunPod health checks")
        with RunPodClient.from_settings(settings) as client:
            bootstrapper, _ = _worker_access(client, settings)
            report = bootstrapper.health(
                worker_id,
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    _print_health(report, json_output=json_output)
    if not report.ready:
        raise typer.Exit(code=1)


@worker_app.command("start")
def start_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Lifecycle polling timeout in seconds."),
    ] = None,
) -> None:
    """Start one retained Pod by exact provider ID and wait for RUNNING."""

    settings = _load_cli_settings(_context(context))
    try:
        _require_runpod_transport(worker_id, "start a stopped Pod")
        with RunPodClient.from_settings(settings) as client:
            worker = _lifecycle(client, settings).start(
                worker_id,
                timeout_seconds=wait_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    typer.echo(f"RunPod worker {worker.id} is RUNNING.")
    _print_worker(worker)


@worker_app.command("stop")
def stop_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Lifecycle polling timeout in seconds."),
    ] = None,
) -> None:
    """Stop one retained Pod by exact provider ID and wait for STOPPED."""

    settings = _load_cli_settings(_context(context))
    try:
        _require_runpod_transport(worker_id, "stop resumable compute")
        with RunPodClient.from_settings(settings) as client:
            worker = _lifecycle(client, settings).stop(
                worker_id,
                timeout_seconds=wait_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    typer.echo(
        f"RunPod worker {worker.id} is STOPPED. Compute is stopped, but retained storage "
        "may continue to incur charges."
    )
    _print_worker(worker)


@worker_app.command("destroy")
def destroy_worker(
    context: typer.Context,
    worker_id: Annotated[
        str,
        typer.Argument(help="Exact RunPod ID or tracked Colab session identity."),
    ],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Lifecycle polling timeout in seconds."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive destroy confirmation.")
    ] = False,
) -> None:
    """Permanently terminate one exact, owned worker after confirmation."""

    settings = _load_cli_settings(_context(context))
    record = _state_record(worker_id)
    if record is not None and record.provider is ProviderKind.COLAB:
        try:
            if (
                record.infra_identity != worker_id
                or record.provider_worker_id != worker_id
                or record.create_pending
                or record.provider_absent
            ):
                raise ConfigurationError(
                    f"Colab session {worker_id} lacks confirmed ownership; "
                    "refusing terminal release"
                )
            client = _colab_client(settings)
            try:
                target = client.get_worker(worker_id)
            except ProviderNotFoundError:
                _mark_state_destroyed(worker_id)
                typer.echo(f"Colab session {worker_id} is already absent.")
                return
            typer.echo(f"Colab terminal release target: {target.id} ({target.gpu_type or 'CPU'})")
            if not yes and not typer.confirm(
                f"Terminate exact infra-owned Colab session {worker_id}?"
            ):
                typer.echo("Release cancelled; session unchanged.")
                return
            # Do not repeat a mutation whose acknowledgement was lost.
            with suppress(ProviderOperationAmbiguousError):
                client.destroy_worker(record)
            if any(worker.id == worker_id for worker in client.list_workers()):
                raise ProviderOperationAmbiguousError(
                    f"Colab stop of {worker_id} is not confirmed absent; inspect "
                    "infra worker list --provider colab before taking further action"
                )
            _mark_state_destroyed(worker_id)
            typer.echo(f"Colab session {worker_id} was terminated and is absent.")
            return
        except ConfigurationError as exc:
            _configuration_failure(exc)
        except ProviderError as exc:
            _provider_failure(exc, provider="Colab")
    try:
        with RunPodClient.from_settings(settings) as client:
            lifecycle = _lifecycle(client, settings)
            try:
                target = client.get_worker(worker_id)
            except ProviderNotFoundError:
                _mark_state_destroyed(worker_id)
                typer.echo(f"RunPod worker {worker_id} is already absent; nothing was destroyed.")
                return
            _print_destroy_plan(target, _state_record(worker_id))
            if not yes and not typer.confirm(
                f"Permanently destroy exact RunPod worker {worker_id}?"
            ):
                typer.echo("Destroy cancelled; the Pod was not changed.")
                return
            result = lifecycle.destroy(worker_id, timeout_seconds=wait_timeout)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if result.already_absent:
        typer.echo(f"RunPod worker {worker_id} was already absent.")
    else:
        typer.echo(f"RunPod worker {worker_id} was destroyed and is now absent.")


@volume_app.command("list")
def list_volumes(
    context: typer.Context,
    read_only: Annotated[
        bool,
        typer.Option("--read-only", help="Inspect provider state without updating local records."),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """List network volumes and reconcile tracked local metadata."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            volumes = (
                client.list_network_volumes()
                if read_only
                else _volume_lifecycle(client, settings).refresh()
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    records = _volume_records()
    if json_output:
        _print_json(
            {
                "volumes": [volume.model_dump(mode="json") for volume in volumes],
                "tracked": {
                    record.infra_identity: _volume_record_json(record) for record in records
                },
            }
        )
        return
    if not volumes:
        typer.echo("No RunPod network volumes found.")
        _print_untracked_volume_warning(volumes, records)
        return

    typer.echo("ID\tDATA CENTER\tSIZE\tTIER\tNAME")
    for volume in volumes:
        typer.echo(
            "\t".join(
                (
                    volume.id,
                    volume.datacenter,
                    f"{volume.size_gb} GB",
                    volume.volume_type.value if volume.volume_type is not None else "-",
                    volume.name or "-",
                )
            )
        )
    _print_untracked_volume_warning(volumes, records)


@volume_app.command("show")
def show_volume(
    context: typer.Context,
    volume_id: Annotated[str, typer.Argument(help="Exact RunPod network volume ID.")],
    read_only: Annotated[
        bool, typer.Option("--read-only", help="Inspect without updating local records.")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Show one network volume by exact provider ID, with its provider-reported charges."""

    settings = _load_cli_settings(_context(context))
    billing_total: Decimal | None = None
    billing_note: str | None = None
    try:
        with RunPodClient.from_settings(settings) as client:
            lifecycle = _volume_lifecycle(client, settings)
            volume = (
                client.get_network_volume(volume_id) if read_only else lifecycle.show(volume_id)
            )
            try:
                billing = lifecycle.billing(volume_id=volume_id, last_n=24)
                billing_total = billing.total_amount_usd
            except ProviderError as exc:
                billing_note = redact(exc)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderNotFoundError as exc:
        if not read_only:
            _mark_volume_destroyed_locally(volume_id)
        _provider_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    record = _volume_record(volume_id)
    if json_output:
        _print_json(
            {
                "volume": volume.model_dump(mode="json"),
                "tracked": _volume_record_json(record) if record is not None else None,
                "billed_usd_last_24_buckets": (
                    str(billing_total) if billing_total is not None else None
                ),
            }
        )
        return
    _print_volume(volume, record=record, billed_total=billing_total)
    if billing_note is not None:
        typer.echo(f"Provider billing unavailable: {billing_note}", err=True)


@volume_app.command("datacenters")
def list_volume_datacenters(
    context: typer.Context,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """List the data centers that can host a network volume, and their storage tiers."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            data_centers = _volume_lifecycle(client, settings).data_centers()
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    if json_output:
        _print_json([entry.model_dump(mode="json") for entry in data_centers])
        return
    if not data_centers:
        typer.echo("RunPod reports no data center that can host a network volume.")
        return
    typer.echo("DATA CENTER\tREGION\tSTORAGE TIERS\tNAME")
    for entry in data_centers:
        typer.echo(
            "\t".join(
                (
                    entry.id,
                    entry.region or "-",
                    ", ".join(volume_type.value for volume_type in entry.network_volume_types),
                    entry.name or "-",
                )
            )
        )


@volume_app.command("create")
def create_volume(
    context: typer.Context,
    data_center: Annotated[
        str,
        typer.Option("--data-center", help="Exact RunPod data-center ID for the volume."),
    ],
    size: Annotated[
        int,
        typer.Option("--size", min=10, max=4096, help="Volume size in GB."),
    ],
    tier: Annotated[
        VolumeType | None,
        typer.Option(
            "--tier",
            case_sensitive=False,
            help="Storage tier; defaults to the data center's own default tier.",
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Option("--name", help="Human prefix; an infra-unique suffix is always added."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive creation confirmation.")
    ] = False,
) -> None:
    """Plan, confirm, and create one billable persistent network volume."""

    settings = _load_cli_settings(_context(context))
    try:
        spec = NetworkVolumeSpec(
            name=_infra_volume_name(name),
            size_gb=size,
            datacenter=data_center,
            volume_type=tier,
        )
    except ValidationError as exc:
        _configuration_failure(exc)

    try:
        with RunPodClient.from_settings(settings) as client:
            lifecycle = _volume_lifecycle(client, settings)
            plan = lifecycle.plan_create(spec)
            _print_volume_creation_plan(plan)
            if not yes and not typer.confirm("Create this billable RunPod network volume?"):
                typer.echo("Creation cancelled; no volume was created.")
                return
            created = lifecycle.create(plan)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)

    typer.echo("RunPod network volume created.")
    _print_volume(created.volume, record=created.record)


@volume_app.command("destroy")
def destroy_volume(
    context: typer.Context,
    volume_id: Annotated[
        str,
        typer.Argument(help="Exact RunPod network volume ID; names are not accepted."),
    ],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Lifecycle polling timeout in seconds."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive destroy confirmation.")
    ] = False,
) -> None:
    """Permanently delete one exact-ID network volume after explicit confirmation.

    Nothing else is deleted. Pods that mounted this volume keep running, and every canonical
    object in S3 is untouched; only the rebuildable cache on the volume is lost.
    """

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            lifecycle = _volume_lifecycle(client, settings)
            try:
                target = client.get_network_volume(volume_id)
            except ProviderNotFoundError:
                _mark_volume_destroyed_locally(volume_id)
                typer.echo(
                    f"RunPod network volume {volume_id} is already absent; nothing was "
                    "destroyed. This command addresses a volume by its exact provider ID, "
                    "never by name; `infra volume list` shows both."
                )
                return
            record = _volume_record(volume_id)
            _print_volume_destroy_plan(
                target,
                record=record,
                mounting_workers=_mounting_workers(target.id),
            )
            if not yes and not typer.confirm(
                f"Permanently destroy exact RunPod network volume {volume_id} and its cached data?"
            ):
                typer.echo("Destroy cancelled; the volume was not changed.")
                return
            result = lifecycle.destroy(volume_id, timeout_seconds=wait_timeout)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if result.already_absent:
        typer.echo(f"RunPod network volume {volume_id} was already absent.")
    else:
        typer.echo(f"RunPod network volume {volume_id} was destroyed and is now absent.")
    typer.echo(
        "No Pod was stopped or destroyed, and no S3 object was deleted. "
        "A Pod that still mounts this volume must be destroyed explicitly."
    )


@volume_app.command("forget")
def forget_volume(
    context: typer.Context,
    infra_identity: Annotated[
        str,
        typer.Argument(
            help="Infra identity of a tracked volume record, as shown by `infra volume list`.",
        ),
    ],
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive confirmation.")
    ] = False,
) -> None:
    """Remove one local volume record without contacting or changing the provider.

    This exists for one case: a create whose response was lost and whose reconciliation
    found no matching provider volume. Forgetting the intent allows a later create, and it
    deletes bookkeeping only. A provider volume that does exist stays visible in
    `infra volume list`, but this controller would no longer link it to a create.
    """

    _load_cli_settings(_context(context))
    store = _volume_state_store()
    try:
        record = store.get_by_identity(infra_identity)
    except StateError as exc:
        _operation_failure(exc)
    if record is None:
        typer.echo(
            f"No tracked network volume record has infra identity {infra_identity!r}; "
            "`infra volume list` shows the tracked identities."
        )
        return
    typer.echo("Local volume record to forget")
    typer.echo(f"Infra identity: {record.infra_identity}")
    typer.echo(f"Provider volume id: {record.provider_volume_id or '-'}")
    typer.echo(f"Requested: {record.requested_size_gb} GB in {record.requested_data_center}")
    typer.echo(f"Life cycle: {record.lifecycle_state.value}")
    typer.echo(
        "No provider resource is changed or deleted by this command; only local bookkeeping "
        "is removed."
    )
    if not yes and not typer.confirm(f"Forget the local record for {infra_identity!r}?"):
        typer.echo("Forget cancelled; the record is unchanged.")
        return
    try:
        forgotten = store.forget(infra_identity)
    except StateError as exc:
        _operation_failure(exc)
    if forgotten is None:
        typer.echo("The record was already absent; nothing changed.")
        return
    typer.echo(
        f"Local record {infra_identity!r} was forgotten. Any provider volume it named is "
        "unchanged and still appears in `infra volume list`."
    )


@volume_cache_app.command("stats")
def cache_stats_command(
    context: typer.Context,
    worker: Annotated[
        str,
        typer.Option("--worker", help="Exact RunPod worker ID whose volume holds the cache."),
    ],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="SSH readiness timeout in seconds."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Remote command timeout in seconds."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Report the recorded contents of the rebuildable cache on a worker's volume.

    Sizes come from each entry's own metadata document, so this stays cheap on a full volume.
    Verification happens whenever an entry is used, which is where it matters; this command
    proves the mount is present and reports an entry whose metadata is unusable separately.
    """

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            worker_record = _state_record(worker)
            cache_root = None if worker_record is None else worker_record.network_volume_mount_path
            if cache_root is None:
                raise ConfigurationError(
                    f"Worker {worker} has no network volume mount recorded, so it has no "
                    "rebuildable cache to inspect; create the Pod with --network-volume-id and "
                    "bootstrap it first"
                )
            worker_view = client.get_worker(worker)
            if worker_view.state is not WorkerState.RUNNING:
                raise ConfigurationError(
                    f"RunPod worker {worker} is {worker_view.state.value}; cache inspection "
                    "requires provider state RUNNING"
                )
            executor, waiter = _ssh_access(client, settings)
            transfer = WorkerArtifactTransfer(waiter, executor, settings.ssh)
            cache = WorkerArtifactCache(
                waiter,
                executor,
                settings.ssh,
                transfer,
                warn=_print_warning,
            )
            stats = cache.stats(
                worker,
                cache_root=cache_root,
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if json_output:
        _print_json(stats.model_dump(mode="json"))
        return
    _print_cache_stats(stats)


@storage_app.command("list")
def list_storage(
    context: typer.Context,
    prefix: Annotated[
        str | None,
        typer.Option(
            "--prefix",
            help="Restrict to a key prefix relative to the configured namespace.",
        ),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=1000, help="Maximum objects to display.")
    ] = 100,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """List objects inside the configured bucket prefix."""

    settings = _load_cli_settings(_context(context))
    try:
        storage = S3Storage.from_settings(settings)
        objects = storage.list_objects(prefix or "", limit=limit)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except StorageError as exc:
        _storage_failure(exc)
    if json_output:
        _print_json([stored.model_dump(mode="json") for stored in objects])
        return
    if not objects:
        typer.echo(f"No objects found under s3://{storage.bucket}/{storage.prefix}/.")
        return

    typer.echo("SIZE\tLAST MODIFIED\tKEY")
    for stored in objects:
        typer.echo(
            "\t".join(
                (
                    str(stored.size_bytes),
                    stored.last_modified.isoformat() if stored.last_modified else "-",
                    f"s3://{storage.bucket}/{stored.key}",
                )
            )
        )


@storage_app.command("presign-download")
def presign_download(
    context: typer.Context,
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact key relative to the configured namespace prefix."),
    ],
    expires_in: Annotated[
        int | None,
        typer.Option(
            "--expires-in",
            min=MIN_PRESIGN_EXPIRY_SECONDS,
            max=MAX_PRESIGN_EXPIRY_SECONDS,
            help="URL lifetime in seconds; defaults to storage.presign_expiry_seconds.",
        ),
    ] = None,
) -> None:
    """Print one time-limited GET URL for an exact stored object.

    The URL is a bearer secret. It is written only to standard output as the explicitly
    requested result of this command and is never recorded in logs or local state.
    """

    settings = _load_cli_settings(_context(context))
    try:
        storage = S3Storage.from_settings(settings)
        if storage.object_metadata(artifact) is None:
            raise StorageObjectNotFoundError(
                f"s3://{storage.bucket}/{storage.object_key(artifact)} does not exist; "
                "list the namespace before presigning a download"
            )
        presigned = storage.presign_download(artifact, expires_in_seconds=expires_in)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except StorageError as exc:
        _storage_failure(exc)
    typer.echo(presigned.reveal())


@storage_app.command("presign-upload")
def presign_upload(
    context: typer.Context,
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact key relative to the configured namespace prefix."),
    ],
    expires_in: Annotated[
        int | None,
        typer.Option(
            "--expires-in",
            min=MIN_PRESIGN_EXPIRY_SECONDS,
            max=MAX_PRESIGN_EXPIRY_SECONDS,
            help="URL lifetime in seconds; defaults to storage.presign_expiry_seconds.",
        ),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Permit replacing an object that already exists at this key.",
        ),
    ] = False,
) -> None:
    """Print one time-limited PUT URL for an exact artifact key.

    The URL is a bearer secret. It is written only to standard output as the explicitly
    requested result of this command and is never recorded in logs or local state.
    """

    settings = _load_cli_settings(_context(context))
    try:
        storage = S3Storage.from_settings(settings)
        storage.require_writable(artifact, overwrite=overwrite)
        presigned = storage.presign_upload(
            artifact, expires_in_seconds=expires_in, overwrite=overwrite
        )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except StorageError as exc:
        _storage_failure(exc)
    typer.echo(presigned.reveal())
    if presigned.if_none_match:
        typer.echo("Send the signed HTTP header 'If-None-Match: *' with this PUT.", err=True)


@storage_app.command("verify")
def verify_artifact(
    context: typer.Context,
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact key relative to the configured namespace prefix."),
    ],
    expected_size: Annotated[
        int | None,
        typer.Option("--expected-size", min=0, help="Required object size in bytes."),
    ] = None,
    manifest: Annotated[
        str | None,
        typer.Option("--manifest", help="Manifest object key stored in the namespace."),
    ] = None,
    manifest_file: Annotated[
        Path | None,
        typer.Option(
            "--manifest-file",
            dir_okay=False,
            help="Local manifest JSON to check against the stored object.",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Verify stored existence, size, and manifest consistency without downloading.

    This command deliberately makes no cryptographic claim about object content: S3
    ETags are not SHA-256 digests, and the recorded digest is only confirmed when
    something actually reads the bytes.
    """

    settings = _load_cli_settings(_context(context))
    if manifest is not None and manifest_file is not None:
        _configuration_failure(
            ConfigurationError("--manifest and --manifest-file are mutually exclusive")
        )
    try:
        storage = S3Storage.from_settings(settings)
        artifact_manifest = (
            storage.read_manifest(manifest)
            if manifest is not None
            else _load_manifest_file(manifest_file)
        )
        verification = storage.verify_object(
            artifact,
            expected_size=expected_size,
            manifest=artifact_manifest,
        )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except StorageError as exc:
        _storage_failure(exc)
    if json_output:
        _print_json(verification.model_dump(mode="json"))
        return
    _print_storage_verification(verification, bucket=storage.bucket)


@storage_app.command("read")
def read_artifact(
    context: typer.Context,
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact key relative to the configured namespace prefix."),
    ],
    max_bytes: Annotated[
        int,
        typer.Option(
            "--max-bytes",
            min=1,
            max=MAX_READABLE_EVIDENCE_BYTES,
            help="Refuse to buffer an object larger than this many bytes.",
        ),
    ] = MAX_MANIFEST_BYTES,
    expected_sha256: Annotated[
        str | None,
        typer.Option(
            "--expected-sha256",
            help="Required SHA-256 of the stored bytes; the read fails on a mismatch.",
        ),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Read one small stored object's bytes back for independent inspection.

    Read-only, and deliberately bounded: it exists so a caller can validate a small
    evidence document (a job manifest, a metrics text file) against bytes the caller hashes
    itself, rather than trusting a worker's report about them. When `--expected-sha256` is
    supplied the digest is checked before anything is printed, so a caller that already
    recorded a digest can prove the content it inspects is the content that was verified.
    """

    settings = _load_cli_settings(_context(context))
    expected = expected_sha256.lower() if expected_sha256 is not None else None
    if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", expected):
        _configuration_failure(
            ConfigurationError("--expected-sha256 must be 64 lowercase hex characters")
        )
    try:
        storage = S3Storage.from_settings(settings)
        payload = storage.read_object_bytes(artifact, max_bytes=max_bytes)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except StorageError as exc:
        _storage_failure(exc)
    digest = hashlib.sha256(payload).hexdigest()
    if expected is not None and digest != expected:
        _storage_failure(
            StorageVerificationError(
                f"s3://{storage.bucket}/{storage.object_key(artifact)} does not contain the "
                f"expected bytes: its SHA-256 is {digest}, but {expected} was expected"
            )
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        _storage_failure(
            StorageVerificationError(
                f"s3://{storage.bucket}/{storage.object_key(artifact)} is not valid UTF-8, "
                "so it cannot be inspected as an evidence document"
            )
        )
    if json_output:
        _print_json(
            {
                "artifact": storage.object_key(artifact),
                "size_bytes": len(payload),
                "sha256": digest,
                "text": text,
            }
        )
        return
    typer.echo(text, nl=False)


@storage_app.command("download")
def download_artifact(
    context: typer.Context,
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact key relative to the configured namespace prefix."),
    ],
    destination: Annotated[
        str,
        typer.Argument(help="Absolute destination path on the worker."),
    ],
    worker: Annotated[
        str, typer.Option("--worker", help="Exact READY worker ID that receives the artifact.")
    ],
    expected_size: Annotated[
        int | None,
        typer.Option("--expected-size", min=0, help="Required object size in bytes."),
    ] = None,
    expected_sha256: Annotated[
        str | None,
        typer.Option("--expected-sha256", help="Required SHA-256 digest of the object."),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing destination file on the worker."),
    ] = False,
    concurrency: Annotated[
        int | None,
        typer.Option(
            "--concurrency",
            min=1,
            max=MAX_DOWNLOAD_CONCURRENCY,
            help=(
                "Parallel byte-range streams for a large artifact "
                f"(1-{MAX_DOWNLOAD_CONCURRENCY}); the worker default applies when omitted."
            ),
        ),
    ] = None,
    expires_in: Annotated[
        int | None,
        typer.Option(
            "--expires-in",
            min=MIN_PRESIGN_EXPIRY_SECONDS,
            max=MAX_PRESIGN_EXPIRY_SECONDS,
            help="Presigned URL lifetime in seconds.",
        ),
    ] = None,
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="SSH readiness timeout in seconds."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Transfer timeout in seconds."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Materialize one S3 artifact on a READY worker through a presigned GET URL."""

    settings = _load_cli_settings(_context(context))
    try:
        with _artifact_transfer_for(worker, settings) as transfer:
            result = transfer.download(
                worker,
                storage=S3Storage.from_settings(settings),
                key=artifact,
                destination=destination,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
                overwrite=overwrite,
                concurrency=concurrency,
                expires_in_seconds=expires_in,
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if json_output:
        _print_json(result.model_dump(mode="json"))
        return
    typer.echo(f"Worker {worker} downloaded the artifact.")
    _print_transfer_result(result)


@storage_app.command("upload")
def upload_artifact(
    context: typer.Context,
    artifact: Annotated[
        str,
        typer.Argument(help="Artifact key relative to the configured namespace prefix."),
    ],
    source: Annotated[
        str,
        typer.Argument(help="Absolute path of the artifact on the worker."),
    ],
    worker: Annotated[
        str, typer.Option("--worker", help="Exact READY worker ID that uploads the artifact.")
    ],
    overwrite: Annotated[
        bool,
        typer.Option(
            "--overwrite",
            help="Permit replacing a persisted object that already exists at this key.",
        ),
    ] = False,
    expires_in: Annotated[
        int | None,
        typer.Option(
            "--expires-in",
            min=MIN_PRESIGN_EXPIRY_SECONDS,
            max=MAX_PRESIGN_EXPIRY_SECONDS,
            help="Presigned URL lifetime in seconds.",
        ),
    ] = None,
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="SSH readiness timeout in seconds."),
    ] = None,
    command_timeout: Annotated[
        float | None,
        typer.Option("--command-timeout", min=0.1, help="Transfer timeout in seconds."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Upload one worker artifact through a presigned PUT URL, then verify it."""

    settings = _load_cli_settings(_context(context))
    try:
        with _artifact_transfer_for(worker, settings) as transfer:
            outcome = transfer.upload(
                worker,
                storage=S3Storage.from_settings(settings),
                source=source,
                key=artifact,
                overwrite=overwrite,
                expires_in_seconds=expires_in,
                wait_timeout_seconds=wait_timeout,
                command_timeout_seconds=command_timeout,
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _operation_failure(exc)
    if json_output:
        _print_json(outcome.model_dump(mode="json"))
        return
    typer.echo(f"Worker {worker} uploaded the artifact.")
    _print_transfer_result(outcome.result)
    typer.echo("Controller verification of the stored object follows.")
    _print_storage_verification(outcome.verification, bucket=settings.storage.bucket or "-")


@job_app.command("submit")
def submit_job(
    context: typer.Context,
    job_spec: Annotated[
        Path,
        typer.Argument(
            help="Version 1 JSON job specification file.",
            exists=True,
            dir_okay=False,
            readable=True,
            resolve_path=True,
        ),
    ],
    worker: Annotated[
        str | None, typer.Option("--worker", help="Exact READY worker ID; overrides placement.")
    ] = None,
    provider: Annotated[
        ProviderKind | None,
        typer.Option("--provider", case_sensitive=False, help="Restrict provider selection."),
    ] = None,
    wait: Annotated[
        bool, typer.Option("--wait", help="Block until the job reaches a terminal state.")
    ] = False,
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=1.0, help="Bounded wait in seconds with --wait."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Submit one exact-commit job to an owned READY worker.

    Unspecified placement prefers configured providers; never provisions a paid
    resource implicitly. An interrupted preparation stays reconcilable.
    """

    settings = _load_cli_settings(_context(context))
    try:
        spec = _load_job_spec_file(job_spec)
        # Serialize local selection and submission: a second Colab job cannot race
        # past the single-session busy check while the first is being recorded.
        with _state_store().locked():
            selected = worker or _select_job_worker(settings, spec, provider)
            tracked = _state_store().get(selected)
            if provider is not None and tracked is not None and tracked.provider is not provider:
                raise ConfigurationError(f"Worker {selected} does not belong to {provider.value}")
            if tracked is not None and tracked.provider is ProviderKind.COLAB:
                _require_colab_job_budget(settings, spec, tracked)
            with _job_context_for(selected, settings) as job_context:
                record = JobSubmitter(job_context).submit(spec, worker_id=selected)
        if wait:
            with _job_context_for(selected, settings) as job_context:
                result = JobCoordinator(job_context).wait(
                    record.job_id,
                    timeout_seconds=(
                        wait_timeout
                        if wait_timeout is not None
                        else _default_wait_timeout(spec, settings)
                    ),
                )
                _print_warning(result.warning)
                record = result.record
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except JobSpecError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _job_failure(exc, job_id=None)
    _print_job(record, json_output=json_output)
    if record.reconciliation_required:
        typer.echo(
            f"Job {record.job_id} is {record.state.value} on {record.provenance.provider} "
            f"worker {record.worker_id}: the "
            "controller stopped watching a worker phase that may still be running. The job was "
            "neither failed nor lost, and it is never retried implicitly; run "
            f"`infra job status {record.job_id}` to reconcile it.",
            err=True,
        )
        raise typer.Exit(code=1)
    if record.state in {JobState.FAILED, JobState.CANCELLED}:
        raise typer.Exit(code=1)


@job_app.command("status")
def status_job(
    context: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Local job ID returned by `infra job submit`.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Reconcile one job with real worker evidence and report its durable state.

    A job that is still PREPARING is driven forward from worker evidence, because every
    preparation step is idempotent: this may resume an interrupted input materialization
    (verifying anything already on the worker before trusting it) and start the command
    that never started. A job whose outcome cannot be determined stays PREPARING with a
    warning instead of being reported as failed.
    """

    settings = _load_cli_settings(_context(context))
    canonical = _canonical_job_id(job_id)
    try:
        with _job_context_for(_job_store().require(canonical).worker_id, settings) as job_context:
            result = JobCoordinator(job_context).refresh(canonical)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _job_failure(exc, job_id=canonical)
    _print_warning(result.warning)
    record = result.record
    _print_job(record, json_output=json_output)
    if record.state is JobState.FAILED:
        raise typer.Exit(code=1)


@job_app.command("logs")
def logs_job(
    context: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Local job ID returned by `infra job submit`.")],
    tail_bytes: Annotated[
        int | None,
        typer.Option(
            "--tail-bytes",
            min=1,
            max=16 * 1024 * 1024,
            help="Maximum trailing bytes to read; defaults to jobs.log_tail_bytes.",
        ),
    ] = None,
    local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Print the bounded local copy captured at finalization instead of the worker.",
        ),
    ] = False,
) -> None:
    """Print a job's combined stdout/stderr log without any protocol framing."""

    settings = _load_cli_settings(_context(context))
    canonical = _canonical_job_id(job_id)
    store = _job_store()
    if local:
        text = store.read_log(canonical)
        if text is None:
            _job_failure(
                JobError(
                    f"No local log copy exists for job {canonical}; it is captured only when a "
                    "job is finalized by `infra job status`"
                ),
                job_id=canonical,
            )
        typer.echo(text, nl=False)
        return
    try:
        with _job_context_for(store.require(canonical).worker_id, settings) as job_context:
            text = JobCoordinator(job_context).logs(canonical, tail_bytes=tail_bytes)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _job_log_fallback(store, canonical, exc)
    typer.echo(text, nl=False)


@job_app.command("cancel")
def cancel_job(
    context: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Local job ID returned by `infra job submit`.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Cancel one running job process on its worker; the worker itself is untouched.

    A job whose command never started is cancelled from the controller's own decision; a
    job with a live recorded process is cancelled through the worker runner's identity
    check. Input materialization that is still draining on the worker is reported, never
    silently assumed to have stopped.
    """

    settings = _load_cli_settings(_context(context))
    canonical = _canonical_job_id(job_id)
    try:
        with _job_context_for(_job_store().require(canonical).worker_id, settings) as job_context:
            record = JobCoordinator(job_context).cancel(canonical)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    except InfraError as exc:
        _job_failure(exc, job_id=canonical)
    if json_output:
        _print_job(record, json_output=True)
        return
    if record.state is JobState.CANCELLED:
        typer.echo(
            f"Job {record.job_id} cancelled on {record.provenance.provider} worker "
            f"{record.worker_id}; the worker was not stopped or destroyed."
        )
    elif record.state is JobState.RUNNING:
        if record.cancellation_requested_at is not None:
            typer.echo(
                f"Cancellation was requested for job {record.job_id}; the worker has not "
                "reported a final outcome yet. Run `infra job status` again."
            )
        else:
            typer.echo(f"Job {record.job_id} is still RUNNING; no cancellation was applied.")
    else:
        typer.echo(f"Job {record.job_id} is already {record.state.value}; nothing was cancelled.")
    _print_job(record, json_output=False)


@job_app.command("list")
def list_jobs(
    context: typer.Context,
    state: Annotated[
        JobState | None,
        typer.Option("--state", case_sensitive=False, help="Only jobs in this state."),
    ] = None,
    worker: Annotated[
        str | None,
        typer.Option("--worker", help="Only jobs recorded against this exact worker ID."),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """List recorded jobs, oldest first, without touching a provider or a worker.

    Read-only: it reads the durable controller-local records only, so it works
    when a worker is absent and never reconciles, resumes, or cancels anything.
    An automation caller uses it to rediscover job identities after a restart,
    which is otherwise impossible because a job ID is only returned by submit.
    """

    try:
        records = _job_store().list_records()
    except StateError as exc:
        _job_failure(exc, job_id=None)
    if state is not None:
        records = [record for record in records if record.state is state]
    if worker is not None:
        records = [record for record in records if record.worker_id == worker]
    if json_output:
        _print_json([record.model_dump(mode="json") for record in records])
        return
    if not records:
        typer.echo(
            "No recorded jobs."
            if state is None and worker is None
            else "No recorded jobs match the filter."
        )
        return
    typer.echo("JOB ID\tSTATE\tWORKER\tEXIT\tNAME\tCREATED")
    for record in records:
        typer.echo(
            "\t".join(
                (
                    record.job_id,
                    record.state.value,
                    record.worker_id,
                    "-" if record.exit_code is None else str(record.exit_code),
                    record.name,
                    record.created_at.isoformat(),
                )
            )
        )


def _load_job_spec_file(path: Path) -> JobSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Could not read job specification {path}: {redact(exc)}") from exc
    return load_job_spec(text, source=str(path))


def _canonical_job_id(job_id: str) -> str:
    try:
        return validate_job_id(job_id)
    except JobSpecError as exc:
        _configuration_failure(exc)


def _job_store() -> JobStateStore:
    return JobStateStore()


def _optional_storage(settings: Settings) -> S3Storage | None:
    try:
        return S3Storage.from_settings(settings)
    except ConfigurationError:
        return None


def _job_context(client: RunPodClient, settings: Settings) -> JobContext:
    """Assemble the shared job collaborators for one CLI invocation."""

    state_store = _state_store()
    executor = SshExecutor(settings.ssh)
    waiter = WorkerSshWaiter(client, executor, state_store, settings.ssh)
    transfer = WorkerArtifactTransfer(waiter, executor, settings.ssh)
    return JobContext(
        provider=client,
        worker_state=state_store,
        job_store=_job_store(),
        executor=JobExecutor(waiter, executor, settings.ssh, settings.jobs),
        transfer=transfer,
        storage=_optional_storage(settings),
        jobs_config=settings.jobs,
        environ=os.environ,
        cache=WorkerArtifactCache(
            waiter,
            executor,
            settings.ssh,
            transfer,
            warn=_print_warning,
        ),
    )


def _colab_job_context(settings: Settings) -> JobContext:
    client = _colab_client(settings)
    state = _state_store()
    waiter = ColabWaiter(client, state)
    executor = ColabExecutor(client)
    jobs = settings.jobs.model_copy(
        update={
            "worker_root": "/content/.wavcse/jobs",
            "runner_path": "/content/.wavcse/job_runner.py",
        }
    )
    return JobContext(
        provider=client,
        worker_state=state,
        job_store=_job_store(),
        executor=ColabJobExecutor(client, waiter, executor, settings.ssh, jobs),
        transfer=WorkerArtifactTransfer(waiter, executor, settings.ssh),
        storage=_optional_storage(settings),
        jobs_config=jobs,
        environ=os.environ,
    )


@contextmanager
def _job_context_for(worker_id: str, settings: Settings) -> Iterator[JobContext]:
    record = _state_record(worker_id)
    if record is not None and record.provider is ProviderKind.COLAB:
        yield _colab_job_context(settings)
    else:
        with RunPodClient.from_settings(settings) as client:
            yield _job_context(client, settings)


@contextmanager
def _artifact_transfer_for(worker_id: str, settings: Settings) -> Iterator[WorkerArtifactTransfer]:
    record = _state_record(worker_id)
    if record is not None and record.provider is ProviderKind.COLAB:
        yield _colab_job_context(settings).transfer
    else:
        with RunPodClient.from_settings(settings) as client:
            yield _worker_transfer(client, settings)


def _require_colab_job_budget(settings: Settings, spec: JobSpec, record: WorkerRecord) -> None:
    if (
        record.create_pending
        or record.provider_absent
        or record.readiness_state is not WorkerReadinessState.READY
    ):
        raise ConfigurationError(f"Colab worker {record.provider_worker_id} is not READY")
    if any(
        job.worker_id == record.provider_worker_id
        and job.state in {JobState.PENDING, JobState.PREPARING, JobState.RUNNING}
        for job in _job_store().list_records()
    ):
        raise ConfigurationError(
            f"Colab worker {record.provider_worker_id} already has an active job"
        )
    timeout = spec.runtime.timeout_seconds or settings.jobs.default_timeout_seconds
    usage = _colab_client(settings).usage_snapshot()
    baseline = record.baseline_rate_cu_per_hour
    rate = usage.rate_cu_per_hour - baseline if baseline is not None else None
    if (
        baseline is None
        or record.baseline_assignments_count is None
        or usage.assignments != record.baseline_assignments_count + 1
    ):
        raise CostGuardError(
            f"Colab {record.provider_worker_id} session accounting is inconsistent; "
            "exactly one owned active assignment is required"
        )
    if usage.billing_mode is ColabBillingMode.FREE_TIER:
        # A zero paid balance selects best-effort free tier. Its reported usage rate is
        # observed provider metering, never a paid-balance coverage requirement.
        if not settings.colab.allow_free_tier:
            raise CostGuardError(
                "Colab paid CU balance is zero and free-tier execution is disabled "
                "by colab.allow_free_tier"
            )
        return
    projected = rate * Decimal(timeout) / Decimal(3600) if rate is not None else None
    if (
        rate is None
        or rate <= 0
        or rate > settings.colab.max_incremental_rate_cu_per_hour
        or projected is None
        or projected > settings.colab.max_job_cu
        or usage.paid_balance_cu < settings.colab.minimum_balance_cu
        or usage.paid_balance_cu < projected
    ):
        raise CostGuardError(
            f"Colab {record.provider_worker_id} CU budget or job timeout "
            "exceeds the configured policy"
        )


def _select_job_worker(settings: Settings, spec: JobSpec, provider: ProviderKind | None) -> str:
    """Choose an existing READY lease; never provision paid RunPod capacity implicitly."""

    records = _state_store().list_records()
    ordered = (provider,) if provider is not None else settings.placement.preferred_providers
    failure: InfraError | None = None
    for kind in ordered:
        if kind is ProviderKind.COLAB and not settings.colab.enabled:
            continue
        for record in records:
            if (
                record.provider is not kind
                or record.provider_absent
                or record.create_pending
                or record.readiness_state is not WorkerReadinessState.READY
            ):
                continue
            if kind is ProviderKind.COLAB:
                try:
                    _require_colab_job_budget(settings, spec, record)
                except (CostGuardError, ConfigurationError, ProviderError) as exc:
                    failure = exc
                    continue
            try:
                with _job_context_for(record.provider_worker_id, settings) as context:
                    context.provider.get_worker(record.provider_worker_id)
            except ProviderError as exc:
                failure = exc
                continue
            return record.provider_worker_id
    if failure is not None:
        raise failure
    raise ConfigurationError(
        "No eligible READY worker exists for the requested provider policy; "
        "create and bootstrap a Colab worker or supply --worker with an existing READY worker. "
        "RunPod provisioning requires an explicit offer and USD/hour ceiling"
    )


def _default_wait_timeout(spec: JobSpec, settings: Settings) -> float:
    """Bound `--wait` by the job's own timeout plus a small finalization allowance."""

    job_timeout = (
        spec.runtime.timeout_seconds
        if spec.runtime.timeout_seconds is not None
        else settings.jobs.default_timeout_seconds
    )
    return float(job_timeout) + 300.0


def _print_warning(warning: str | None) -> None:
    if warning:
        typer.echo(f"Warning: {redact(warning)}", err=True)


def _print_job(record: JobRecord, *, json_output: bool) -> None:
    if json_output:
        _print_json(record.model_dump(mode="json"))
        return
    fields = (
        ("Job ID", record.job_id),
        ("Name", record.name),
        ("State", record.state.value),
        ("Worker", record.worker_id),
        ("Provider", record.provenance.provider),
        ("Requested commit", record.requested_commit),
        ("Executed commit", record.executed_commit),
        ("Exit code", record.exit_code),
        ("Worker PID", record.pid),
        ("Job directory", record.job_directory),
        ("Log path", record.log_path),
        ("Log bytes", record.log_bytes),
        ("Created", record.created_at.isoformat()),
        ("Started", record.started_at.isoformat() if record.started_at else None),
        ("Finished", record.finished_at.isoformat() if record.finished_at else None),
        ("Remote status", record.remote_status),
        ("Preparation phase", record.preparation_phase),
        ("Interrupted", record.interrupted_at.isoformat() if record.interrupted_at else None),
        ("Reconciliation required", "yes" if record.reconciliation_required else "no"),
        ("Reason", record.state_reason),
        ("Failure", record.failure_reason),
        ("Worker absent", "yes" if record.worker_absent else "no"),
        ("GPU", ", ".join(record.provenance.gpu_models) or None),
        ("GPU count", record.provenance.gpu_count),
        ("Bootstrap version", record.provenance.worker_bootstrap_version),
        ("Infra version", record.provenance.infra_version),
        ("MLflow owner", record.provenance.mlflow_owner),
    )
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")
    for job_input in record.inputs:
        status = "materialized" if job_input.materialized else "NOT MATERIALIZED"
        typer.echo(
            f"Input: {job_input.artifact} -> inputs/{job_input.destination} "
            f"({status}"
            + (f", {job_input.size_bytes} bytes" if job_input.size_bytes is not None else "")
            + (f", {job_input.sha256}" if job_input.sha256 else "")
            + (f", from {job_input.source}" if job_input.source else "")
            + ")"
            + (f" [{job_input.failure_reason}]" if job_input.failure_reason else "")
        )
    for output in record.outputs:
        status = "persisted" if output.persisted else "NOT PERSISTED"
        typer.echo(
            f"Output: {output.path} -> {output.artifact} ({status}"
            + (f", {output.size_bytes} bytes" if output.size_bytes is not None else "")
            + (f", {output.sha256}" if output.sha256 else "")
            + (f", required={output.required}" if output.required else ", optional")
            + ")"
            + (f" [{output.failure_reason}]" if output.failure_reason else "")
        )
    if record.spec.outputs:
        typer.echo(
            "Local output paths: "
            + ", ".join(
                resolve_output_source(record.job_directory, output)
                for output in record.spec.outputs
            )
        )


def _job_failure(exc: Exception, *, job_id: str | None) -> Never:
    """Report an infrastructure failure and name the recorded job when one is known."""

    prefix = f"Job {job_id}: " if job_id else ""
    typer.echo(f"{prefix}{redact(exc)}", err=True)
    if job_id and not _job_store().path_for(job_id).exists():
        typer.echo(
            f"No local record exists for job {job_id}; list durable records under "
            f"{_job_store().directory}",
            err=True,
        )
    raise typer.Exit(code=1) from exc


def _job_log_fallback(store: JobStateStore, job_id: str, exc: Exception) -> Never:
    """Print the locally captured log copy when the remote log cannot be read."""

    typer.echo(f"Job {job_id}: {redact(exc)}", err=True)
    local = store.read_log(job_id)
    if local is None:
        typer.echo(
            "No local log copy was captured either; it is written only when a job reaches a "
            "terminal state, and remote logs live on the worker.",
            err=True,
        )
    else:
        typer.echo(
            "Remote log unavailable; showing the bounded local copy captured at finalization.",
            err=True,
        )
        typer.echo(local, nl=False)
    raise typer.Exit(code=1) from exc


def _context(context: typer.Context) -> CliContext:
    root_context = context.find_root().obj
    if not isinstance(root_context, CliContext):
        raise RuntimeError("CLI context was not initialized")
    return root_context


def _load_cli_settings(cli_context: CliContext) -> Settings:
    try:
        return load_settings(
            config_path=cli_context.config_path,
            cli_overrides=cli_context.cli_overrides,
        )
    except ConfigurationError as exc:
        _configuration_failure(exc)


def _lifecycle(client: RunPodClient, settings: Settings) -> WorkerLifecycle:
    return WorkerLifecycle(
        client,
        _state_store(),
        default_timeout_seconds=settings.runpod.lifecycle_timeout_seconds,
        poll_interval_seconds=settings.runpod.poll_interval_seconds,
        max_poll_interval_seconds=settings.runpod.max_poll_interval_seconds,
    )


def _volume_lifecycle(client: RunPodClient, settings: Settings) -> VolumeLifecycle:
    return VolumeLifecycle(
        client,
        _volume_state_store(),
        default_timeout_seconds=settings.runpod.lifecycle_timeout_seconds,
        poll_interval_seconds=settings.runpod.poll_interval_seconds,
        max_poll_interval_seconds=settings.runpod.max_poll_interval_seconds,
    )


def _volume_state_store() -> VolumeStateStore:
    return VolumeStateStore()


def _volume_records() -> list[VolumeRecord]:
    try:
        return _volume_state_store().list_records()
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)
        return []


def _volume_record(volume_id: str) -> VolumeRecord | None:
    try:
        return _volume_state_store().get(volume_id)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)
        return None


def _mark_volume_destroyed_locally(volume_id: str) -> None:
    try:
        _volume_state_store().mark_destroyed(volume_id)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)


def _volume_record_json(record: VolumeRecord | None) -> dict[str, object] | None:
    return None if record is None else record.model_dump(mode="json")


def _mounting_workers(volume_id: str) -> list[str]:
    """Return tracked worker IDs whose Pod was created with this exact network volume.

    This is advisory evidence for the operator, never a cascade: a volume destroy does not
    touch a Pod, and a Pod destroy does not touch a volume.
    """

    return [
        record.provider_worker_id
        for record in _state_records()
        if record.network_volume_id == volume_id and not record.provider_absent
    ]


def _state_records() -> list[WorkerRecord]:
    try:
        return _state_store().list_records()
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)
        return []


def _resolve_volume_mount_path(
    explicit: str | None,
    *,
    volume: NetworkVolume | None,
    settings: Settings,
) -> str:
    """Return the mount path for one Pod request, defaulting per storage kind.

    A network volume defaults to the configured cache path rather than to `/workspace`, so
    the persistent volume holds only the rebuildable cache while job scratch and the job
    workspace stay on ephemeral container disk. An explicit `--volume-mount-path` always
    wins, and a Pod with no network volume keeps the historical `/workspace` default.
    """

    if explicit is not None:
        return explicit
    if volume is not None:
        return settings.volumes.mount_path
    return "/workspace"


def _infra_volume_name(prefix: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", (prefix or "cache").strip().lower())
    normalized = normalized.strip("-")[:40] or "cache"
    return f"wavcse-vol-{normalized}-{uuid4().hex[:12]}"


def _state_store() -> WorkerStateStore:
    return WorkerStateStore()


def _worker_access(
    client: RunPodClient,
    settings: Settings,
) -> tuple[WorkerBootstrapper, WorkerSshWaiter]:
    state_store = _state_store()
    executor = SshExecutor(settings.ssh)
    waiter = WorkerSshWaiter(client, executor, state_store, settings.ssh)
    return (
        WorkerBootstrapper(waiter, executor, state_store, settings.ssh),
        waiter,
    )


def _ssh_access(
    client: RunPodClient,
    settings: Settings,
) -> tuple[SshExecutor, WorkerSshWaiter]:
    state_store = _state_store()
    executor = SshExecutor(settings.ssh)
    waiter = WorkerSshWaiter(client, executor, state_store, settings.ssh)
    return executor, waiter


def _worker_transfer(
    client: RunPodClient,
    settings: Settings,
) -> WorkerArtifactTransfer:
    executor, waiter = _ssh_access(client, settings)
    return WorkerArtifactTransfer(waiter, executor, settings.ssh)


def _load_manifest_file(path: Path | None) -> ArtifactManifest | None:
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Could not read manifest file {path}: {redact(exc)}") from exc
    return load_manifest_json(text)


def _require_runpod_transport(worker_id: str, operation: str) -> None:
    record = _state_record(worker_id)
    if record is not None and record.provider is ProviderKind.COLAB:
        raise ConfigurationError(
            f"Colab worker {worker_id} cannot {operation}: that operation requires "
            "RunPod SSH or a resumable Pod. Colab destroy is terminal"
        )


def _app_config_confirmation(worker_id: str) -> Callable[[AppConfigEntry], bool]:
    """Prompt before replacing a differing worker file, and never prompt without a TTY.

    A scripted run must neither hang nor silently overwrite remote configuration, so
    without an interactive terminal every override is declined.
    """

    def confirm(entry: AppConfigEntry) -> bool:
        if not sys.stdin.isatty():
            return False
        return typer.confirm(
            f"Override ~/{entry.destination} on worker {worker_id}? The current file is backed up",
            default=False,
        )

    return confirm


def _colab_client(settings: Settings) -> ColabClient:
    if not settings.colab.enabled:
        raise ConfigurationError(
            "Colab is disabled; set colab.enabled = true in the controller TOML"
        )
    return ColabClient(settings.colab)


def _reconcile_colab_state(workers: list[Worker]) -> None:
    try:
        _state_store().reconcile(workers, provider=ProviderKind.COLAB)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)


def _reconcile_state(workers: list[Worker]) -> None:
    try:
        _state_store().reconcile(workers)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)


def _observe_state(worker: Worker) -> None:
    try:
        _state_store().observe(worker)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)


def _mark_state_destroyed(worker_id: str) -> None:
    try:
        _state_store().mark_destroyed(worker_id)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)


def _state_record(worker_id: str) -> WorkerRecord | None:
    try:
        return _state_store().get(worker_id)
    except StateError as exc:
        typer.echo(f"State warning: {redact(exc)}", err=True)
        return None


def _print_storage_verification(verification: StorageVerification, *, bucket: str) -> None:
    typer.echo(f"Object: s3://{bucket}/{verification.key}")
    typer.echo(f"Size: {verification.size_bytes} bytes")
    typer.echo(f"ETag: {verification.etag or '-'} (S3 ETag is not a SHA-256 digest)")
    last_modified = verification.last_modified.isoformat() if verification.last_modified else "-"
    typer.echo(f"Last modified: {last_modified}")
    typer.echo(
        "Expected size: " + ("matched" if verification.expected_size_checked else "not requested")
    )
    if verification.manifest_checked:
        typer.echo(f"Manifest: consistent; recorded SHA-256 {verification.manifest_sha256}")
    else:
        typer.echo("Manifest: not provided")
    typer.echo("Content checksum: NOT verified; the object body was not downloaded")
    for limitation in verification.limitations:
        typer.echo(f"note: {limitation}")


def _print_transfer_result(result: ArtifactTransferResult) -> None:
    typer.echo(f"Operation: {result.operation}")
    typer.echo(f"Path: {result.path}")
    typer.echo(f"Size: {result.size_bytes} bytes")
    typer.echo(f"SHA-256: {result.sha256}")


def _print_doctor_report(report: DoctorReport) -> None:
    colors = {
        CheckStatus.PASS: typer.colors.GREEN,
        CheckStatus.WARN: typer.colors.YELLOW,
        CheckStatus.FAIL: typer.colors.RED,
        CheckStatus.SKIP: typer.colors.BLUE,
    }
    for check in report.checks:
        typer.secho(f"{check.status.value:4} ", fg=colors[check.status], bold=True, nl=False)
        typer.echo(f"{check.name}: {check.detail}")
    if report.successful:
        typer.echo("Controller checks passed.")
    else:
        typer.echo(f"Controller checks failed ({report.failed_count} required check(s)).")


def _print_worker(worker: Worker) -> None:
    fields = (
        ("Provider", worker.provider),
        ("ID", worker.id),
        ("Name", worker.name),
        ("State", worker.state.value),
        ("Native status", worker.native_status),
        ("GPU", worker.gpu_type),
        ("GPU count", worker.gpu_count),
        ("Cloud", worker.cloud_type.value if worker.cloud_type is not None else None),
        ("Current compute cost/hour", _money(worker.hourly_cost)),
        ("Image", worker.image),
        ("Template", worker.template_id),
        ("Container disk (GB)", worker.container_disk_gb),
        ("Persistent volume (GB)", worker.volume_gb),
        ("Network volume", worker.network_volume_id),
        ("Volume mount", worker.volume_mount_path),
        ("Datacenter", worker.datacenter),
        ("Public IP", worker.public_ip),
        ("SSH port", worker.ssh_port),
        ("SSH direct endpoint", _format_connection(worker.ssh_direct)),
        ("SSH proxy endpoint", _format_connection(worker.ssh_proxy)),
        ("Exposed ports", ", ".join(worker.exposed_ports) or None),
        ("Created", worker.created_at.isoformat() if worker.created_at else None),
        ("Last started", worker.last_started_at.isoformat() if worker.last_started_at else None),
    )
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")


def _print_colab_billing(record: WorkerRecord | None) -> None:
    """Render the persisted Colab execution mode and observed metering, never as USD cost."""

    if record is None or record.provider is not ProviderKind.COLAB:
        return
    fields = (
        ("Billing mode", record.billing_mode.value if record.billing_mode else None),
        ("Observed CU rate (CU/hour)", record.observed_rate_cu_per_hour),
        ("Baseline CU rate (CU/hour)", record.baseline_rate_cu_per_hour),
        ("Baseline assignments", record.baseline_assignments_count),
    )
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")


def _format_connection(connection: WorkerConnectionInfo | None) -> str | None:
    if connection is None:
        return None
    host = f"[{connection.host}]" if ":" in connection.host else connection.host
    return f"{connection.username}@{host}:{connection.port}"


def _print_creation_plan(plan: WorkerCreationPlan, *, volume: NetworkVolume | None = None) -> None:
    spec = plan.spec
    offer = plan.offer
    fields = (
        ("Provider", "RunPod"),
        ("Infra identity", spec.name),
        ("GPU", f"{offer.display_name} ({offer.gpu_type_id})"),
        ("GPU count", spec.gpu_count),
        ("Cloud", spec.cloud_type.value),
        ("Availability", offer.availability.value),
        ("Provider list price/hour", _money(offer.total_price_per_hour)),
        ("Maximum price/hour", _money(plan.max_hourly_price)),
        ("Image", spec.image),
        ("Template", spec.template_id),
        ("Container disk", f"{spec.container_disk_gb} GB"),
        ("Persistent volume", f"{spec.volume_gb} GB" if spec.volume_gb else "none"),
        ("Network volume", spec.network_volume_id),
        ("Volume mount", spec.volume_mount_path),
        ("Data centers", ", ".join(spec.data_center_ids) if spec.data_center_ids else "any"),
        ("Interruptible", "yes" if spec.interruptible else "no (on-demand)"),
        ("Start SSH", "yes" if spec.start_ssh else "no"),
        ("Require direct SSH", "yes" if spec.require_direct_ssh else "no"),
        (
            "Public-IP-capable offer",
            "yes" if offer.public_ip_capable is True else "not confirmed",
        ),
    )
    typer.echo("RunPod creation plan")
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")
    if volume is not None:
        typer.echo(f"Network volume data center: {volume.datacenter} (placement constrained)")
        typer.echo(f"Network volume size: {volume.size_gb} GB")
    if offer.total_price_per_hour is None:
        typer.echo("WARNING: RunPod did not provide a reliable pre-creation GPU price.")
    typer.echo(
        "Storage charges are billed separately from GPU compute and continue while a "
        "network volume and its Pod exist."
    )


def _print_volume(
    volume: NetworkVolume,
    *,
    record: VolumeRecord | None,
    billed_total: Decimal | None = None,
) -> None:
    fields = (
        ("Provider", volume.provider),
        ("ID", volume.id),
        ("Name", volume.name),
        ("Data center", volume.datacenter),
        ("Size", f"{volume.size_gb} GB"),
        ("Storage tier", volume.volume_type.value if volume.volume_type is not None else None),
        (
            "Tracked life cycle",
            record.lifecycle_state.value if record is not None else "untracked",
        ),
        (
            "Provider-reported absent",
            "yes" if record is not None and record.provider_absent else "no",
        ),
    )
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")
    if billed_total is not None:
        typer.echo(f"Provider-billed storage (last 24 buckets): ${billed_total:.2f}")
    typer.echo(
        "This is rebuildable working storage, not canonical storage: S3 holds the only "
        "authoritative copy of every artifact."
    )


def _print_volume_creation_plan(plan: NetworkVolumeCreationPlan) -> None:
    spec = plan.spec
    typer.echo("RunPod network volume creation plan")
    typer.echo(f"Infra identity: {spec.name}")
    typer.echo(f"Data center: {spec.datacenter}")
    typer.echo(f"Size: {spec.size_gb} GB")
    typer.echo(
        "Storage tier: "
        + (spec.volume_type.value if spec.volume_type is not None else "data center default")
    )
    typer.echo(
        "Data center storage tiers: "
        + (", ".join(tier.value for tier in plan.data_center.network_volume_types) or "unknown")
    )
    if plan.published_list_price_usd_per_gb_month is not None:
        typer.echo(
            "RunPod published list price: "
            f"${plan.published_list_price_usd_per_gb_month}/GB/month "
            "(published rate, not a provider-reported charge)"
        )
    if plan.estimated_monthly_cost_usd is not None:
        typer.echo(f"Estimated storage cost: ${plan.estimated_monthly_cost_usd}/month")
    elif plan.published_list_price_usd_per_gb_month is not None:
        typer.echo(
            "Estimated storage cost: not quoted; the published rate covers volumes up to "
            f"{plan.published_list_price_max_size_gb} GB and this request is larger"
        )
    else:
        typer.echo(
            "Estimated storage cost: unknown; RunPod publishes no rate this tool can quote "
            "for the requested tier"
        )
    typer.echo(
        "Exact create request: " + json.dumps(network_volume_create_payload(spec), sort_keys=True)
    )
    typer.echo(
        "This is a billable persistent resource: storage charges continue until the volume "
        "is destroyed."
    )


def _print_volume_destroy_plan(
    volume: NetworkVolume,
    *,
    record: VolumeRecord | None,
    mounting_workers: list[str],
) -> None:
    typer.echo("RunPod network volume destroy target")
    typer.echo(f"ID: {volume.id}")
    typer.echo(f"Name: {volume.name or '-'}")
    typer.echo(f"Data center: {volume.datacenter}")
    typer.echo(f"Size: {volume.size_gb} GB")
    typer.echo(
        "Storage tier: "
        + (volume.volume_type.value if volume.volume_type is not None else "unknown")
    )
    typer.echo(f"Tracked life cycle: {record.lifecycle_state.value if record else 'untracked'}")
    if mounting_workers:
        typer.echo(
            "Tracked Pods that mount this volume (they are NOT destroyed): "
            + ", ".join(sorted(mounting_workers))
        )
    typer.echo(
        "The rebuildable cache on this volume is permanently lost. Canonical S3 objects are "
        "not touched."
    )


def _print_untracked_volume_warning(
    volumes: list[NetworkVolume],
    records: list[VolumeRecord],
) -> None:
    """Warn about local intents the provider's list has not confirmed yet.

    A pending intent with no provider match is the visible evidence of an ambiguous create.
    None is ever hidden: a paid resource that may exist must never look like it does not.
    """

    provider_names = {volume.name for volume in volumes}
    for record in records:
        if (
            record.lifecycle_state is VolumeLifecycleState.PENDING_CREATE
            and record.infra_identity not in provider_names
        ):
            typer.echo(
                f"Warning: local state records an unresolved create intent "
                f"{record.infra_identity!r} ({record.requested_size_gb} GB in "
                f"{record.requested_data_center}) with no matching provider volume. RunPod "
                "may have created it; inspect the provider console before creating anything "
                "with that identity.",
                err=True,
            )


def _print_cache_stats(stats: CacheStats) -> None:
    typer.echo(f"Cache root: {stats.root}")
    typer.echo(f"Verified entries: {stats.entries}")
    typer.echo(f"Recorded cached bytes: {stats.cached_bytes} ({_gibibytes(stats.cached_bytes)})")
    typer.echo(f"Staged bytes (safe to delete): {stats.staging_bytes}")
    typer.echo(f"Entries with unusable metadata: {stats.unverified_entries}")
    typer.echo("Marker schema version: " + (stats.marker_schema_version or "absent"))
    typer.echo(
        "Recorded sizes are metadata, not a re-read: every entry is hashed whenever it is "
        "actually used. Explicit cleanup is operator-managed: remove "
        "a specific digest directory, or everything under staging/, with "
        "`infra worker exec <worker-id> -- rm -rf <path>`."
    )


def _gibibytes(value: int) -> str:
    return f"{value / (1024**3):.2f} GiB"


def _print_destroy_plan(worker: Worker, record: WorkerRecord | None) -> None:
    known_price = worker.hourly_cost
    if (known_price is None or known_price == 0) and record is not None:
        known_price = record.known_hourly_price
    typer.echo("RunPod destroy target")
    typer.echo(f"ID: {worker.id}")
    typer.echo(f"Name: {worker.name or '-'}")
    typer.echo(f"GPU: {worker.gpu_type or '-'}")
    typer.echo(f"GPU count: {worker.gpu_count if worker.gpu_count is not None else '-'}")
    typer.echo(f"State: {worker.state.value}")
    typer.echo(f"Known running price/hour: {_money(known_price)}")


def _print_health(report: WorkerHealthReport, *, json_output: bool) -> None:
    if json_output:
        payload = report.model_dump(mode="json")
        payload["ready"] = report.ready
        _print_json(payload)
        return
    typer.echo(f"Worker: {report.provider_worker_id}")
    typer.echo(f"Provider state: {report.provider_state.value}")
    typer.echo(f"Readiness: {report.readiness_state.value}")
    for check in report.checks:
        typer.echo(f"{check.status.value:4} {check.name}: {check.detail}")
    if report.gpu is not None:
        typer.echo(f"GPU count: {report.gpu.count}")
        typer.echo(f"GPU model(s): {', '.join(report.gpu.models) or '-'}")
        typer.echo(
            "GPU VRAM (MiB): " + (", ".join(str(value) for value in report.gpu.memory_mib) or "-")
        )
        typer.echo(f"NVIDIA driver: {report.gpu.driver_version or '-'}")
        typer.echo(f"CUDA compatibility: {report.gpu.cuda_version or '-'}")
    if report.disk_available_bytes is not None:
        gibibytes = report.disk_available_bytes / (1024**3)
        typer.echo(f"Disk available: {gibibytes:.2f} GiB at {report.disk_path}")


def _infra_worker_name(prefix: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", (prefix or "worker").strip().lower())
    normalized = normalized.strip("-")[:48] or "worker"
    return f"wavcse-{normalized}-{uuid4().hex[:12]}"


def _print_json(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True))


def _money(value: Decimal | None) -> str:
    return f"${value:.4f}" if value is not None else "unknown"


def _parse_price(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        price = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigurationError(f"Invalid --max-price value: {value!r}") from exc
    if not price.is_finite() or price < 0:
        raise ConfigurationError("--max-price must be a finite non-negative amount")
    return price


def _configuration_failure(exc: Exception) -> Never:
    typer.echo(f"Configuration error: {redact(exc)}", err=True)
    raise typer.Exit(code=2) from exc


def _provider_failure(exc: Exception, *, provider: str = "RunPod") -> Never:
    typer.echo(f"{provider} error: {redact(exc)}", err=True)
    raise typer.Exit(code=1) from exc


def _storage_failure(exc: Exception) -> Never:
    typer.echo(f"Storage error: {redact(exc)}", err=True)
    raise typer.Exit(code=1) from exc


def _operation_failure(exc: Exception) -> Never:
    typer.echo(f"Infrastructure error: {redact(exc)}", err=True)
    raise typer.Exit(code=1) from exc


def main() -> None:
    """Run the command-line application."""

    app()


if __name__ == "__main__":
    main()
