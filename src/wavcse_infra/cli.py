"""Public `infra` command-line interface."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Never
from uuid import uuid4

import typer
from pydantic import ValidationError

from wavcse_infra import __version__
from wavcse_infra.config import Settings, load_settings, resolved_config_path
from wavcse_infra.doctor import CheckStatus, DoctorReport, run_doctor
from wavcse_infra.errors import (
    ConfigurationError,
    InfraError,
    JobError,
    JobSpecError,
    ProviderError,
    ProviderNotFoundError,
    SshEndpointUnavailableError,
    StateError,
    StorageError,
    StorageObjectNotFoundError,
)
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
    Worker,
    WorkerConnectionInfo,
    WorkerCreationPlan,
    WorkerHealthReport,
    WorkerSpec,
    WorkerState,
)
from wavcse_infra.providers.runpod import RunPodClient
from wavcse_infra.redaction import redact
from wavcse_infra.state import WorkerRecord, WorkerStateStore
from wavcse_infra.storage.manifests import ArtifactManifest, load_manifest_json
from wavcse_infra.storage.s3 import (
    MAX_PRESIGN_EXPIRY_SECONDS,
    MIN_PRESIGN_EXPIRY_SECONDS,
    S3Storage,
    StorageVerification,
)
from wavcse_infra.storage.transfer import (
    ArtifactTransferResult,
    WorkerArtifactTransfer,
)
from wavcse_infra.storage.worker_transfer import MAX_DOWNLOAD_CONCURRENCY
from wavcse_infra.workers.bootstrap import WorkerBootstrapper
from wavcse_infra.workers.lifecycle import WorkerLifecycle
from wavcse_infra.workers.ssh import SshExecutor, WorkerSshWaiter, select_worker_connection

app = typer.Typer(
    name="infra",
    help="Operate reproducible wavCSE infrastructure.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Validate controller configuration.", no_args_is_help=True)
worker_app = typer.Typer(help="Manage RunPod GPU workers.", no_args_is_help=True)
storage_app = typer.Typer(help="Inspect and transfer canonical S3 artifacts.", no_args_is_help=True)
job_app = typer.Typer(help="Submit and inspect recorded exact-commit jobs.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(worker_app, name="worker")
app.add_typer(storage_app, name="storage")
app.add_typer(job_app, name="job")


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
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """List RunPod workers and reconcile tracked local metadata."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            workers = client.list_workers()
        _reconcile_state(workers)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    if json_output:
        _print_json([worker.model_dump(mode="json") for worker in workers])
        return
    if not workers:
        typer.echo("No RunPod workers found.")
        return

    typer.echo("ID\tSTATE\tGPU\tCOUNT\tCLOUD\tCOST/HR\tNAME")
    for worker in workers:
        typer.echo(
            "\t".join(
                (
                    worker.id,
                    worker.state.value,
                    worker.gpu_type or "-",
                    str(worker.gpu_count) if worker.gpu_count is not None else "-",
                    worker.cloud_type.value if worker.cloud_type is not None else "-",
                    _money(worker.hourly_cost),
                    worker.name or "-",
                )
            )
        )


