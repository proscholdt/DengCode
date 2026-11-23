from __future__ import annotations

from pathlib import Path

import pytest

from dengcode_core.errors import ConfigError
from dengcode_engine.config.credentials import load_dotenv, parse_dotenv, resolve_value
from dengcode_engine.config.loader import StackLoader, deep_merge

PRESETS = ("local", "opensource", "azure", "aws", "google")


def _write(folder: Path, name: str, body: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    project, home = tmp_path / "proj", tmp_path / "home"
    project.mkdir()
    return project, home


def test_builtin_presets_load(dirs: tuple[Path, Path]) -> None:
    loader = StackLoader(*dirs)
    assert set(PRESETS) <= {s.name for s in loader.list()}
    for name in PRESETS:
        assert loader.load(name).name == name


def test_placeholders_are_reported(dirs: tuple[Path, Path]) -> None:
    assert "llm.models.main" in StackLoader(*dirs).load("aws").placeholders()
    assert StackLoader(*dirs).load("local").placeholders() == []


def test_extends_merges_dicts_replaces_lists_and_null_removes(dirs: tuple[Path, Path]) -> None:
    project, home = dirs
    body = (
        "name: minha\n"
        "extends: azure\n"
        "llm: { provider: anthropic, model: claude-opus-5-5 }\n"
        "compute: [ { adapter: duckdb } ]\n"
        "orchestrator: null\n"
        "environments:\n"
        "  dev: { permission_mode: auto }\n"
    )
    _write(project / ".dengcode" / "stacks", "minha", body)
    stack = StackLoader(project, home).load("minha")
    assert stack.extends == "azure"
    assert stack.llm is not None
    assert stack.llm.models is not None
    assert stack.llm.models.main == stack.llm.models.fast == "claude-opus-5-5"
    assert [c.adapter for c in stack.compute] == ["duckdb"]  # lista substituída
    assert stack.orchestrator is None  # null remove
    assert stack.catalog is not None
    assert stack.catalog.adapter == "unity_catalog"  # herdado
    assert stack.environments["dev"].permission_mode == "auto"
    assert stack.environments["prod"].read_only  # merge profundo manteve prod


def test_project_beats_user_beats_builtin(dirs: tuple[Path, Path]) -> None:
    project, home = dirs
    _write(home / "stacks", "local", "name: local\ndescription: do usuário\n")
    assert StackLoader(project, home).load("local").description == "do usuário"
    _write(project / ".dengcode" / "stacks", "local", "name: local\ndescription: do projeto\n")
    assert StackLoader(project, home).load("local").description == "do projeto"
    assert StackLoader(project, home).source("local").origin == "projeto"


def test_extends_cycle_is_error(dirs: tuple[Path, Path]) -> None:
    project, home = dirs
    folder = project / ".dengcode" / "stacks"
    _write(folder, "a", "name: a\nextends: b\n")
    _write(folder, "b", "name: b\nextends: a\n")
    with pytest.raises(ConfigError, match="ciclo"):
        StackLoader(project, home).load("a")


def test_unknown_key_and_missing_stack(dirs: tuple[Path, Path]) -> None:
    project, home = dirs
    _write(project / ".dengcode" / "stacks", "ruim", "name: ruim\ncomputee: []\n")
    with pytest.raises(ConfigError, match="inválida"):
        StackLoader(project, home).load("ruim")
    with pytest.raises(ConfigError, match="não encontrada"):
        StackLoader(project, home).load("nao-existe")


def test_deep_merge() -> None:
    merged = deep_merge({"a": {"x": 1, "y": 2}, "l": [1]}, {"a": {"y": 3}, "l": [2]})
    assert merged == {"a": {"x": 1, "y": 3}, "l": [2]}


def test_dotenv_parsing_and_loading(tmp_path: Path) -> None:
    text = '# comentário\nexport A_KEY="segredo com espaço"\nB=2 # nota\nC=\nJA=novo\n'
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    assert parse_dotenv(text)["A_KEY"] == "segredo com espaço"
    env = {"JA": "antigo"}
    loaded = load_dotenv(tmp_path, env)
    assert sorted(loaded) == ["A_KEY", "B"]
    assert env["JA"] == "antigo"  # não sobrescreve
    assert env["B"] == "2"


def test_resolve_env_references() -> None:
    env = {"K": "v"}
    resolved = resolve_value({"api_key": "env://K", "n": [1, "env://K"]}, env)
    assert resolved == {"api_key": "v", "n": [1, "v"]}
    with pytest.raises(ConfigError, match="FALTA"):
        resolve_value("env://FALTA", env)
