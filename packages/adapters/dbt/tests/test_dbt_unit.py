"""Testes rápidos do adapter dbt: nenhum deles roda o dbt de verdade."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest

from dengcode_adapter_dbt import manifest as mf
from dengcode_adapter_dbt.runner import find_dbt_executable, run_dbt, tail
from dengcode_adapter_dbt.transform import DbtTransform, project_fingerprint
from dengcode_core.errors import AdapterError, ConfigError, TransientError
from dengcode_core.ports.models import AdapterContext, LineageEdge, PortKind
from dengcode_core.ports.transform import TransformPort

RUN_RESULTS: dict[str, Any] = {
    "metadata": {"dbt_version": "1.12.7"},
    "results": [
        {
            "unique_id": "model.loja.silver_pedidos",
            "status": "success",
            "message": "OK",
            "execution_time": 0.25,
            "failures": None,
        },
        {
            "unique_id": "model.loja.quebrado",
            "status": "error",
            "message": 'Parser Error: syntax error at or near "from"',
            "execution_time": 0.01,
            "failures": None,
        },
        {
            "unique_id": "test.loja.unique_silver_pedidos_id_pedido.a1b2c3",
            "status": "fail",
            "message": "Got 2 results, configured to fail if != 0",
            "execution_time": 0.02,
            "failures": 2,
        },
        {
            "unique_id": "model.loja.gold_vendas",
            "status": "skipped",
            "message": None,
            "execution_time": 0,
            "failures": None,
        },
        {"status": "success"},  # sem unique_id: ignorado
    ],
}

MANIFEST: dict[str, Any] = {
    "nodes": {
        "model.loja.bronze_pedidos": {
            "resource_type": "model",
            "name": "bronze_pedidos",
            "unique_id": "model.loja.bronze_pedidos",
            "original_file_path": "models\\bronze\\bronze_pedidos.sql",
            "schema": "Main",
            "alias": "bronze_pedidos",
            "config": {"materialized": "view"},
            "depends_on": {"nodes": ["source.loja.raw.pedidos"]},
            "columns": {},
            "description": "",
        },
        "model.loja.silver_pedidos": {
            "resource_type": "model",
            "name": "silver_pedidos",
            "unique_id": "model.loja.silver_pedidos",
            "original_file_path": "models/silver/silver_pedidos.sql",
            "schema": "main",
            "alias": "pedidos_limpos",
            "config": {"materialized": "table"},
            "depends_on": {"nodes": ["model.loja.bronze_pedidos", "seed.loja.canais"]},
            "columns": {"id_pedido": {}, "status": {}},
            "description": "Pedidos limpos.",
            "compiled_code": "select * from main.bronze_pedidos",
        },
        "seed.loja.canais": {
            "resource_type": "seed",
            "name": "canais",
            "unique_id": "seed.loja.canais",
            "schema": "main",
            "alias": "canais",
        },
        "test.loja.unique_silver_pedidos_id_pedido.a1b2c3": {
            "resource_type": "test",
            "name": "unique_silver_pedidos_id_pedido",
            "unique_id": "test.loja.unique_silver_pedidos_id_pedido.a1b2c3",
            "schema": "main_dbt_test__audit",
            "depends_on": {"nodes": ["model.loja.silver_pedidos"]},
        },
    },
    "sources": {
        "source.loja.raw.pedidos": {
            "resource_type": "source",
            "name": "pedidos",
            "source_name": "raw",
            "schema": "raw",
            "identifier": "pedidos_csv",
            "unique_id": "source.loja.raw.pedidos",
        }
    },
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("success", "success"),
        ("pass", "pass"),
        ("error", "error"),
        ("runtime error", "error"),
        ("fail", "fail"),
        ("warn", "warn"),
        ("partial success", "warn"),
        ("skipped", "skipped"),
        ("no-op", "skipped"),
        ("SUCCESS", "success"),
        ("algo novo", "unknown"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_status_mapping(raw: str | None, expected: str) -> None:
    assert mf.map_status(raw) == expected


def test_node_results_from_run_results_json(tmp_path: Path) -> None:
    path = tmp_path / "run_results.json"
    path.write_text(json.dumps(RUN_RESULTS), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    results = mf.node_results(loaded, MANIFEST)
    assert [(r.name, r.status) for r in results] == [
        ("silver_pedidos", "success"),
        ("quebrado", "error"),
        ("unique_silver_pedidos_id_pedido", "fail"),
        ("gold_vendas", "skipped"),
    ]
    assert results[0].elapsed_s == pytest.approx(0.25)
    assert results[1].message is not None
    assert "syntax error" in results[1].message
    assert results[2].failures == 2
    assert results[3].message is None


def test_node_results_without_manifest_uses_unique_id() -> None:
    names = [r.name for r in mf.node_results(RUN_RESULTS)]
    assert names[:3] == ["silver_pedidos", "quebrado", "unique_silver_pedidos_id_pedido"]
    assert mf.node_results({"results": "inválido"}) == []


def test_model_infos_from_manifest() -> None:
    infos = {m.name: m for m in mf.model_infos(MANIFEST)}
    assert sorted(infos) == ["bronze_pedidos", "silver_pedidos"]
    bronze = infos["bronze_pedidos"]
    assert bronze.path == "models/bronze/bronze_pedidos.sql"  # "\" do Windows normalizado
    assert bronze.relation == "main.bronze_pedidos"
    assert bronze.materialized == "view"
    assert bronze.depends_on == ["raw.pedidos"]
    assert bronze.description is None
    silver = infos["silver_pedidos"]
    assert silver.relation == "main.pedidos_limpos"  # alias, não o nome
    assert silver.depends_on == ["bronze_pedidos", "canais"]
    assert silver.columns == ["id_pedido", "status"]
    assert silver.description == "Pedidos limpos."


def test_lineage_edges_from_manifest() -> None:
    edges = mf.lineage_edges(MANIFEST)
    assert edges == [
        LineageEdge(source="raw.pedidos_csv", target="main.bronze_pedidos", via="bronze_pedidos"),
        LineageEdge(
            source="main.bronze_pedidos", target="main.pedidos_limpos", via="silver_pedidos"
        ),
        LineageEdge(source="main.canais", target="main.pedidos_limpos", via="silver_pedidos"),
    ]


def test_compiled_models_by_name_and_unique_id() -> None:
    compiled = mf.compiled_models(MANIFEST)
    assert compiled["silver_pedidos"] == compiled["model.loja.silver_pedidos"]
    assert "bronze_pedidos" not in compiled  # sem compiled_code


def test_tail_truncates() -> None:
    assert tail("abc", 10) == "abc"
    cut = tail("x" * 50 + "fim", 10)
    assert cut.endswith("x" * 7 + "fim")
    assert cut.startswith("[...]")


# ------------------------------------------------------------------ executável
def _fake_exe(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("", encoding="utf-8")
    return path


def test_executable_beside_python(tmp_path: Path) -> None:
    scripts = tmp_path / ("Scripts" if os.name == "nt" else "bin")
    python = _fake_exe(scripts, "python.exe" if os.name == "nt" else "python")
    dbt = _fake_exe(scripts, "dbt.exe" if os.name == "nt" else "dbt")
    assert find_dbt_executable(python_executable=str(python)) == dbt


def test_executable_configured_path(tmp_path: Path) -> None:
    exe = _fake_exe(tmp_path / "ferramentas", "meu-dbt")
    assert find_dbt_executable(str(exe)) == exe.resolve()


def test_executable_falls_back_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    found = tmp_path / "no-path" / "dbt"
    monkeypatch.setattr("shutil.which", lambda name: str(found) if name == "dbt" else None)
    python = tmp_path / "sem-dbt" / "python"
    assert find_dbt_executable(python_executable=str(python)) == found


def test_missing_executable_is_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(ConfigError):
        find_dbt_executable(python_executable=str(tmp_path / "python"))
    with pytest.raises(ConfigError):
        find_dbt_executable(str(tmp_path / "nao-existe" / "dbt"))


async def test_operation_with_missing_executable_is_config_error(tmp_path: Path) -> None:
    (tmp_path / "dbt_project.yml").write_text("name: x\n", encoding="utf-8")
    transform = DbtTransform(tmp_path, dbt_executable=str(tmp_path / "nao-existe-dbt"))
    with pytest.raises(ConfigError):
        await transform.list_models()
    with pytest.raises(ConfigError):
        await transform.run("x", target="dev")


async def test_missing_project_is_config_error(tmp_path: Path) -> None:
    transform = DbtTransform(tmp_path / "sem-projeto")
    with pytest.raises(ConfigError):
        await transform.list_models()
    with pytest.raises(ConfigError):
        await transform.compile("qualquer")


async def test_selector_cannot_inject_flags(tmp_path: Path) -> None:
    transform = DbtTransform(tmp_path)
    with pytest.raises(AdapterError):
        await transform.run("--vars {x: 1}", target="dev")
    with pytest.raises(AdapterError):
        await transform.test("   ", target="dev")


# --------------------------------------------------------------------- runner
async def test_runner_timeout_kills_process(tmp_path: Path) -> None:
    started = time.perf_counter()
    with pytest.raises(TransientError):
        await run_dbt(
            Path(sys.executable),
            ["-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout_s=0.5,
            logger=logging.getLogger("teste"),
        )
    assert time.perf_counter() - started < 15


async def test_runner_captures_output_and_exit_code(tmp_path: Path) -> None:
    inv = await run_dbt(
        Path(sys.executable),
        ["-c", "import sys; print('olá, ação'); sys.stderr.write('erro\\n'); sys.exit(3)"],
        cwd=tmp_path,
        timeout_s=30,
        logger=logging.getLogger("teste"),
    )
    assert inv.returncode == 3
    assert not inv.ok
    assert "olá, ação" in inv.output
    assert "erro" in inv.output


# ---------------------------------------------------------------- config/meta
def test_from_config_resolves_relative_to_project(tmp_path: Path) -> None:
    ctx = AdapterContext(workspace="t", project_dir=tmp_path)
    transform = DbtTransform.from_config(
        {"project_dir": "transform/dbt", "profiles_dir": "perfis", "target": "dev", "timeout_s": 5},
        ctx,
    )
    assert transform.project_dir == (tmp_path / "transform" / "dbt").resolve()
    assert transform.profiles_dir == (tmp_path / "perfis").resolve()
    assert transform.target == "dev"
    assert transform.timeout_s == 5
    default = DbtTransform.from_config({"project_dir": "dbt"}, ctx)
    assert default.profiles_dir == default.project_dir


@pytest.mark.parametrize(
    "cfg", [{}, {"project_dir": "dbt", "timeout_s": "rápido"}, {"project_dir": "d", "timeout_s": 0}]
)
def test_from_config_invalid(tmp_path: Path, cfg: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        DbtTransform.from_config(cfg, AdapterContext(workspace="t", project_dir=tmp_path))


def test_info_and_entry_point() -> None:
    assert DbtTransform.info.name == "dbt_core"
    assert DbtTransform.info.port is PortKind.TRANSFORM
    assert DbtTransform.info.knowledge_pack == "dengcode_adapter_dbt:knowledge/dbt.md"
    assert isinstance(DbtTransform(Path.cwd()), TransformPort)
    eps = {ep.name: ep.value for ep in entry_points(group="dengcode.adapters")}
    assert eps["dbt_core"] == "dengcode_adapter_dbt.transform:DbtTransform"


def test_fingerprint_ignores_generated_files(tmp_path: Path) -> None:
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "dbt_project.yml").write_text("name: x\n", encoding="utf-8")
    before = project_fingerprint(tmp_path)
    for generated in ("target", "logs"):
        (tmp_path / generated).mkdir()
        (tmp_path / generated / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".user.yml").write_text("id: 1\n", encoding="utf-8")
    (tmp_path / "banco.duckdb").write_bytes(b"x")
    assert project_fingerprint(tmp_path) == before
    (tmp_path / "models" / "a.sql").write_text("select 1 as alterado", encoding="utf-8")
    assert project_fingerprint(tmp_path) != before
    assert [p for p, _, _ in project_fingerprint(tmp_path)] == ["dbt_project.yml", "models/a.sql"]


async def test_runner_cancel_kills_process(tmp_path: Path) -> None:
    marker = tmp_path / "terminou.txt"
    code = f"import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('x')"
    task = asyncio.create_task(
        run_dbt(
            Path(sys.executable),
            ["-c", code],
            cwd=tmp_path,
            timeout_s=60,
            logger=logging.getLogger("teste"),
        )
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(3)
    assert not marker.exists()  # o processo morreu antes de terminar
