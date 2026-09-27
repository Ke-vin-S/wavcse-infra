"""Public `infra` command-line interface."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Never

import typer

from wavcse_infra import __version__
from wavcse_infra.config import Settings, load_settings, resolved_config_path
from wavcse_infra.doctor import CheckStatus, DoctorReport, run_doctor
from wavcse_infra.errors import ConfigurationError, ProviderError
from wavcse_infra.models import Worker
from wavcse_infra.providers.runpod import RunPodClient
from wavcse_infra.redaction import redact

app = typer.Typer(
    name="infra",
    help="Operate reproducible wavCSE infrastructure.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Validate controller configuration.", no_args_is_help=True)
worker_app = typer.Typer(help="Inspect RunPod GPU workers.", no_args_is_help=True)
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
def list_workers(context: typer.Context) -> None:
    """List RunPod workers without changing provider state."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient(settings.runpod) as client:
            workers = client.list_workers()
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    if not workers:
        typer.echo("No RunPod workers found.")
        return

    typer.echo("ID\tSTATE\tGPU\tCOUNT\tCOST/HR\tNAME")
    for worker in workers:
        typer.echo(
            "\t".join(
                (
                    worker.id,
                    worker.state.value,
                    worker.gpu_type or "-",
                    str(worker.gpu_count) if worker.gpu_count is not None else "-",
                    _money(worker.hourly_cost),
                    worker.name or "-",
                )
            )
        )


@worker_app.command("show")
def show_worker(
    context: typer.Context,
    worker_id: Annotated[str, typer.Argument(help="RunPod worker ID.")],
) -> None:
    """Show one RunPod worker without changing provider state."""

    settings = _load_cli_settings(_context(context))
    try:
        with RunPodClient(settings.runpod) as client:
            worker = client.get_worker(worker_id)
    except ConfigurationError as exc:
        _configuration_failure(exc)
    except ProviderError as exc:
        _provider_failure(exc)
    _print_worker(worker)


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
        ("Hourly cost", _money(worker.hourly_cost)),
        ("Base hourly cost", _money(worker.base_hourly_cost)),
        ("Public IP", worker.public_ip),
        ("SSH port", worker.ssh_port),
        ("Datacenter", worker.datacenter),
        ("Image", worker.image),
        ("Interruptible", worker.interruptible),
        ("Last started", worker.last_started_at.isoformat() if worker.last_started_at else None),
    )
    for label, value in fields:
        typer.echo(f"{label}: {value if value is not None else '-'}")


def _money(value: Decimal | None) -> str:
    return f"{value:.4f}" if value is not None else "-"


def _configuration_failure(exc: Exception) -> Never:
    typer.echo(f"Configuration error: {redact(exc)}", err=True)
    raise typer.Exit(code=2) from exc


def _provider_failure(exc: Exception) -> Never:
    typer.echo(f"RunPod error: {redact(exc)}", err=True)
    raise typer.Exit(code=1) from exc


def main() -> None:
    """Run the command-line application."""

    app()


if __name__ == "__main__":
    main()
