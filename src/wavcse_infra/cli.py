"""Public `infra` command-line interface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from wavcse_infra import __version__
from wavcse_infra.config import load_settings, resolved_config_path
from wavcse_infra.errors import ConfigurationError

app = typer.Typer(
    name="infra",
    help="Operate reproducible wavCSE infrastructure.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Validate controller configuration.", no_args_is_help=True)
app.add_typer(config_app, name="config")


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
    try:
        load_settings(
            config_path=cli_context.config_path,
            cli_overrides=cli_context.cli_overrides,
        )
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    path = resolved_config_path(cli_context.config_path)
    source = str(path) if path.exists() else "defaults and environment"
    typer.echo(f"Configuration valid ({source}).")


def _context(context: typer.Context) -> CliContext:
    root_context = context.find_root().obj
    if not isinstance(root_context, CliContext):
        raise RuntimeError("CLI context was not initialized")
    return root_context


def main() -> None:
    """Run the command-line application."""

    app()


if __name__ == "__main__":
    main()
