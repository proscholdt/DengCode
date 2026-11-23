"""Ponto de entrada da CLI ``dengcode``."""

from __future__ import annotations

import sys

import typer
from rich.console import Console
from rich.table import Table

from dengcode_cli import __version__
from dengcode_engine.registry import AdapterRegistry

app = typer.Typer(
    name="dengcode",
    help="DengCode — assistente de IA para engenharia de dados.",
    no_args_is_help=True,
    add_completion=False,
)
adapters_app = typer.Typer(help="Adapters instalados.", no_args_is_help=True)
app.add_typer(adapters_app, name="adapters")


def _ensure_utf8() -> None:
    """Console do Windows usa outra codificação por padrão; a CLI fala UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_ensure_utf8()
console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"dengcode {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Mostra a versão e sai.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """DengCode — assistente de IA para engenharia de dados."""


@adapters_app.command("list")
def adapters_list() -> None:
    """Lista os adapters descobertos por entry points."""
    registry = AdapterRegistry.discover()
    table = Table(title="Adapters instalados")
    table.add_column("porta")
    table.add_column("nome")
    table.add_column("versão")
    table.add_column("pacote")
    for item in registry.all():
        if item.info is None:
            table.add_row("?", item.entry_point, "-", f"[red]erro: {item.error}[/red]")
            continue
        table.add_row(
            item.info.port.value, item.info.name, item.info.version, item.distribution or "-"
        )
    console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
