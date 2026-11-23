from __future__ import annotations

import pytest

from dengcode_core.lineage.columns import build_column_lineage, column_lineage, output_columns
from dengcode_core.lineage.graph import LineageGraph
from dengcode_core.policy.permission import Decision, PermissionPolicy
from dengcode_core.ports.models import LineageEdge
from dengcode_core.tools.base import ActionRequest, Risk

GOLD = """
with base as (
  select data_pedido, valor_total, canal
  from {silver}
  where status <> 'cancelado'
)
select data_pedido, canal as canal_venda, count(*) as pedidos, sum(valor_total) as receita
from base group by 1, 2
"""

SILVER_REFS = {
    "duckdb": '"ecommerce"."main"."silver_pedidos"',
    "postgres": '"ecommerce"."main"."silver_pedidos"',
    "spark": "`ecommerce`.`main`.`silver_pedidos`",
    "bigquery": "`ecommerce.main.silver_pedidos`",
    "tsql": "[ecommerce].[main].[silver_pedidos]",
    "trino": '"ecommerce"."main"."silver_pedidos"',
}


def _pairs(edges: list[LineageEdge]) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in edges}


@pytest.mark.parametrize("dialect", sorted(SILVER_REFS))
def test_column_lineage_across_dialects(dialect: str) -> None:
    sql = GOLD.format(silver=SILVER_REFS[dialect])
    edges = column_lineage("main.gold_vendas_diarias", sql, dialect)
    assert _pairs(edges) >= {
        ("main.silver_pedidos.data_pedido", "main.gold_vendas_diarias.data_pedido"),
        ("main.silver_pedidos.canal", "main.gold_vendas_diarias.canal_venda"),
        ("main.silver_pedidos.valor_total", "main.gold_vendas_diarias.receita"),
    }
    # count(*) não tem coluna de origem
    assert not any(t.endswith(".pedidos") for _, t in _pairs(edges))


def test_output_columns() -> None:
    assert output_columns("select a, b as c, d + 1 as e from t", "duckdb") == ["a", "c", "e"]
    assert output_columns("delete from t", "duckdb") == []
    assert output_columns("isso não é sql (", "duckdb") == []


def test_chain_and_select_star_through_models() -> None:
    models = {
        "main.silver_pedidos": "select cast(id as int) as id_pedido, lower(canal) as canal "
        "from main.bronze_pedidos",
        "main.silver_ativos": "select * from main.silver_pedidos where id_pedido > 0",
        "main.gold": "select canal, count(*) as n from main.silver_ativos group by 1",
    }
    edges = build_column_lineage(models, "duckdb")
    graph = LineageGraph(edges)
    found = graph.resolve("bronze_pedidos.canal")
    assert found == ("main.bronze_pedidos.canal", "column")
    impacted = {i.entity for i in graph.walk(found[0])}
    assert "main.silver_pedidos.canal" in impacted
    assert "main.gold.canal" in impacted  # passou pelo SELECT * do modelo intermediário
    tables = {i.entity for i in graph.walk("main.bronze_pedidos")}
    assert tables == {"main.silver_pedidos", "main.silver_ativos", "main.gold"}
    up = graph.walk("main.gold", direction="up")
    assert up[-1].entity == "main.bronze_pedidos"
    assert up[-1].path == (
        "main.gold",
        "main.silver_ativos",
        "main.silver_pedidos",
        "main.bronze_pedidos",
    )


def test_graph_resolve_and_table_edges() -> None:
    graph = LineageGraph(table_edges=[LineageEdge(source="raw.x", target="main.y", via="y")])
    assert graph.resolve("y") == ("main.y", "table")
    assert graph.resolve("nao_existe") is None
    [item] = graph.walk("raw.x")
    assert item.via == "y"


def test_scratch_schema_writes_are_allowed_in_dev_only() -> None:
    req = ActionRequest(
        "data_snapshot",
        Risk.SQL,
        details={
            "sql_kind": "ddl",
            "tables_written": ["dengcode_tmp_abc.gold_antes"],
            "dangerous": [],
        },
    )
    assert PermissionPolicy().decide(req).decision is Decision.ALLOW
    assert PermissionPolicy(env="prod").decide(req).decision is Decision.DENY
    other = ActionRequest(
        "query_run",
        Risk.SQL,
        details={
            "sql_kind": "ddl",
            "tables_written": ["dengcode_tmp_abc.x", "main.y"],
            "dangerous": [],
        },
    )
    assert PermissionPolicy().decide(other).decision is Decision.ASK
