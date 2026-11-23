from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dengcode_cli import __version__
from dengcode_cli.main import app
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_testkit.fake_llm import FakeLLMAdapter

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DENGCODE_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".dengcode" / "stacks").mkdir(parents=True)
    (proj / ".dengcode" / "stacks" / "t.yaml").write_text(
        "name: t\nllm: { provider: fake, script: [feito] }\n", encoding="utf-8"
    )
    real = AdapterRegistry.discover

    def discover(*_: object, **__: object) -> AdapterRegistry:
        fake = RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter)
        return AdapterRegistry([*real().all(), fake])

    monkeypatch.setattr(AdapterRegistry, "discover", staticmethod(discover))
    return proj


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_adapters_list_shows_installed() -> None:
    result = runner.invoke(app, ["adapters", "list"])
    assert result.exit_code == 0
    assert "local_fs" in result.output
    assert "anthropic" in result.output


def test_stack_list_and_validate(project: Path) -> None:
    listed = runner.invoke(app, ["stack", "list", "--project", str(project)])
    assert listed.exit_code == 0
    assert "opensource" in listed.output
    ok = runner.invoke(app, ["stack", "validate", "t", "--project", str(project)])
    assert ok.exit_code == 0, ok.output
    aws = runner.invoke(app, ["stack", "validate", "aws", "--project", str(project)])
    assert aws.exit_code == 1
    assert "não instalado" in aws.output


def test_run_json_headless(project: Path) -> None:
    args = ["run", "faça", "--json", "--stack", "t", "--project", str(project)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    lines = [json.loads(x) for x in result.output.splitlines() if x.startswith("{")]
    assert lines[-1]["type"] == "result"
    assert lines[-1]["reason"] == "end"
    assert any(e["type"] == "assistant_text" for e in lines)


def test_run_reports_config_error_as_json(project: Path) -> None:
    args = ["run", "x", "--json", "--stack", "nao-existe", "--project", str(project)]
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert json.loads(result.output.splitlines()[0])["type"] == "error"


def test_undo_without_checkpoints(project: Path) -> None:
    result = runner.invoke(app, ["undo", "--project", str(project)])
    assert result.exit_code == 1
    assert "nada para desfazer" in result.output
