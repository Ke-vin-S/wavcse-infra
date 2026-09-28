"""Public `infra` command-line interface."""

from __future__ import annotations

import json
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
    ProviderError,
    ProviderNotFoundError,
    StateError,
)
from wavcse_infra.models import (
    CloudType,
    Worker,
    WorkerCreationPlan,
    WorkerHealthReport,
    WorkerSpec,
)
from wavcse_infra.providers.runpod import RunPodClient
from wavcse_infra.redaction import redact
from wavcse_infra.state import WorkerRecord, WorkerStateStore
from wavcse_infra.workers.bootstrap import WorkerBootstrapper
from wavcse_infra.workers.lifecycle import WorkerLifecycle
from wavcse_infra.workers.ssh import SshExecutor, WorkerSshWaiter

app = typer.Typer(
    name="infra",
    help="Operate reproducible wavCSE infrastructure.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Validate controller configuration.", no_args_is_help=True)
worker_app = typer.Typer(help="Manage RunPod GPU workers.", no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(worker_app, name="worker")


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
            )
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    if json_output:
        _print_json([offer.model_dump(mode="json") for offer in offers])
        return
    typer.echo("GPU TYPE ID\tVRAM\tAVAILABILITY\tMAX COUNT\tPRICE/GPU-HR\tTOTAL/HR")
    for offer in offers:
        typer.echo(
            "\t".join(
                (
                    offer.gpu_type_id,
                    f"{offer.memory_gb} GB" if offer.memory_gb is not None else "-",
                    offer.availability.value,
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
        ("Exposed ports", ", ".join(worker.exposed_ports) or None),
        ("Created", worker.created_at.isoformat() if worker.created_at else None),
        ("Last started", worker.last_started_at.isoformat() if worker.last_started_at else None),
    )
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")


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


def _operation_failure(exc: Exception) -> Never:
    typer.echo(f"Infrastructure error: {redact(exc)}", err=True)
    raise typer.Exit(code=1) from exc


def main() -> None:
    """Run the command-line application."""

    app()


if __name__ == "__main__":
    main()
