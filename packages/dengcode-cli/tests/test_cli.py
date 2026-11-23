from __future__ import annotations

from typer.testing import CliRunner

from dengcode_cli import __version__
from dengcode_cli.main import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_adapters_list_shows_local_fs() -> None:
    result = runner.invoke(app, ["adapters", "list"])
    assert result.exit_code == 0
    assert "local_fs" in result.output
