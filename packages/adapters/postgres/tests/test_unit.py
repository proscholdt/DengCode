"""Testes rápidos (sem Docker): configuração, tipos, erros e montagem de SQL."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import entry_points
from uuid import UUID

import psycopg
import pyarrow as pa
import pytest
from psycopg import errors as pg_errors
from psycopg import sql as pgsql
from psycopg.conninfo import conninfo_to_dict

from dengcode_adapter_postgres.arrow import ColumnSpec, rows_to_table, to_arrow_array
from dengcode_adapter_postgres.catalog import (
    PostgresCatalog,
    base_type,
    build_stats,
    stats_expressions,
)
from dengcode_adapter_postgres.engine import (
    PostgresQueryEngine,
    build_conninfo,
    parse_connect_timeout,
)
from dengcode_adapter_postgres.errors import map_error
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    PermissionDeniedError,
    TransientError,
    UnsupportedOperation,
)
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.models import AdapterContext, ColumnInfo, TableRef
from dengcode_core.ports.query_engine import QueryEnginePort

CTX = AdapterContext(workspace="teste")


# ------------------------------------------------------------------ configuração
def test_conninfo_from_keys() -> None:
    conninfo = build_conninfo(
        {"host": "db.local", "port": 5433, "dbname": "vendas", "user": "ana", "password": "s3"}
    )
    parsed = conninfo_to_dict(conninfo)
    assert parsed["host"] == "db.local"
    assert parsed["port"] == "5433"
    assert parsed["dbname"] == "vendas"
    assert parsed["application_name"] == "dengcode"


def test_keys_override_dsn() -> None:
    conninfo = build_conninfo(
        {"dsn": "postgresql://u:p@h1:5432/a?application_name=meu", "dbname": "b"}
    )
    parsed = conninfo_to_dict(conninfo)
    assert parsed["dbname"] == "b"
    assert parsed["host"] == "h1"
    assert parsed["application_name"] == "meu"


@pytest.mark.parametrize(
    "cfg",
    [{"port": "abc"}, {"port": 70000}, {"dsn": 123}, {"dsn": "host=a port"}],
)
def test_invalid_config(cfg: dict[str, object]) -> None:
    with pytest.raises(ConfigError) as info:
        build_conninfo(cfg)
    assert "host=a" not in str(info.value)


def test_connect_timeout_parsing() -> None:
    assert parse_connect_timeout({}) == 10
    assert parse_connect_timeout({"connect_timeout_s": "3"}) == 3
    for bad in (0, -1, "x"):
        with pytest.raises(ConfigError):
            parse_connect_timeout({"connect_timeout_s": bad})


def test_repr_never_shows_password() -> None:
    eng = PostgresQueryEngine.from_config({"host": "h", "password": "segredo-123"}, CTX)
    assert "segredo-123" not in repr(eng)


def test_entry_points_are_registered() -> None:
    names = {ep.name: ep.value for ep in entry_points(group="dengcode.adapters")}
    assert names["postgres_query_engine"] == "dengcode_adapter_postgres.engine:PostgresQueryEngine"
    assert names["postgres_catalog"] == "dengcode_adapter_postgres.catalog:PostgresCatalog"


def test_ports_and_capabilities() -> None:
    eng = PostgresQueryEngine("host=h")
    catalog = PostgresCatalog(eng)
    assert isinstance(eng, QueryEnginePort)
    assert isinstance(catalog, CatalogPort)
    caps = eng.capabilities()
    assert caps.sql_dialect == "postgres"
    assert caps.supports_explain
    assert not caps.supports_dry_run
    assert caps.supports_transactions
    assert caps.supports_cancel
    assert not catalog.capabilities().supports_lineage
    assert eng.info.name == catalog.info.name == "postgres"


async def test_unsupported_operations() -> None:
    eng = PostgresQueryEngine("host=h")
    with pytest.raises(UnsupportedOperation):
        await eng.dry_run("SELECT 1")
    with pytest.raises(UnsupportedOperation):
        await PostgresCatalog(eng).lineage(TableRef(name="t"), direction="up")


def test_catalog_reuses_peer_engine() -> None:
    eng = PostgresQueryEngine("host=h")
    ctx = AdapterContext(workspace="t", peers={"query_engine": eng})
    assert PostgresCatalog.from_config({}, ctx).engine is eng
    own = PostgresCatalog.from_config({"host": "outro"}, ctx)
    assert own.engine is not eng


# ------------------------------------------------------------------ erros
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (pg_errors.UndefinedTable("relation x does not exist"), NotFoundError),
        (pg_errors.UndefinedColumn("column y does not exist"), NotFoundError),
        (pg_errors.InvalidSchemaName("schema z does not exist"), NotFoundError),
        (pg_errors.SyntaxError("syntax error at or near SELEC"), AdapterError),
        (pg_errors.InvalidPassword("password authentication failed"), AuthError),
        (pg_errors.InsufficientPrivilege("permission denied for table t"), PermissionDeniedError),
        (pg_errors.SerializationFailure("could not serialize access"), TransientError),
        (pg_errors.DeadlockDetected("deadlock detected"), TransientError),
        (pg_errors.QueryCanceled("canceling statement due to statement timeout"), TransientError),
        (pg_errors.AdminShutdown("terminating connection"), TransientError),
        (pg_errors.TooManyConnections("sorry, too many clients"), TransientError),
        (
            psycopg.OperationalError(
                'connection failed: FATAL:  password authentication failed for user "ana"'
            ),
            AuthError,
        ),
        (psycopg.OperationalError("connection failed: Connection refused"), TransientError),
        (
            psycopg.OperationalError('connection failed: FATAL:  database "x" does not exist'),
            NotFoundError,
        ),
        (pg_errors.DivisionByZero("division by zero"), AdapterError),
        (psycopg.InterfaceError("the connection is closed"), AdapterError),
    ],
)
def test_map_error(exc: psycopg.Error, expected: type[Exception]) -> None:
    mapped = map_error(exc)
    assert type(mapped) is expected


def test_map_error_keeps_sqlstate_in_message() -> None:
    assert "42P01" in str(map_error(pg_errors.UndefinedTable("relation x does not exist")))


def test_proactor_error_has_hint() -> None:
    mapped = map_error(
        psycopg.InterfaceError("Psycopg cannot use the 'ProactorEventLoop' to run in async mode")
    )
    assert "SelectorEventLoop" in str(mapped)


def test_proactor_loop_is_domain_error() -> None:
    if sys.platform != "win32":
        pytest.skip("só o Windows tem ProactorEventLoop")
    eng = PostgresQueryEngine("host=127.0.0.1 port=1 connect_timeout=1")
    with pytest.raises(AdapterError, match="SelectorEventLoop"):
        asyncio.run(eng.execute("SELECT 1"), loop_factory=asyncio.ProactorEventLoop)


# ------------------------------------------------------------------ tipos
def _spec(oid: int, precision: int | None = None, scale: int | None = None) -> ColumnSpec:
    return ColumnSpec(
        name="c", type_code=oid, type_display=str(oid), precision=precision, scale=scale
    )


@pytest.mark.parametrize(
    ("oid", "values", "expected"),
    [
        (23, [1, None], pa.int32()),
        (20, [2**40], pa.int64()),
        (701, [1.5], pa.float64()),
        (16, [True, None], pa.bool_()),
        (25, ["Ana"], pa.string()),
        (1043, ["Bruno"], pa.string()),
        (1184, [datetime(2026, 1, 1, tzinfo=UTC)], pa.timestamp("us", tz="UTC")),
        (1007, [[1, 2], None], pa.list_(pa.int32())),
        (3802, [{"a": 1}], pa.string()),
        (2950, [UUID(int=1)], pa.string()),
        (99999, [object()], pa.string()),
    ],
)
def test_arrow_types(oid: int, values: list[object], expected: pa.DataType) -> None:
    assert to_arrow_array(_spec(oid), values).type == expected


def test_numeric_with_declared_precision() -> None:
    arr = to_arrow_array(_spec(1700, 10, 2), [Decimal("1.25"), None])
    assert arr.type == pa.decimal128(10, 2)


def test_numeric_without_precision_uses_value_scale() -> None:
    arr = to_arrow_array(_spec(1700), [Decimal(30), Decimal("1.125")])
    assert arr.type == pa.decimal128(38, 3)
    assert arr.to_pylist() == [Decimal("30.000"), Decimal("1.125")]


def test_numeric_nan_falls_back_to_text() -> None:
    arr = to_arrow_array(_spec(1700), [Decimal("NaN"), Decimal(1)])
    assert arr.to_pylist() == ["NaN", "1"]


def test_json_is_serialized() -> None:
    arr = to_arrow_array(_spec(114), [{"nome": "Ação"}, [1, 2], None])
    assert arr.to_pylist() == ['{"nome": "Ação"}', "[1, 2]", None]


def test_conversion_failure_falls_back_to_text() -> None:
    arr = to_arrow_array(_spec(23), ["não é número"])
    assert arr.type == pa.string()


def test_rows_to_table_keeps_schema_without_rows() -> None:
    table = rows_to_table([_spec(23), ColumnSpec("nome", 25, "text")], [])
    assert table.num_rows == 0
    assert table.schema.types == [pa.int32(), pa.string()]


# ------------------------------------------------------------------ estatísticas
def test_base_type_strips_modifiers() -> None:
    assert base_type("timestamp(3) with time zone") == "timestamp with time zone"
    assert base_type("numeric(10, 2)") == "numeric"
    assert base_type("character varying(50)[]") == "character varying[]"


def _render(parts: list[pgsql.Composable]) -> list[str]:
    # renderização sem conexão: só para inspecionar a forma do SQL
    return [repr(p) for p in parts]


def test_stats_expressions_per_type() -> None:
    ordered = _render(stats_expressions("valor", "numeric(10,2)"))
    assert "min" in ordered[2]
    assert "max" in ordered[3]
    jsonish = _render(stats_expressions("doc", "json"))
    assert "::text" in jsonish[1]
    assert "NULL::text" in jsonish[2]
    boolean = _render(stats_expressions("ativo", "boolean"))
    assert "bool_and" in boolean[2]
    assert "bool_or" in boolean[3]
    array = _render(stats_expressions("ids", "integer[]"))
    assert "NULL::text" in array[2]


def test_build_stats() -> None:
    cols = [ColumnInfo(name="id", data_type="integer"), ColumnInfo(name="email", data_type="text")]
    stats = build_stats(cols, (3, 3, 3, 1, 3, 2, 2, "a@x.com", "c@x.com"))
    assert stats[0].null_fraction == 0
    assert (stats[0].min, stats[0].max) == ("1", "3")
    assert stats[1].null_fraction == pytest.approx(1 / 3)
    assert stats[1].distinct_count == 2
    empty = build_stats(cols[:1], (0, 0, 0, None, None))
    assert empty[0].null_fraction is None
    assert empty[0].min is None
