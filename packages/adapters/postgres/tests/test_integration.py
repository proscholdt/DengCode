"""Contratos e comportamento do adapter contra um PostgreSQL real (testcontainers)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from dengcode_adapter_postgres.catalog import PostgresCatalog
from dengcode_adapter_postgres.engine import PostgresQueryEngine
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    NotFoundError,
    TransientError,
)
from dengcode_core.ports.models import AdapterContext, TableRef
from dengcode_testkit.contracts.data import CatalogContract, QueryEngineContract

pytestmark = pytest.mark.integration

CTX = AdapterContext(workspace="teste")


@pytest.fixture
def engine(pg_config: dict[str, Any]) -> PostgresQueryEngine:
    return PostgresQueryEngine.from_config(pg_config, CTX)


class TestPostgresQueryEngine(QueryEngineContract):
    @pytest.fixture
    def slow_sql(self) -> str:
        return "SELECT pg_sleep(30)"


class TestPostgresCatalog(CatalogContract):
    @pytest.fixture
    async def seeded_table(self, engine: PostgresQueryEngine) -> TableRef:
        await engine.execute("CREATE SCHEMA IF NOT EXISTS vendas")
        await engine.execute("DROP TABLE IF EXISTS vendas.clientes")
        await engine.execute("CREATE TABLE vendas.clientes (id INTEGER, email VARCHAR(120))")
        await engine.execute(
            "INSERT INTO vendas.clientes VALUES (1, 'a@x.com'), (2, NULL), (3, 'c@x.com')"
        )
        return TableRef(schema="vendas", name="clientes")

    @pytest.fixture
    def seeded_schema(self) -> str:
        return "vendas"

    @pytest.fixture
    def catalog(self, engine: PostgresQueryEngine) -> PostgresCatalog:
        return PostgresCatalog(engine)


# ------------------------------------------------------------------ motor
async def test_types_are_mapped_to_arrow(engine: PostgresQueryEngine) -> None:
    result = await engine.execute(
        "SELECT 1::int4 AS i4, 2::int8 AS i8, 1.25::numeric(10,2) AS preco, "
        "sum(x) AS soma, 0.5::float8 AS f8, true AS flag, 'Ana'::text AS nome, "
        "DATE '2026-01-31' AS dia, TIMESTAMPTZ '2026-01-31 10:00:00+00' AS quando, "
        "INTERVAL '1 day' AS duracao, '{\"a\": 1}'::jsonb AS doc, "
        "'6f1c1a52-6b0f-4f0e-9d5e-1d3b6c2a7f10'::uuid AS uid, ARRAY[1, 2]::int4[] AS ids, "
        "'NaN'::numeric AS nan, NULL::int4 AS vazio "
        "FROM (VALUES (10::bigint), (20::bigint)) AS t(x)"
    )
    schema = result.preview.schema
    assert schema.field("i4").type == pa.int32()
    assert schema.field("i8").type == pa.int64()
    assert schema.field("preco").type == pa.decimal128(10, 2)
    assert schema.field("soma").type == pa.decimal128(38, 0)
    assert schema.field("f8").type == pa.float64()
    assert schema.field("flag").type == pa.bool_()
    assert schema.field("dia").type == pa.date32()
    assert schema.field("quando").type == pa.timestamp("us", tz="UTC")
    assert schema.field("duracao").type == pa.duration("us")
    assert schema.field("ids").type == pa.list_(pa.int32())
    for text_col in ("nome", "doc", "uid", "nan"):
        assert schema.field(text_col).type == pa.string()
    row = result.preview.to_pylist()[0]
    assert row["soma"] == Decimal(30)
    assert row["doc"] == '{"a": 1}'
    assert row["uid"] == "6f1c1a52-6b0f-4f0e-9d5e-1d3b6c2a7f10"
    assert row["nan"] == "NaN"
    assert row["vazio"] is None
    by_name = {c.name: c.data_type for c in result.columns}
    assert by_name["preco"] == "numeric(10,2)"


async def test_empty_select_keeps_schema(engine: PostgresQueryEngine) -> None:
    result = await engine.execute("SELECT 1::int4 AS id, 'x'::text AS nome WHERE false")
    assert result.preview.num_rows == 0
    assert result.preview.column_names == ["id", "nome"]
    assert result.total_rows == 0


async def test_ddl_returns_empty_table(engine: PostgresQueryEngine) -> None:
    result = await engine.execute("CREATE TABLE IF NOT EXISTS dc_ddl (id int)")
    assert result.preview.num_rows == 0
    assert result.columns == []
    assert result.total_rows == 0


async def test_truncation_sets_total_rows_none(engine: PostgresQueryEngine) -> None:
    full = await engine.execute("SELECT g FROM generate_series(1, 5) g")
    assert full.total_rows == 5
    cut = await engine.execute("SELECT g FROM generate_series(1, 5) g", max_rows=3)
    assert cut.preview.num_rows == 3
    assert cut.total_rows is None


async def test_multiple_statements_return_last_result(engine: PostgresQueryEngine) -> None:
    result = await engine.execute(
        "DROP TABLE IF EXISTS dc_multi; CREATE TABLE dc_multi (n int); "
        "INSERT INTO dc_multi VALUES (7); SELECT n FROM dc_multi"
    )
    assert result.preview.column("n").to_pylist() == [7]


async def test_error_mapping(engine: PostgresQueryEngine) -> None:
    with pytest.raises(NotFoundError):
        await engine.execute("SELECT * FROM tabela_que_nao_existe_xyz")
    with pytest.raises(NotFoundError):
        await engine.execute("SELECT coluna_inexistente FROM (SELECT 1 AS a) t")
    with pytest.raises(AdapterError) as syntax:
        await engine.execute("SELEC isso nao e sql")
    assert type(syntax.value) is AdapterError
    assert "42601" in str(syntax.value)


async def test_statement_timeout_is_transient(engine: PostgresQueryEngine) -> None:
    with pytest.raises(TransientError, match="cancelada"):
        await engine.execute("SELECT pg_sleep(30)", timeout_s=1)


async def test_cancel_running_query(engine: PostgresQueryEngine) -> None:
    task = asyncio.create_task(engine.execute("SELECT pg_sleep(30)", timeout_s=60))
    for _ in range(100):
        if engine._running:
            break
        await asyncio.sleep(0.05)
    assert engine._running, "a consulta não começou"
    await asyncio.sleep(0.2)  # garante que o SELECT já está no servidor
    await engine.cancel(next(iter(engine._running)))
    with pytest.raises(TransientError):
        await asyncio.wait_for(task, timeout=10)
    await engine.cancel("id-desconhecido")  # não faz nada


async def test_wrong_password_is_auth_error(pg_config: dict[str, Any]) -> None:
    bad = PostgresQueryEngine.from_config({**pg_config, "password": "errada"}, CTX)
    with pytest.raises(AuthError) as info:
        await bad.execute("SELECT 1")
    assert "errada" not in str(info.value)


async def test_dsn_is_accepted(pg_config: dict[str, Any]) -> None:
    dsn = (
        f"postgresql://{pg_config['user']}:{pg_config['password']}@"
        f"{pg_config['host']}:{pg_config['port']}/{pg_config['dbname']}"
    )
    eng = PostgresQueryEngine.from_config({"dsn": dsn}, CTX)
    result = await eng.execute("SELECT current_setting('application_name') AS app")
    assert result.preview.column("app").to_pylist() == ["dengcode"]


async def test_unreachable_server_is_transient() -> None:
    eng = PostgresQueryEngine.from_config(
        {"host": "127.0.0.1", "port": 1, "dbname": "x", "user": "x", "connect_timeout_s": 2},
        CTX,
    )
    with pytest.raises(TransientError):
        await eng.execute("SELECT 1")


# ------------------------------------------------------------------ catálogo
@pytest.fixture
async def stats_table(engine: PostgresQueryEngine) -> TableRef:
    await engine.execute('DROP TABLE IF EXISTS public."Pedidos Ano"')
    await engine.execute(
        'CREATE TABLE public."Pedidos Ano" ('
        '"ID" bigint, valor numeric(10,2), ativo boolean, criado timestamptz, '
        "doc jsonb, tags text[], codigo uuid)"
    )
    await engine.execute(
        'INSERT INTO public."Pedidos Ano" VALUES '
        "(1, 10.50, true, '2026-01-01T00:00:00Z', '{\"a\":1}', '{x}', "
        "'00000000-0000-0000-0000-000000000001'), "
        "(2, 99.90, false, '2026-02-01T00:00:00Z', '{\"a\":1}', '{y}', NULL), "
        "(2, NULL, NULL, NULL, NULL, NULL, NULL)"
    )
    await engine.execute("COMMENT ON TABLE public.\"Pedidos Ano\" IS 'pedidos do ano'")
    return TableRef(name="Pedidos Ano")


async def test_describe_resolves_schema_and_quotes(
    engine: PostgresQueryEngine, stats_table: TableRef
) -> None:
    info = await PostgresCatalog(engine).describe(stats_table)
    assert info.ref.schema_ == "public"
    assert info.ref.catalog == "test"
    assert info.row_count == 3
    assert info.description == "pedidos do ano"
    types = {c.name: c.data_type for c in info.columns}
    assert types["valor"] == "numeric(10,2)"
    assert types["tags"] == "text[]"
    assert types["criado"] == "timestamp with time zone"


async def test_column_stats_values(engine: PostgresQueryEngine, stats_table: TableRef) -> None:
    stats = {s.column: s for s in await PostgresCatalog(engine).column_stats(stats_table)}
    assert stats["ID"].distinct_count == 2
    assert (stats["ID"].min, stats["ID"].max) == ("1", "2")
    assert stats["valor"].null_fraction == pytest.approx(1 / 3)
    assert (stats["valor"].min, stats["valor"].max) == ("10.50", "99.90")
    assert (stats["ativo"].min, stats["ativo"].max) == ("false", "true")
    assert stats["doc"].distinct_count == 1
    assert stats["doc"].min is None
    assert stats["tags"].distinct_count == 2
    assert stats["codigo"].min == "00000000-0000-0000-0000-000000000001"


async def test_column_stats_filter_and_missing(
    engine: PostgresQueryEngine, stats_table: TableRef
) -> None:
    catalog = PostgresCatalog(engine)
    only = await catalog.column_stats(stats_table, ["valor"])
    assert [s.column for s in only] == ["valor"]
    with pytest.raises(NotFoundError, match="nao_existe"):
        await catalog.column_stats(stats_table, ["nao_existe"])


async def test_list_schemas_hides_system(engine: PostgresQueryEngine) -> None:
    schemas = await PostgresCatalog(engine).list_schemas()
    assert "public" in schemas
    assert not {"pg_catalog", "information_schema", "pg_toast"} & set(schemas)
    assert await PostgresCatalog(engine).list_schemas("outro_banco") == []


async def test_catalog_from_config_works_alone(pg_config: dict[str, Any]) -> None:
    catalog = PostgresCatalog.from_config(pg_config, CTX)
    assert "public" in await catalog.list_schemas()