@worker_app.command("show")
def show_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="Exact RunPod worker ID.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Render normalized machine-readable JSON.")
    ] = False,
) -> None:
    """Show one RunPod worker by exact provider ID."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient.from_settings(settings) as client:
            worker = client.get_worker(worker_id)
        _observe_state(worker)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderNotFoundError as exc:
        _mark_state_destroyed(worker_id)
        _provider_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    if json_output:
        _print_json(worker.model_dump(mode="json"))
    else:
        _print_worker(worker)


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
    gpu: Annotated[str, typer.Option("--gpu", help="Exact RunPod GPU type ID.")],
    cloud: Annotated[
        CloudType,
        typer.Option("--cloud", case_sensitive=False, help="RunPod cloud tier."),
    ],
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
    volume: Annotated[
        int,
        typer.Option("--volume", min=0, help="Host-local persistent volume in GB; 0 disables."),
    ] = 0,
    volume_mount_path: Annotated[
        str, typer.Option("--volume-mount-path", help="Persistent/network volume mount path.")
    ] = "/workspace",
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
    """Plan, confirm, create, persist, and wait for one RunPod Pod."""

    settings = _load_cli_settings(_context(context))
    try:
        spec = WorkerSpec(
            name=_infra_worker_name(name),
            gpu_type=gpu,
            gpu_count=gpu_count,
            cloud_type=cloud,
            image=image,
            template_id=template,
            container_disk_gb=container_disk,
            volume_gb=volume,
            volume_mount_path=volume_mount_path,
            network_volume_id=network_volume_id,
            data_center_ids=tuple(data_center or ()),
            interruptible=interruptible,
            start_ssh=start_ssh,
            require_direct_ssh=require_direct_ssh,
        )
    except ValidationError as exc:
        _configuration_failure(exc)

    try:
        with RunPodClient.from_settings(settings) as client:
            lifecycle = _lifecycle(client, settings)
            plan = lifecycle.plan_create(
                spec,
                max_hourly_price=_parse_price(max_price),
            )
            _print_creation_plan(plan)
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
    try:
        with RunPodClient.from_settings(settings) as client:
            bootstrapper, _ = _worker_access(client, settings)
            report = bootstrapper.bootstrap(
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
        typer.echo(
            f"Worker {worker_id} bootstrap completed, but required health checks failed; "
            "local readiness is FAILED.",
            err=True,
        )
        raise typer.Exit(code=1)


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
    try:
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
        typer.Argument(help="Exact RunPod worker ID; names are not accepted."),
    ],
    wait_timeout: Annotated[
        float | None,
        typer.Option("--wait-timeout", min=0.1, help="Lifecycle polling timeout in seconds."),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Bypass only the interactive destroy confirmation.")
    ] = False,
) -> None:
    """Permanently terminate one exact-ID Pod after explicit confirmation."""

    settings = _load_cli_settings(_context(context))
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
        str, typer.Option("--worker", help="Exact RunPod worker ID that receives the artifact.")
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
        with RunPodClient.from_settings(settings) as client:
            result = _worker_transfer(client, settings).download(
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
        str, typer.Option("--worker", help="Exact RunPod worker ID that uploads the artifact.")
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
        with RunPodClient.from_settings(settings) as client:
            outcome = _worker_transfer(client, settings).upload(
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
        str, typer.Option("--worker", help="Exact RunPod worker ID that executes the job.")
    ],
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
    """Submit one recorded job to an explicit existing READY worker.

    The worker must already exist and be READY. Phase 6 never creates, bootstraps,
    starts, or destroys a worker implicitly, and it never reruns a failed job.
    """

    settings = _load_cli_settings(_context(context))
    try:
        spec = _load_job_spec_file(job_spec)
        with RunPodClient.from_settings(settings) as client:
            job_context = _job_context(client, settings)
            record = JobSubmitter(job_context).submit(spec, worker_id=worker)
            if wait:
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
    """Reconcile one job with real worker evidence and report its durable state."""

    settings = _load_cli_settings(_context(context))
    canonical = _canonical_job_id(job_id)
    try:
        with RunPodClient.from_settings(settings) as client:
            job_context = _job_context(client, settings)
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
        with RunPodClient.from_settings(settings) as client:
            job_context = _job_context(client, settings)
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
    """Cancel one running job process on its worker; the worker itself is untouched."""

    settings = _load_cli_settings(_context(context))
    canonical = _canonical_job_id(job_id)
    try:
        with RunPodClient.from_settings(settings) as client:
            job_context = _job_context(client, settings)
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
            f"Job {record.job_id} cancelled on RunPod worker {record.worker_id}; the worker "
            "was not stopped or destroyed."
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
    return JobContext(
        provider=client,
        worker_state=state_store,
        job_store=_job_store(),
        executor=JobExecutor(waiter, executor, settings.ssh, settings.jobs),
        transfer=WorkerArtifactTransfer(waiter, executor, settings.ssh),
        storage=_optional_storage(settings),
        jobs_config=settings.jobs,
        environ=os.environ,
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


def _format_connection(connection: WorkerConnectionInfo | None) -> str | None:
    if connection is None:
        return None
    host = f"[{connection.host}]" if ":" in connection.host else connection.host
    return f"{connection.username}@{host}:{connection.port}"


def _print_creation_plan(plan: WorkerCreationPlan) -> None:
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
    if offer.total_price_per_hour is None:
        typer.echo("WARNING: RunPod did not provide a reliable pre-creation GPU price.")


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


def _provider_failure(exc: Exception) -> Never:
    typer.echo(f"RunPod error: {redact(exc)}", err=True)
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
