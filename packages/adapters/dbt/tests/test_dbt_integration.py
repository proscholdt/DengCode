"""Testes que rodam o dbt de verdade (subprocesso) sobre uma cópia do exemplo.

Cada teste copia ``examples/ecommerce`` (CSVs + projeto dbt) para ``tmp_path``,
mantendo o layout ``../{nome}.csv`` que as fontes do projeto usam.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dengcode_adapter_dbt.transform import DbtTransform
from dengcode_core.ports.models import AdapterContext
from dengcode_testkit.contracts.transform import TransformContract

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[4] / "examples" / "ecommerce"
_GENERATED = shutil.ignore_patterns(
    "target", "logs", "dbt_packages", "*.duckdb", "*.duckdb.wal", ".user.yml", "__pycache__"
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "ecommerce"
    root.mkdir()
    for csv in EXAMPLE.glob("*.csv"):
        shutil.copy2(csv, root / csv.name)
    shutil.copytree(EXAMPLE / "dbt", root / "dbt", ignore=_GENERATED)
    return root / "dbt"


@pytest.fixture
def transform(project: Path) -> DbtTransform:
    ctx = AdapterContext(workspace="teste", project_dir=project.parent)
    return DbtTransform.from_config({"project_dir": "dbt", "target": "dev", "timeout_s": 300}, ctx)


class TestDbtContract(TransformContract):
    @pytest.fixture
    def known_model(self) -> str:
        return "silver_pedidos"

    @pytest.fixture
    def upstream_model(self) -> str:
        return "bronze_pedidos"


async def test_example_project_builds_green(transform: DbtTransform) -> None:
    ran = await transform.run("ecommerce", target="dev")
    assert ran.success, ran.logs
    assert {r.name for r in ran.results} >= {
        "bronze_pedidos",
        "silver_pedidos",
        "silver_clientes",
        "gold_vendas_diarias",
    }
    tested = await transform.test("ecommerce", target="dev")
    assert tested.success, tested.logs
    assert len(tested.results) >= 5
    assert all(r.status == "pass" for r in tested.results)


async def test_broken_model_is_a_failed_result(transform: DbtTransform, project: Path) -> None:
    (project / "models" / "silver" / "quebrado.sql").write_text(
        "select from from {{ ref('bronze_pedidos') }}\n", encoding="utf-8"
    )
    result = await transform.run("quebrado", target="dev")
    assert not result.success
    assert result.command == "run"
    broken = [r for r in result.results if r.name == "quebrado"]
    assert broken
    assert broken[0].status == "error"
    assert broken[0].message
    assert "quebrado" in result.logs
    assert len(result.logs) <= 4100


async def test_selector_filters_list_models(transform: DbtTransform) -> None:
    upstream = await transform.list_models("+silver_pedidos")
    assert sorted(m.name for m in upstream) == ["bronze_pedidos", "silver_pedidos"]
    everything = {m.name: m for m in await transform.list_models()}
    assert set(everything) == {
        "bronze_clientes",
        "bronze_itens_pedido",
        "bronze_pedidos",
        "gold_vendas_diarias",
        "silver_clientes",
        "silver_pedidos",
    }
    silver = everything["silver_pedidos"]
    assert silver.relation == "main.silver_pedidos"
    assert silver.materialized == "table"
    assert silver.path == "models/silver/silver_pedidos.sql"
    assert "id_pedido" in silver.columns
    assert "canal" not in silver.columns  # o cenário da fase 4 adiciona canal_venda
    assert everything["bronze_pedidos"].depends_on == ["raw.pedidos"]
    assert await transform.list_models("tag:nao_existe") == []


async def test_lineage_includes_sources(transform: DbtTransform) -> None:
    edges = {(e.source, e.target, e.via) for e in await transform.manifest_lineage()}
    assert ("raw.pedidos", "main.bronze_pedidos", "bronze_pedidos") in edges
    assert ("main.silver_pedidos", "main.gold_vendas_diarias", "gold_vendas_diarias") in edges


async def test_cache_invalidates_after_editing_model(
    transform: DbtTransform, project: Path
) -> None:
    before = await transform.compile("gold_vendas_diarias")
    assert "-- marcador_edicao" not in before
    model = project / "models" / "gold" / "gold_vendas_diarias.sql"
    model.write_text(model.read_text(encoding="utf-8") + "\n-- marcador_edicao\n", encoding="utf-8")
    (project / "models" / "gold" / "gold_novo.sql").write_text(
        "select * from {{ ref('gold_vendas_diarias') }}\n", encoding="utf-8"
    )
    after = await transform.compile("gold_vendas_diarias")
    assert "-- marcador_edicao" in after
    names = {m.name for m in await transform.list_models()}
    assert "gold_novo" in names
    edges = {(e.source, e.target) for e in await transform.manifest_lineage()}
    assert ("main.gold_vendas_diarias", "main.gold_novo") in edges
