"""Testes rápidos (sem Docker): config, tipos, erros, protocolo e montagem de SQL.

O protocolo HTTP é exercitado com um Trino de mentira (``httpx2.MockTransport``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from importlib.metadata import entry_points
from importlib.resources import files
from typing import Any

import httpx2
import pyarrow as pa
import pytest

from dengcode_adapter_trino.arrow import (
    TrinoColumn,
    column_mapping,
    parse_timestamp_tz,
    parse_type,
    rows_to_table,
    to_arrow_array,
)
from dengcode_adapter_trino.catalog import (
    TrinoCatalog,
    build_stats,
    quote_ident,
    quote_literal,
    stats_expressions,
)
from dengcode_adapter_trino.engine import TrinoQueryEngine, build_settings
from dengcode_adapter_trino.errors import map_query_error, map_status, map_transport_error
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    TransientError,
    UnsupportedOperation,
)
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.models import AdapterContext, ColumnInfo, PortKind, TableRef
from dengcode_core.ports.query_engine import QueryEnginePort

CTX = AdapterContext(workspace="teste")
Page = dict[str, Any]


# ------------------------------------------------------------------ Trino de mentira
@dataclass
class FakeTrino:
    """``POST /v1/statement`` devolve a 1ª página do script; cada ``GET nextUri``, a seguinte."""

    scripts: list[tuple[str, list[Page]]] = field(default_factory=lambda: [])
    requests: list[httpx2.Request] = field(default_factory=lambda: [])
    statements: list[str] = field(default_factory=lambda: [])
    fail_first: list[int] = field(default_factory=lambda: [])  # status antes de responder
    hang_on_get: bool = False
    _pending: dict[str, list[Page]] = field(default_factory=lambda: {})
    _count: int = 0

    def add(self, sql_part: str, *pages: Page) -> None:
        self.scripts.append((sql_part, list(pages)))

    def deletes(self) -> list[str]:
        return [r.url.path for r in self.requests if r.method == "DELETE"]

    def factory(self) -> Callable[[], httpx2.AsyncBaseTransport]:
        return lambda: httpx2.MockTransport(self.handle)

    def _chain(self, pages: list[Page]) -> Page:
        self._count += 1
        query_id = f"20260101_000000_{self._count:05d}_abcde"
        chained = [{"id": query_id, **p} for p in pages]
        for i, page in enumerate(chained[:-1]):
            token = f"{query_id}/{i + 1}"
            page["nextUri"] = f"http://trino.test/v1/statement/executing/{token}"
            self._pending[token] = chained[i + 1 :]
        return chained[0]

    async def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.fail_first:
            return httpx2.Response(self.fail_first.pop(0), text="ocupado")
        path = request.url.path
        if request.method == "POST" and path == "/v1/statement":
            sql = request.content.decode("utf-8")
            self.statements.append(sql)
            for part, pages in self.scripts:
                if part in sql:
                    return httpx2.Response(200, json=self._chain(pages))
            return httpx2.Response(200, json={"id": "sem_script", "columns": [], "data": []})
        if request.method == "GET" and path.startswith("/v1/statement/executing/"):
            if self.hang_on_get:
                await asyncio.sleep(30)
            rest = self._pending.pop(path.removeprefix("/v1/statement/executing/"), None)
            if rest is None:
                return httpx2.Response(410, text="gone")
            return httpx2.Response(200, json=rest[0])
        if request.method == "DELETE":
            return httpx2.Response(204)
        return httpx2.Response(404, text=json.dumps({"path": path}))


def cols(*specs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"name": n, "type": t} for n, t in specs]


@pytest.fixture
def fake() -> FakeTrino:
    return FakeTrino()


def make_engine(fake: FakeTrino, **cfg: Any) -> TrinoQueryEngine:
    base = {"host": "trino.test", "catalog": "memory", "schema": "default", **cfg}
    return TrinoQueryEngine.from_config(base, CTX, transport_factory=fake.factory())


# ------------------------------------------------------------------ configuração
def test_settings_defaults() -> None:
    s = build_settings({})
    assert s.base_url == "http://localhost:8080"
    assert s.user == "dengcode"
    assert s.catalog is None
    assert s.schema is None
    assert s.password is None
    assert s.request_timeout_s == 60


def test_settings_custom_and_password_hidden() -> None:
    s = build_settings(
        {
            "host": "trino.local",
            "port": "8443",
            "http_scheme": "HTTPS",
            "user": "ana",
            "catalog": "iceberg",
            "schema": "vendas",
            "password": "segredo-123",
            "timeout_s": 15,
        }
    )
    assert s.base_url == "https://trino.local:8443"
    assert (s.user, s.catalog, s.schema, s.request_timeout_s) == ("ana", "iceberg", "vendas", 15)
    assert "segredo-123" not in repr(s)
    engine = TrinoQueryEngine.from_config({"password": "segredo-123"}, CTX)
    assert "segredo-123" not in repr(engine)
    assert "segredo-123" not in repr(engine.client)


@pytest.mark.parametrize(
    ("cfg", "match"),
    [
        ({"port": "abc"}, "port"),
        ({"port": 0}, "port"),
        ({"port": 70000}, "port"),
        ({"http_scheme": "ftp"}, "http_scheme"),
        ({"host": "http://x:8080"}, "host"),
        ({"user": 5}, "user"),
        ({"timeout_s": -1}, "timeout_s"),
        ({"password": 123}, "password"),
    ],
)
def test_settings_invalid(cfg: dict[str, Any], match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        build_settings(cfg)


def test_entry_points_and_info() -> None:
    eps = {ep.name: ep.value for ep in entry_points(group="dengcode.adapters")}
    assert eps["trino_query_engine"] == "dengcode_adapter_trino.engine:TrinoQueryEngine"
    assert eps["trino_catalog"] == "dengcode_adapter_trino.catalog:TrinoCatalog"
    assert TrinoQueryEngine.info.name == TrinoCatalog.info.name == "trino"
    assert TrinoQueryEngine.info.port is PortKind.QUERY_ENGINE
    assert TrinoCatalog.info.port is PortKind.CATALOG
    pack = TrinoQueryEngine.info.knowledge_pack
    assert pack == "dengcode_adapter_trino:knowledge/trino.md"
    module, _, rel = pack.partition(":")
    assert files(module).joinpath(rel).read_text(encoding="utf-8").startswith("# ")


def test_satisfies_ports() -> None:
    engine = TrinoQueryEngine.from_config({}, CTX)
    assert isinstance(engine, QueryEnginePort)
    assert isinstance(TrinoCatalog(engine), CatalogPort)
    caps = engine.capabilities()
    assert caps.sql_dialect == "trino"
    assert (caps.supports_explain, caps.supports_dry_run, caps.supports_cancel) == (
        True,
        False,
        True,
    )


def test_catalog_reuses_peer_engine() -> None:
    engine = TrinoQueryEngine.from_config({"catalog": "memory"}, CTX)
    ctx = AdapterContext(workspace="teste", peers={"query_engine": engine})
    assert TrinoCatalog.from_config({}, ctx).engine is engine
    own = TrinoCatalog.from_config({"host": "outro"}, ctx)
    assert own.engine is not engine
    assert own.engine.settings.base_url == "http://outro:8080"


# ------------------------------------------------------------------ tipos
def test_parse_type() -> None:
    assert parse_type("varchar(50)").base == "varchar"
    p = parse_type("timestamp(3) with time zone")
    assert (p.base, p.args, p.with_time_zone) == ("timestamp", "3", True)
    assert parse_type("timestamp with time zone").with_time_zone
    assert parse_type("decimal(10, 2)").args == "10, 2"
    assert parse_type("array(varchar(10))").args == "varchar(10)"
    assert parse_type("interval day to second").base == "interval day to second"


@pytest.mark.parametrize(
    ("trino_type", "arrow_type"),
    [
        ("boolean", pa.bool_()),
        ("tinyint", pa.int8()),
        ("smallint", pa.int16()),
        ("integer", pa.int32()),
        ("bigint", pa.int64()),
        ("real", pa.float32()),
        ("double", pa.float64()),
        ("decimal(10,2)", pa.decimal128(10, 2)),
        ("decimal(3, 2)", pa.decimal128(3, 2)),
        ("decimal(38,0)", pa.decimal128(38, 0)),
        ("varchar", pa.string()),
        ("char(3)", pa.string()),
        ("varbinary", pa.binary()),
        ("date", pa.date32()),
        ("time(3)", pa.time64("us")),
        ("timestamp(6)", pa.timestamp("us")),
        ("timestamp(3) with time zone", pa.timestamp("us", tz="UTC")),
        ("array(integer)", pa.list_(pa.int32())),
        ("array(row(a integer))", pa.string()),
        ("map(varchar, integer)", pa.string()),
        ("row(a integer, b varchar)", pa.string()),
        ("json", pa.string()),
        ("uuid", pa.string()),
        ("interval day to second", pa.string()),
        ("time(3) with time zone", pa.string()),
        ("unknown", pa.string()),
    ],
)
def test_column_mapping(trino_type: str, arrow_type: pa.DataType) -> None:
    assert column_mapping(trino_type)[0] == arrow_type


def test_values_are_converted() -> None:
    columns = [
        TrinoColumn("d", "decimal(10,2)"),
        TrinoColumn("f", "double"),
        TrinoColumn("dt", "date"),
        TrinoColumn("ts", "timestamp(9)"),
        TrinoColumn("tz", "timestamp(3) with time zone"),
        TrinoColumn("tm", "time(12)"),
        TrinoColumn("bin", "varbinary"),
        TrinoColumn("arr", "array(bigint)"),
        TrinoColumn("m", "map(varchar, integer)"),
        TrinoColumn("rw", "row(integer, varchar)"),
        TrinoColumn("j", "json"),
    ]
    rows = [
        [
            "1.25",
            "NaN",
            "2026-01-31",
            "2026-01-31 10:00:00.123456789",
            "2026-01-31 10:00:00.000 America/Sao_Paulo",
            "10:00:00.123456789012",
            base64.b64encode(b"\x01\x02").decode(),
            [1, None, 3],
            {"a": 1},
            [1, "x"],
            '{"a":1}',
        ],
        [
            None,
            "Infinity",
            None,
            None,
            "2026-01-31 10:00:00.000 +05:30",
            None,
            None,
            [],
            {},
            None,
            None,
        ],
    ]
    table = rows_to_table(columns, rows)
    first = table.to_pylist()[0]
    assert first["d"] == Decimal("1.25")
    assert math.isnan(first["f"])
    assert first["dt"] == date(2026, 1, 31)
    assert first["ts"] == datetime(2026, 1, 31, 10, 0, 0, 123456)
    assert first["tz"] == datetime(2026, 1, 31, 13, 0, tzinfo=UTC)
    assert first["tm"] == time(10, 0, 0, 123456)
    assert first["bin"] == b"\x01\x02"
    assert first["arr"] == [1, None, 3]
    assert json.loads(first["m"]) == {"a": 1}
    assert json.loads(first["rw"]) == [1, "x"]
    assert first["j"] == '{"a":1}'
    second = table.to_pylist()[1]
    assert second["f"] == math.inf
    assert second["tz"] == datetime(2026, 1, 31, 4, 30, tzinfo=UTC)
    assert second["d"] is None


def test_timestamp_with_unknown_zone_falls_back_to_text() -> None:
    with pytest.raises(ValueError, match="fuso"):
        parse_timestamp_tz("2026-01-31 10:00:00 Marte/Olimpo")
    arr = to_arrow_array("timestamp(3) with time zone", ["2026-01-31 10:00:00 Marte/Olimpo"])
    assert arr.type == pa.string()
    assert arr.to_pylist() == ["2026-01-31 10:00:00 Marte/Olimpo"]


def test_bad_value_falls_back_to_text() -> None:
    arr = to_arrow_array("integer", [1, "não é número"])
    assert arr.type == pa.string()
    assert arr.to_pylist() == ["1", "não é número"]


def test_empty_result_keeps_schema_and_duplicate_names() -> None:
    table = rows_to_table([TrinoColumn("id", "integer"), TrinoColumn("id", "varchar")], [])
    assert table.num_rows == 0
    assert table.column_names == ["id", "id"]
    assert table.schema.types == [pa.int32(), pa.string()]
    assert rows_to_table([], []).num_columns == 0


# ------------------------------------------------------------------ erros
@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("TABLE_NOT_FOUND", "USER_ERROR", NotFoundError),
        ("SCHEMA_NOT_FOUND", "USER_ERROR", NotFoundError),
        ("CATALOG_NOT_FOUND", "USER_ERROR", NotFoundError),
        ("COLUMN_NOT_FOUND", "USER_ERROR", NotFoundError),
        ("PERMISSION_DENIED", "USER_ERROR", PermissionDeniedError),
        ("EXCEEDED_TIME_LIMIT", "INSUFFICIENT_RESOURCES", TransientError),
        ("USER_CANCELED", "USER_ERROR", TransientError),
        ("EXCEEDED_GLOBAL_MEMORY_LIMIT", "INSUFFICIENT_RESOURCES", TransientError),
        ("SERVER_STARTING_UP", "INTERNAL_ERROR", TransientError),
        ("SYNTAX_ERROR", "USER_ERROR", AdapterError),
        ("GENERIC_INTERNAL_ERROR", "INTERNAL_ERROR", AdapterError),
    ],
)
def test_map_query_error(name: str, kind: str, expected: type[Exception]) -> None:
    err = map_query_error({"message": "line 1:1: algo", "errorName": name, "errorType": kind})
    assert type(err) is expected
    assert name in str(err)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AuthError),
        (403, PermissionDeniedError),
        (429, RateLimitError),
        (410, TransientError),
        (502, TransientError),
        (503, TransientError),
        (504, TransientError),
        (400, AdapterError),
        (500, AdapterError),
    ],
)
def test_map_status(status: int, expected: type[Exception]) -> None:
    assert type(map_status(status, "corpo")) is expected


def test_rate_limit_keeps_retry_after() -> None:
    err = map_status(429, "", retry_after="7")
    assert isinstance(err, RateLimitError)
    assert err.retry_after_s == 7


def test_transport_errors_are_transient() -> None:
    req = httpx2.Request("GET", "http://x")
    assert isinstance(
        map_transport_error(httpx2.ConnectError("recusada", request=req)), TransientError
    )
    assert isinstance(map_transport_error(httpx2.ReadTimeout("lento", request=req)), TransientError)


# ------------------------------------------------------------------ protocolo
async def test_execute_follows_next_uri_and_sends_headers(fake: FakeTrino) -> None:
    fake.add(
        "SELECT id",
        {"stats": {"state": "QUEUED"}},
        {"columns": cols(("id", "integer"), ("nome", "varchar(10)")), "data": [[1, "Ana"]]},
        {"data": [[2, "Bruno"]]},
        {"stats": {"state": "FINISHED", "physicalInputBytes": 123}},
    )
    engine = make_engine(fake, user="ana", password="s3")
    result = await engine.execute("SELECT id, nome FROM t", timeout_s=42)
    assert result.preview.to_pylist() == [{"id": 1, "nome": "Ana"}, {"id": 2, "nome": "Bruno"}]
    assert [(c.name, c.data_type) for c in result.columns] == [
        ("id", "integer"),
        ("nome", "varchar(10)"),
    ]
    assert result.total_rows == 2
    assert result.query_id == "20260101_000000_00001_abcde"
    assert result.cost is not None
    assert result.cost.bytes_scanned == 123
    post = fake.requests[0]
    assert post.headers["X-Trino-User"] == "ana"
    assert post.headers["X-Trino-Catalog"] == "memory"
    assert post.headers["X-Trino-Schema"] == "default"
    assert post.headers["X-Trino-Session"] == "query_max_run_time=42s"
    assert post.headers["X-Trino-Client-Capabilities"] == "PARAMETRIC_DATETIME"
    assert post.headers["Authorization"].startswith("Basic ")
    assert not fake.deletes()
    assert not engine.client.running


async def test_no_auth_header_without_password(fake: FakeTrino) -> None:
    fake.add("SELECT 1", {"columns": cols(("x", "integer")), "data": [[1]]})
    await make_engine(fake).execute("SELECT 1")
    assert "Authorization" not in fake.requests[0].headers


async def test_max_rows_truncates_and_cancels_rest(fake: FakeTrino) -> None:
    fake.add(
        "SELECT g",
        {"columns": cols(("g", "bigint")), "data": [[1], [2]]},
        {"data": [[3], [4]]},
        {"data": [[5]]},
    )
    result = await make_engine(fake).execute("SELECT g FROM t", max_rows=3)
    assert result.preview.column("g").to_pylist() == [1, 2, 3]
    assert result.total_rows is None
    assert len(fake.deletes()) == 1  # o resto da consulta foi cancelado


async def test_ddl_returns_empty_table(fake: FakeTrino) -> None:
    fake.add("CREATE TABLE", {"updateType": "CREATE TABLE"}, {"updateType": "CREATE TABLE"})
    result = await make_engine(fake).execute("CREATE TABLE t (id int)")
    assert result.columns == []
    assert result.preview.num_rows == 0
    assert result.total_rows == 0


async def test_query_error_is_mapped(fake: FakeTrino) -> None:
    fake.add(
        "nao_existe",
        {"stats": {"state": "QUEUED"}},
        {
            "error": {
                "message": "line 1:15: Table 'memory.default.nao_existe' does not exist",
                "errorName": "TABLE_NOT_FOUND",
                "errorType": "USER_ERROR",
            }
        },
    )
    with pytest.raises(NotFoundError, match="does not exist"):
        await make_engine(fake).execute("SELECT * FROM nao_existe")


async def test_busy_server_is_retried(fake: FakeTrino, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dengcode_adapter_trino.client._RETRY_DELAY_S", 0)
    fake.fail_first = [503, 502]
    fake.add("SELECT 1", {"columns": cols(("x", "integer")), "data": [[1]]})
    result = await make_engine(fake).execute("SELECT 1")
    assert result.preview.num_rows == 1


async def test_server_always_busy_is_transient(
    fake: FakeTrino, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dengcode_adapter_trino.client._RETRY_DELAY_S", 0)
    fake.fail_first = [503] * 10
    with pytest.raises(TransientError, match="503"):
        await make_engine(fake).execute("SELECT 1")


async def test_unauthorized_is_auth_error(fake: FakeTrino) -> None:
    fake.fail_first = [401]
    with pytest.raises(AuthError) as info:
        await make_engine(fake, password="senha-secreta").execute("SELECT 1")
    assert "senha-secreta" not in str(info.value)


async def test_connection_refused_is_transient() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("conexão recusada", request=request)

    engine = TrinoQueryEngine.from_config(
        {}, CTX, transport_factory=lambda: httpx2.MockTransport(refuse)
    )
    with pytest.raises(TransientError, match="conexão"):
        await engine.execute("SELECT 1")


async def test_client_timeout_cancels_on_server(
    fake: FakeTrino, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dengcode_adapter_trino.engine._CLIENT_GRACE_S", 0.1)
    fake.hang_on_get = True
    fake.add("lenta", {"stats": {"state": "RUNNING"}}, {"data": []})
    with pytest.raises(TransientError, match="tempo esgotado"):
        await make_engine(fake).execute("SELECT lenta", timeout_s=1)
    assert len(fake.deletes()) == 1


async def test_task_cancel_cancels_on_server(fake: FakeTrino) -> None:
    fake.hang_on_get = True
    fake.add("lenta", {"stats": {"state": "RUNNING"}}, {"data": []})
    engine = make_engine(fake)
    task = asyncio.create_task(engine.execute("SELECT lenta"))
    for _ in range(100):
        if engine.client.running:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fake.deletes()) == 1
    assert not engine.client.running


async def test_cancel_by_id_while_running(fake: FakeTrino) -> None:
    fake.hang_on_get = True
    fake.add("lenta", {"stats": {"state": "RUNNING"}}, {"data": []})
    engine = make_engine(fake)
    task = asyncio.create_task(engine.execute("SELECT lenta"))
    for _ in range(100):
        if engine.client.running:
            break
        await asyncio.sleep(0.01)
    query_id = next(iter(engine.client.running))
    await engine.cancel(query_id)
    assert fake.deletes() == [f"/v1/statement/executing/{query_id}/1"]
    task.cancel()  # o GET de mentira não reage ao DELETE; encerra a tarefa
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_cancel_unknown_id_uses_query_api(fake: FakeTrino) -> None:
    await make_engine(fake).cancel("20260101_000000_00009_zzzzz")
    assert fake.deletes() == ["/v1/query/20260101_000000_00009_zzzzz"]


async def test_explain_and_dry_run(fake: FakeTrino) -> None:
    fake.add("EXPLAIN", {"columns": cols(("Query Plan", "varchar")), "data": [["Fragment 0"]]})
    engine = make_engine(fake)
    assert await engine.explain("SELECT 1") == "Fragment 0"
    assert fake.statements[-1] == "EXPLAIN SELECT 1"
    with pytest.raises(UnsupportedOperation):
        await engine.dry_run("SELECT 1")


# ------------------------------------------------------------------ catálogo
def test_quoting() -> None:
    assert quote_ident('a"b') == '"a""b"'
    assert quote_literal("d'água") == "'d''água'"


def test_stats_expressions_by_type() -> None:
    num = stats_expressions("valor", "decimal(10,2)")
    assert num == [
        'count("valor")',
        'approx_distinct("valor")',
        'CAST(min("valor") AS varchar)',
        'CAST(max("valor") AS varchar)',
    ]
    flag = stats_expressions("ativo", "boolean")
    assert flag[2] == 'CAST(bool_and("ativo") AS varchar)'
    doc = stats_expressions("doc", "json")
    assert doc[1] == 'approx_distinct(json_format("doc"))'
    assert doc[2] == "CAST(NULL AS varchar)"
    tags = stats_expressions("tags", "array(varchar)")
    assert tags[1] == "CAST(NULL AS bigint)"
    uid = stats_expressions("uid", "uuid")
    assert uid[2] == 'min(CAST("uid" AS varchar))'
    ttz = stats_expressions("hora", "time(3) with time zone")
    assert ttz[2] == "CAST(NULL AS varchar)"


def test_build_stats() -> None:
    columns = [ColumnInfo(name="id", data_type="integer"), ColumnInfo(name="t", data_type="map")]
    stats = build_stats(columns, [3, 3, 3, "1", "3", 2, None, None, None])
    assert stats[0].null_fraction == 0
    assert (stats[0].distinct_count, stats[0].min, stats[0].max) == (3, "1", "3")
    assert stats[1].null_fraction == pytest.approx(1 / 3)
    assert stats[1].distinct_count is None
    empty = build_stats(columns[:1], [0, 0, 0, None, None])
    assert empty[0].null_fraction is None


def scripted_catalog(fake: FakeTrino) -> TrinoCatalog:
    """Respostas de metadados de uma tabela ``memory.vendas.clientes``."""
    fake.add(
        "information_schema.columns",
        {
            "columns": cols(
                ("column_name", "varchar"),
                ("data_type", "varchar"),
                ("is_nullable", "varchar"),
                ("comment", "varchar"),
            ),
            "data": [["id", "integer", "NO", "chave"], ["email", "varchar(120)", "YES", None]],
        },
    )
    fake.add("system.metadata.catalogs", {"columns": cols(("c", "varchar")), "data": [["iceberg"]]})
    fake.add("table_comments", {"columns": cols(("c", "varchar")), "data": [["clientes"]]})
    fake.add("approx_distinct", {"data": [[3, 3, 3, "1", "3", 2, 2, "a@x", "c@x"]]})
    fake.add("count(*)", {"columns": cols(("_col0", "bigint")), "data": [[3]]})
    return TrinoCatalog(make_engine(fake))


async def test_describe_builds_table_info(fake: FakeTrino) -> None:
    info = await scripted_catalog(fake).describe(TableRef(schema="vendas", name="clientes"))
    assert info.ref == TableRef(catalog="memory", schema="vendas", name="clientes")
    assert [(c.name, c.nullable, c.description) for c in info.columns] == [
        ("id", False, "chave"),
        ("email", True, None),
    ]
    assert info.row_count == 3
    assert info.table_format == "iceberg"
    assert info.description == "clientes"
    assert len(info.schema_hash) == 16
    columns_sql = next(s for s in fake.statements if "information_schema.columns" in s)
    assert '"memory".information_schema.columns' in columns_sql
    assert "table_schema = 'vendas'" in columns_sql
    assert any('FROM "memory"."vendas"."clientes"' in s for s in fake.statements)


async def test_describe_tries_lowercase(fake: FakeTrino) -> None:
    catalog = TrinoCatalog(make_engine(fake))
    with pytest.raises(NotFoundError, match="não encontrada"):
        await catalog.describe(TableRef(schema="Vendas", name="Clientes"))
    names = [s for s in fake.statements if "information_schema.columns" in s]
    assert len(names) == 4  # (Vendas, vendas) x (Clientes, clientes)
    assert "table_name = 'clientes'" in names[-1]


async def test_column_stats_filter_and_missing(fake: FakeTrino) -> None:
    catalog = scripted_catalog(fake)
    stats = await catalog.column_stats(TableRef(schema="vendas", name="clientes"))
    assert [s.column for s in stats] == ["id", "email"]
    assert stats[1].null_fraction == pytest.approx(1 / 3)
    with pytest.raises(NotFoundError, match="nao_existe"):
        await catalog.column_stats(TableRef(schema="vendas", name="clientes"), ["nao_existe"])


async def test_catalog_requires_catalog_name() -> None:
    catalog = TrinoCatalog(TrinoQueryEngine.from_config({}, CTX))
    with pytest.raises(ConfigError, match="catálogo"):
        await catalog.list_schemas()
    with pytest.raises(UnsupportedOperation):
        await catalog.lineage(TableRef(name="x"), direction="up")


async def test_describe_without_schema_is_not_found() -> None:
    catalog = TrinoCatalog(TrinoQueryEngine.from_config({"catalog": "memory"}, CTX))
    with pytest.raises(NotFoundError, match="schema"):
        await catalog.describe(TableRef(name="x"))
