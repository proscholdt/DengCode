"""CatalogPort sobre PostgreSQL (``information_schema`` + agregações SQL).

O "catálogo" (``TableRef.catalog``) é o banco: só o banco da conexão é
visível, como no ``information_schema``. As estatísticas de coluna são
calculadas com agregações (não dependem de ``ANALYZE`` nem de ``pg_stats``).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Literal

from psycopg import sql as pgsql

from dengcode_adapter_postgres.engine import CONNECTION_KEYS, Connection, PostgresQueryEngine, Row
from dengcode_core.errors import NotFoundError, UnsupportedOperation
from dengcode_core.ports.models import (
    AdapterContext,
    AdapterInfo,
    CatalogCapabilities,
    ColumnInfo,
    ColumnStats,
    LineageEdge,
    PortKind,
    TableInfo,
    TableRef,
    schema_hash,
)

DEFAULT_TIMEOUT_S = 300

_SCHEMAS_SQL = pgsql.SQL(
    "SELECT schema_name FROM information_schema.schemata "
    "WHERE schema_name NOT IN ('pg_catalog', 'information_schema') "
    "AND schema_name !~ '^pg_(toast|temp_)'"
)
_TABLES_SQL = pgsql.SQL(
    "SELECT table_catalog, table_schema, table_name FROM information_schema.tables "
    "WHERE table_schema = %s"
)
# schema da tabela pelo search_path, quando o TableRef não traz schema
_RESOLVE_SCHEMA_SQL = pgsql.SQL(
    "SELECT table_schema FROM information_schema.tables "
    "WHERE table_name = %s AND table_schema::name = ANY(pg_catalog.current_schemas(false)) "
    "ORDER BY pg_catalog.array_position(pg_catalog.current_schemas(false), table_schema::name) "
    "LIMIT 1"
)
# colunas pelo information_schema; o tipo exibido vem de format_type (inclui
# modificadores, ex.: character varying(50), numeric(10,2), integer[])
_COLUMNS_SQL = pgsql.SQL(
    "SELECT c.column_name, pg_catalog.format_type(a.atttypid, a.atttypmod), c.is_nullable, "
    "c.table_catalog, c.table_schema, pg_catalog.col_description(a.attrelid, a.attnum), "
    "pg_catalog.obj_description(a.attrelid, 'pg_class') "
    "FROM information_schema.columns c "
    "JOIN pg_catalog.pg_attribute a "
    "ON a.attrelid = (pg_catalog.quote_ident(c.table_schema) || '.' "
    "|| pg_catalog.quote_ident(c.table_name))::regclass "
    "AND a.attname::text = c.column_name::text AND a.attnum > 0 AND NOT a.attisdropped "
    "WHERE c.table_schema = %s AND c.table_name = %s"
)

_TYPMOD = re.compile(r"\(\d+(?:\s*,\s*\d+)?\)")
# tipos com ordem total nativa: min/max direto
_ORDERED_TYPES = frozenset(
    {
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "real",
        "double precision",
        "money",
        "text",
        "character varying",
        "character",
        '"char"',
        "name",
        "date",
        "time without time zone",
        "time with time zone",
        "timestamp without time zone",
        "timestamp with time zone",
        "interval",
    }
)
# tipos com igualdade nativa (count distinct direto), sem min/max útil
_EQUALITY_TYPES = frozenset({"boolean", "uuid", "bytea", "inet", "cidr", "macaddr", "jsonb"})


def base_type(data_type: str) -> str:
    """Tipo sem modificadores: ``timestamp(3) with time zone`` → ``timestamp with time zone``."""
    return " ".join(_TYPMOD.sub("", data_type).split())


def stats_expressions(column: str, data_type: str) -> list[pgsql.Composable]:
    """``count``, ``count distinct``, ``min`` e ``max`` (como texto) de uma coluna."""
    ident = pgsql.Identifier(column)
    kind = base_type(data_type)
    is_array = kind.endswith("[]")
    native_eq = not is_array and (kind in _ORDERED_TYPES or kind in _EQUALITY_TYPES)
    distinct = (
        pgsql.SQL("count(DISTINCT {})").format(ident)
        if native_eq
        else pgsql.SQL("count(DISTINCT {}::text)").format(ident)
    )
    if not is_array and kind in _ORDERED_TYPES:
        low = pgsql.SQL("min({})::text").format(ident)
        high = pgsql.SQL("max({})::text").format(ident)
    elif kind == "boolean":
        low = pgsql.SQL("bool_and({})::text").format(ident)
        high = pgsql.SQL("bool_or({})::text").format(ident)
    elif kind == "uuid":  # o texto canônico do uuid preserva a ordem
        low = pgsql.SQL("min({}::text)").format(ident)
        high = pgsql.SQL("max({}::text)").format(ident)
    else:
        low = high = pgsql.SQL("NULL::text")
    return [pgsql.SQL("count({})").format(ident), distinct, low, high]


def build_stats(columns: list[ColumnInfo], row: Row) -> list[ColumnStats]:
    """Interpreta a linha ``count(*), [count, distinct, min, max] * N``."""
    total = int(row[0])
    out: list[ColumnStats] = []
    for i, col in enumerate(columns):
        non_null, distinct, low, high = row[1 + 4 * i : 5 + 4 * i]
        out.append(
            ColumnStats(
                column=col.name,
                null_fraction=(total - int(non_null)) / total if total else None,
                distinct_count=int(distinct),
                min=None if low is None else str(low),
                max=None if high is None else str(high),
            )
        )
    return out


async def _rows(
    conn: Connection, query: pgsql.SQL | pgsql.Composed, params: list[Any]
) -> list[Row]:
    async with conn.cursor() as cur:
        await cur.execute(query, params or None)
        return await cur.fetchall()


class PostgresCatalog:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="postgres", port=PortKind.CATALOG, version="0.1.0"
    )
    name = "postgres"

    def __init__(self, engine: PostgresQueryEngine, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
        self._engine = engine
        self._timeout_s = timeout_s

    @property
    def engine(self) -> PostgresQueryEngine:
        return self._engine

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> PostgresCatalog:
        peer = ctx.peers.get("query_engine")
        has_own_connection = any(cfg.get(key) not in (None, "") for key in CONNECTION_KEYS)
        if isinstance(peer, PostgresQueryEngine) and not has_own_connection:
            return cls(peer)  # mesmo banco do motor de consulta
        return cls(PostgresQueryEngine.from_config(cfg, ctx))

    def capabilities(self) -> CatalogCapabilities:
        return CatalogCapabilities(
            supports_lineage=False, supports_column_stats=True, supports_tags=False
        )

    async def _query(self, query: pgsql.SQL | pgsql.Composed, params: list[Any]) -> list[Row]:
        async with self._engine.connect(timeout_s=self._timeout_s) as conn:
            return await _rows(conn, query, params)

    async def list_schemas(self, catalog: str | None = None) -> list[str]:
        query: pgsql.SQL | pgsql.Composed = _SCHEMAS_SQL
        params: list[Any] = []
        if catalog:
            query = query + pgsql.SQL(" AND catalog_name = %s")
            params.append(catalog)
        rows = await self._query(query + pgsql.SQL(" ORDER BY 1"), params)
        return [str(r[0]) for r in rows]

    async def list_tables(self, schema: str, *, catalog: str | None = None) -> list[TableRef]:
        query: pgsql.SQL | pgsql.Composed = _TABLES_SQL
        params: list[Any] = [schema]
        if catalog:
            query = query + pgsql.SQL(" AND table_catalog = %s")
            params.append(catalog)
        rows = await self._query(query + pgsql.SQL(" ORDER BY table_name"), params)
        return [TableRef(catalog=r[0], schema=r[1], name=r[2]) for r in rows]

    async def _describe(self, conn: Connection, ref: TableRef) -> TableInfo:
        """Colunas + descrição da tabela (sem contagem de linhas)."""
        schema = ref.schema_
        if schema is None:
            found = await _rows(conn, _RESOLVE_SCHEMA_SQL, [ref.name])
            if not found:
                raise NotFoundError(f"tabela não encontrada: {ref.qualified()}")
            schema = str(found[0][0])
        query: pgsql.SQL | pgsql.Composed = _COLUMNS_SQL
        params: list[Any] = [schema, ref.name]
        if ref.catalog:
            query = query + pgsql.SQL(" AND c.table_catalog = %s")
            params.append(ref.catalog)
        rows = await _rows(conn, query + pgsql.SQL(" ORDER BY c.ordinal_position"), params)
        if not rows:
            raise NotFoundError(f"tabela não encontrada: {ref.qualified()}")
        columns = [
            ColumnInfo(name=r[0], data_type=r[1], nullable=r[2] == "YES", description=r[5])
            for r in rows
        ]
        real = TableRef(catalog=rows[0][3], schema=rows[0][4], name=ref.name)
        return TableInfo(
            ref=real,
            columns=columns,
            table_format="postgres",
            description=rows[0][6],
            schema_hash=schema_hash(columns),
        )

    @staticmethod
    def _table_ident(info: TableInfo) -> pgsql.Identifier:
        return pgsql.Identifier(str(info.ref.schema_), info.ref.name)

    async def describe(self, ref: TableRef) -> TableInfo:
        async with self._engine.connect(timeout_s=self._timeout_s) as conn:
            info = await self._describe(conn, ref)
            count = await _rows(
                conn,
                pgsql.SQL("SELECT count(*) FROM {}").format(self._table_ident(info)),
                [],
            )
        return info.model_copy(update={"row_count": int(count[0][0])})

    async def column_stats(
        self, ref: TableRef, columns: list[str] | None = None
    ) -> list[ColumnStats]:
        async with self._engine.connect(timeout_s=self._timeout_s) as conn:
            info = await self._describe(conn, ref)
            selected = info.columns
            if columns is not None:
                known = {c.name for c in info.columns}
                missing = [c for c in columns if c not in known]
                if missing:
                    raise NotFoundError(
                        f"colunas não encontradas em {info.ref.qualified()}: {', '.join(missing)}"
                    )
                wanted = set(columns)
                selected = [c for c in info.columns if c.name in wanted]
            parts: list[pgsql.Composable] = [pgsql.SQL("count(*)")]
            for col in selected:
                parts.extend(stats_expressions(col.name, col.data_type))
            query = pgsql.SQL("SELECT {} FROM {}").format(
                pgsql.SQL(", ").join(parts), self._table_ident(info)
            )
            rows = await _rows(conn, query, [])
        return build_stats(selected, rows[0])

    async def lineage(
        self, ref: TableRef, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        raise UnsupportedOperation("PostgreSQL não guarda lineage; o core calcula pelo SQL")
