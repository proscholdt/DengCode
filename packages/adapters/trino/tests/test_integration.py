"""Contratos e comportamento do adapter contra um Trino real (testcontainers).

Escrita só no catálogo ``memory`` do container descartável.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from dengcode_adapter_trino.catalog import TrinoCatalog
from dengcode_adapter_trino.engine import TrinoQueryEngine
from dengcode_core.errors import AdapterError, NotFoundError, TransientError
from dengcode_core.ports.models import AdapterContext, TableRef
from dengcode_testkit.contracts.data import CatalogContract, QueryEngineContract

pytestmark = pytest.mark.integration

CTX = AdapterContext(workspace="teste")
# consulta pesada de propósito: só termina por tempo limite ou cancelamento
SLOW_SQL = "SELECT count(*) FROM tpch.sf1000.lineitem WHERE comment LIKE '%dengcode%'"


@pytest.fixture
def engine(trino_config: dict[str, Any]) -> TrinoQueryEngine:
    return TrinoQueryEngine.from_config(trino_config, CTX)


async def _wait_running(engine: TrinoQueryEngine) -> str:
    for _ in range(200):
        if engine.client.running:
            return next(iter(engine.client.running))
        await asyncio.sleep(0.05)
    raise AssertionError("a consulta não começou")


async def _server_state(engine: TrinoQueryEngine, query_id: str) -> str:
    """Estado final da consulta no servidor (espera sair de RUNNING)."""
    state = ""
    for _ in range(50):
        result = await engine.execute(
            f"SELECT state FROM system.runtime.queries WHERE query_id = '{query_id}'"
        )
        state = str(result.preview.column("state").to_pylist()[0])
        if state in ("FAILED", "FINISHED"):
            return state
        await asyncio.sleep(0.2)
    return state


class TestTrinoQueryEngine(QueryEngineContract):
    @pytest.fixture
    def slow_sql(self) -> str:
        return SLOW_SQL


class TestTrinoCatalog(CatalogContract):
    @pytest.fixture
    async def seeded_table(self, engine: TrinoQueryEngine) -> TableRef:
        await engine.execute("CREATE SCHEMA IF NOT EXISTS memory.vendas")
        await engine.execute("DROP TABLE IF EXISTS memory.vendas.clientes")
        await engine.execute("CREATE TABLE memory.vendas.clientes (id INTEGER, email VARCHAR(120))")
        await engine.execute(
            "INSERT INTO memory.vendas.clientes VALUES (1, 'a@x.com'), (2, NULL), (3, 'c@x.com')"
        )
        return TableRef(catalog="memory", schema="vendas", name="clientes")

    @pytest.fixture
    def seeded_schema(self) -> str:
        return "vendas"

    @pytest.fixture
    def catalog(self, engine: TrinoQueryEngine) -> TrinoCatalog:
        return TrinoCatalog(engine)


# ------------------------------------------------------------------ motor
async def test_types_are_mapped_to_arrow(engine: TrinoQueryEngine) -> None:
    result = await engine.execute(
        "SELECT 1 AS i, CAST(2 AS bigint) AS b, DECIMAL '1.25' AS preco, 0.5e0 AS f, "
        "true AS flag, 'Ana' AS nome, DATE '2026-01-31' AS dia, "
        "TIMESTAMP '2026-01-31 10:00:00.123456' AS ts, "
        "TIMESTAMP '2026-01-31 10:00:00 America/Sao_Paulo' AS quando, "
        "JSON '{\"a\":1}' AS doc, ARRAY[1, 2] AS ids, MAP(ARRAY['a'], ARRAY[1]) AS m, "
        "X'0102' AS bin, UUID '6f1c1a52-6b0f-4f0e-9d5e-1d3b6c2a7f10' AS uid, "
        "CAST(NULL AS integer) AS vazio"
    )
    schema = result.preview.schema
    assert schema.field("i").type == pa.int32()
    assert schema.field("b").type == pa.int64()
    assert schema.field("preco").type == pa.decimal128(3, 2)
    assert schema.field("f").type == pa.float64()
    assert schema.field("flag").type == pa.bool_()
    assert schema.field("dia").type == pa.date32()
    assert schema.field("ts").type == pa.timestamp("us")
    assert schema.field("quando").type == pa.timestamp("us", tz="UTC")
    assert schema.field("ids").type == pa.list_(pa.int32())
    assert schema.field("bin").type == pa.binary()
    for text_col in ("nome", "doc", "m", "uid"):
        assert schema.field(text_col).type == pa.string()
    row = result.preview.to_pylist()[0]
    assert row["preco"] == Decimal("1.25")
    assert row["ts"] == datetime(2026, 1, 31, 10, 0, 0, 123456)
    assert row["quando"] == datetime(2026, 1, 31, 13, 0, tzinfo=UTC)
    assert row["bin"] == b"\x01\x02"
    assert row["vazio"] is None
    by_name = {c.name: c.data_type for c in result.columns}
    assert by_name["ts"] == "timestamp(6)"
    assert result.query_id
    assert result.cost is not None


async def test_empty_select_keeps_schema(engine: TrinoQueryEngine) -> None:
    result = await engine.execute("SELECT 1 AS id, 'x' AS nome WHERE false")
    assert result.preview.num_rows == 0
    assert result.preview.column_names == ["id", "nome"]
    assert result.total_rows == 0


async def test_ddl_returns_empty_table(engine: TrinoQueryEngine) -> None:
    await engine.execute("DROP TABLE IF EXISTS memory.default.dc_ddl")
    result = await engine.execute("CREATE TABLE memory.default.dc_ddl (id integer)")
    assert result.columns == []
    assert result.total_rows == 0


async def test_truncation_sets_total_rows_none(engine: TrinoQueryEngine) -> None:
    full = await engine.execute("SELECT nationkey FROM tpch.tiny.nation")
    assert full.total_rows == 25
    cut = await engine.execute("SELECT orderkey FROM tpch.sf1.orders", max_rows=3)
    assert cut.preview.num_rows == 3
    assert cut.total_rows is None


async def test_error_mapping(engine: TrinoQueryEngine) -> None:
    with pytest.raises(NotFoundError, match="TABLE_NOT_FOUND"):
        await engine.execute("SELECT * FROM tabela_que_nao_existe_xyz")
    with pytest.raises(NotFoundError, match="COLUMN_NOT_FOUND"):
        await engine.execute("SELECT coluna_inexistente FROM (SELECT 1 AS a) t")
    with pytest.raises(NotFoundError, match="CATALOG_NOT_FOUND"):
        await engine.execute("SELECT * FROM nao_existe.s.t")
    with pytest.raises(AdapterError) as syntax:
        await engine.execute("SELEC isso nao e sql")
    assert type(syntax.value) is AdapterError
    assert "SYNTAX_ERROR" in str(syntax.value)


async def test_server_timeout_is_transient(engine: TrinoQueryEngine) -> None:
    with pytest.raises(TransientError, match="EXCEEDED_TIME_LIMIT"):
        await engine.execute(SLOW_SQL, timeout_s=1)


async def test_cancel_running_query(engine: TrinoQueryEngine) -> None:
    task = asyncio.create_task(engine.execute(SLOW_SQL, timeout_s=120))
    query_id = await _wait_running(engine)
    await asyncio.sleep(0.5)
    await engine.cancel(query_id)
    with pytest.raises(TransientError, match="cancelada"):
        await asyncio.wait_for(task, timeout=30)
    assert await _server_state(engine, query_id) == "FAILED"
    await engine.cancel("20260101_000000_00000_xxxxx")  # id desconhecido: não faz nada


async def test_cancel_from_another_instance(
    engine: TrinoQueryEngine, trino_config: dict[str, Any]
) -> None:
    task = asyncio.create_task(engine.execute(SLOW_SQL, timeout_s=120))
    query_id = await _wait_running(engine)
    await asyncio.sleep(0.5)
    other = TrinoQueryEngine.from_config(trino_config, CTX)
    await other.cancel(query_id)  # não conhece o nextUri: usa a API de consultas
    with pytest.raises(TransientError, match="cancelada"):
        await asyncio.wait_for(task, timeout=30)


async def test_task_cancellation_kills_server_query(engine: TrinoQueryEngine) -> None:
    task = asyncio.create_task(engine.execute(SLOW_SQL, timeout_s=120))
    query_id = await _wait_running(engine)
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _server_state(engine, query_id) == "FAILED"


async def test_explain(engine: TrinoQueryEngine) -> None:
    plan = await engine.explain("SELECT name FROM tpch.tiny.nation WHERE regionkey = 1")
    assert "Fragment" in plan or "Output" in plan


async def test_unreachable_server_is_transient() -> None:
    eng = TrinoQueryEngine.from_config({"host": "127.0.0.1", "port": 1, "timeout_s": 5}, CTX)
    with pytest.raises(TransientError):
        await eng.execute("SELECT 1")


# ------------------------------------------------------------------ catálogo
@pytest.fixture
async def stats_table(engine: TrinoQueryEngine) -> TableRef:
    await engine.execute("CREATE SCHEMA IF NOT EXISTS memory.loja")
    await engine.execute("DROP TABLE IF EXISTS memory.loja.pedidos")
    await engine.execute(
        "CREATE TABLE memory.loja.pedidos (id bigint COMMENT 'chave', valor decimal(10,2), "
        "ativo boolean, criado timestamp(3) with time zone, doc json, tags array(varchar))"
    )
    await engine.execute(
        "INSERT INTO memory.loja.pedidos VALUES "
        "(1, 10.50, true, TIMESTAMP '2026-01-01 00:00:00 UTC', JSON '{\"a\":1}', ARRAY['x']), "
        "(2, 99.90, false, TIMESTAMP '2026-02-01 00:00:00 UTC', JSON '{\"a\":1}', ARRAY['y']), "
        "(2, NULL, NULL, NULL, NULL, NULL)"
    )
    await engine.execute("COMMENT ON TABLE memory.loja.pedidos IS 'pedidos do ano'")
    return TableRef(schema="loja", name="pedidos")


async def test_describe_uses_default_catalog_and_comments(
    engine: TrinoQueryEngine, stats_table: TableRef
) -> None:
    info = await TrinoCatalog(engine).describe(stats_table)
    assert info.ref == TableRef(catalog="memory", schema="loja", name="pedidos")
    assert info.row_count == 3
    assert info.description == "pedidos do ano"
    types = {c.name: c.data_type for c in info.columns}
    assert types["valor"] == "decimal(10,2)"
    assert types["tags"] == "array(varchar)"
    assert types["criado"] == "timestamp(3) with time zone"
    assert info.columns[0].description == "chave"


async def test_describe_is_case_insensitive(
    engine: TrinoQueryEngine, stats_table: TableRef
) -> None:
    info = await TrinoCatalog(engine).describe(TableRef(schema="Loja", name="PEDIDOS"))
    assert info.ref.name == "pedidos"


async def test_column_stats_values(engine: TrinoQueryEngine, stats_table: TableRef) -> None:
    stats = {s.column: s for s in await TrinoCatalog(engine).column_stats(stats_table)}
    assert stats["id"].distinct_count == 2
    assert (stats["id"].min, stats["id"].max) == ("1", "2")
    assert stats["valor"].null_fraction == pytest.approx(1 / 3)
    assert (stats["valor"].min, stats["valor"].max) == ("10.50", "99.90")
    assert (stats["ativo"].min, stats["ativo"].max) == ("false", "true")
    assert stats["doc"].distinct_count == 1
    assert stats["doc"].min is None
    assert stats["tags"].distinct_count is None
    assert stats["tags"].null_fraction == pytest.approx(1 / 3)


async def test_column_stats_filter_and_missing(
    engine: TrinoQueryEngine, stats_table: TableRef
) -> None:
    catalog = TrinoCatalog(engine)
    only = await catalog.column_stats(stats_table, ["valor"])
    assert [s.column for s in only] == ["valor"]
    with pytest.raises(NotFoundError, match="nao_existe"):
        await catalog.column_stats(stats_table, ["nao_existe"])


async def test_other_catalog_by_ref(engine: TrinoQueryEngine) -> None:
    catalog = TrinoCatalog(engine)
    assert "tiny" in await catalog.list_schemas("tpch")
    tables = {t.name for t in await catalog.list_tables("tiny", catalog="tpch")}
    assert {"nation", "region"} <= tables
    info = await catalog.describe(TableRef(catalog="tpch", schema="tiny", name="nation"))
    assert info.row_count == 25
    assert info.columns[0].nullable is False


async def test_list_schemas_hides_information_schema(engine: TrinoQueryEngine) -> None:
    schemas = await TrinoCatalog(engine).list_schemas()
    assert "default" in schemas
    assert "information_schema" not in schemas
    with pytest.raises(NotFoundError):
        await TrinoCatalog(engine).list_schemas("catalogo_que_nao_existe")


async def test_catalog_from_config_works_alone(trino_config: dict[str, Any]) -> None:
    catalog = TrinoCatalog.from_config(trino_config, CTX)
    assert "default" in await catalog.list_schemas()
