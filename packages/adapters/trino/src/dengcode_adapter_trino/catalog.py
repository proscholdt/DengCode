"""CatalogPort sobre Trino (``information_schema`` do catálogo + agregações SQL).

``TableRef.catalog`` é o catálogo do Trino (``memory``, ``iceberg``,
``hive``...); sem ele vale o ``catalog`` da config. O mesmo para o schema.
O Trino guarda nomes em minúsculas na maioria dos conectores: se a busca exata
não acha nada, tenta de novo com o nome em minúsculas.

Estatísticas de coluna saem de uma única agregação: ``count``,
``approx_distinct`` (HyperLogLog, erro padrão ~2,3% — barato em tabela grande)
e ``min``/``max`` como texto.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar, Literal

from dengcode_adapter_trino.arrow import parse_type
from dengcode_adapter_trino.engine import CONNECTION_KEYS, KNOWLEDGE_PACK, TrinoQueryEngine
from dengcode_core.errors import ConfigError, DengCodeError, NotFoundError, UnsupportedOperation
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

# conector do Trino → formato de tabela (os demais ficam sem formato declarado)
_TABLE_FORMATS = {
    "iceberg": "iceberg",
    "delta_lake": "delta",
    "delta-lake": "delta",
    "hudi": "hudi",
    "hive": "hive",
}
# tipos com ordem total: min/max direto
_ORDERED = frozenset(
    {
        "tinyint",
        "smallint",
        "integer",
        "int",
        "bigint",
        "real",
        "double",
        "decimal",
        "varchar",
        "char",
        "date",
        "time",
        "timestamp",
    }
)
# tipos com igualdade (approx_distinct direto), sem min/max útil
_EQUALITY = frozenset({"boolean", "uuid", "varbinary", "ipaddress"})


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def qualified(catalog: str, schema: str, table: str) -> str:
    return ".".join(quote_ident(p) for p in (catalog, schema, table))


def _variants(name: str) -> list[str]:
    lower = name.lower()
    return [name] if lower == name else [name, lower]


def stats_expressions(column: str, data_type: str) -> list[str]:
    """``count``, ``approx_distinct``, ``min`` e ``max`` (como texto) de uma coluna."""
    ident = quote_ident(column)
    parsed = parse_type(data_type)
    base = parsed.base
    count = f"count({ident})"
    if base in _ORDERED or base in _EQUALITY:
        distinct = f"approx_distinct({ident})"
    elif base == "json":
        distinct = f"approx_distinct(json_format({ident}))"
    else:  # array, map, row, intervalos...: sem distintos baratos
        distinct = "CAST(NULL AS bigint)"
    if base in _ORDERED and not (base == "time" and parsed.with_time_zone):
        low, high = f"CAST(min({ident}) AS varchar)", f"CAST(max({ident}) AS varchar)"
    elif base == "boolean":
        low, high = f"CAST(bool_and({ident}) AS varchar)", f"CAST(bool_or({ident}) AS varchar)"
    elif base in ("uuid", "ipaddress"):  # o texto canônico preserva a ordem útil
        low, high = f"min(CAST({ident} AS varchar))", f"max(CAST({ident} AS varchar))"
    else:
        low = high = "CAST(NULL AS varchar)"
    return [count, distinct, low, high]


def build_stats(columns: Sequence[ColumnInfo], row: Sequence[Any]) -> list[ColumnStats]:
    """Interpreta a linha ``count(*), [count, distinct, min, max] * N``."""
    total = int(row[0])
    out: list[ColumnStats] = []
    for i, col in enumerate(columns):
        non_null, distinct, low, high = row[1 + 4 * i : 5 + 4 * i]
        out.append(
            ColumnStats(
                column=col.name,
                null_fraction=(total - int(non_null)) / total if total else None,
                distinct_count=None if distinct is None else int(distinct),
                min=None if low is None else str(low),
                max=None if high is None else str(high),
            )
        )
    return out


class TrinoCatalog:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="trino", port=PortKind.CATALOG, version="0.1.0", knowledge_pack=KNOWLEDGE_PACK
    )
    name = "trino"

    def __init__(self, engine: TrinoQueryEngine, *, timeout_s: int = DEFAULT_TIMEOUT_S) -> None:
        self._engine = engine
        self._timeout_s = timeout_s
        self._connectors: dict[str, str | None] = {}

    @property
    def engine(self) -> TrinoQueryEngine:
        return self._engine

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> TrinoCatalog:
        peer = ctx.peers.get("query_engine")
        has_own_connection = any(cfg.get(key) not in (None, "") for key in CONNECTION_KEYS)
        if isinstance(peer, TrinoQueryEngine) and not has_own_connection:
            return cls(peer)  # mesmo coordenador do motor de consulta
        return cls(TrinoQueryEngine.from_config(cfg, ctx))

    def capabilities(self) -> CatalogCapabilities:
        return CatalogCapabilities(
            supports_lineage=False, supports_column_stats=True, supports_tags=False
        )

    # ------------------------------------------------------------ auxiliares
    async def _rows(self, sql: str) -> list[list[Any]]:
        result = await self._engine.run(sql, timeout_s=self._timeout_s)
        return result.rows

    def _catalog(self, catalog: str | None) -> str:
        name = catalog or self._engine.settings.catalog
        if not name:
            raise ConfigError(
                "trino: informe o catálogo (TableRef.catalog ou 'catalog' na config da stack)"
            )
        return name

    async def _table_format(self, catalog: str) -> str | None:
        if catalog not in self._connectors:
            try:
                rows = await self._rows(
                    "SELECT connector_name FROM system.metadata.catalogs "
                    f"WHERE catalog_name = {quote_literal(catalog)}"
                )
            except DengCodeError:  # sem acesso ao catálogo system: segue sem formato
                rows = []
            connector = str(rows[0][0]).lower() if rows and rows[0][0] else None
            self._connectors[catalog] = _TABLE_FORMATS.get(connector or "")
        return self._connectors[catalog]

    async def _table_comment(self, catalog: str, schema: str, table: str) -> str | None:
        try:
            rows = await self._rows(
                "SELECT comment FROM system.metadata.table_comments "
                f"WHERE catalog_name = {quote_literal(catalog)} "
                f"AND schema_name = {quote_literal(schema)} "
                f"AND table_name = {quote_literal(table)}"
            )
        except DengCodeError:
            return None
        return str(rows[0][0]) if rows and rows[0][0] is not None else None

    # ------------------------------------------------------------ porta
    async def list_schemas(self, catalog: str | None = None) -> list[str]:
        cat = self._catalog(catalog)
        rows = await self._rows(
            f"SELECT schema_name FROM {quote_ident(cat)}.information_schema.schemata "
            "WHERE schema_name <> 'information_schema' ORDER BY schema_name"
        )
        return [str(r[0]) for r in rows]

    async def list_tables(self, schema: str, *, catalog: str | None = None) -> list[TableRef]:
        cat = self._catalog(catalog)
        rows: list[list[Any]] = []
        for name in _variants(schema):
            rows = await self._rows(
                "SELECT table_catalog, table_schema, table_name "
                f"FROM {quote_ident(cat)}.information_schema.tables "
                f"WHERE table_schema = {quote_literal(name)} ORDER BY table_name"
            )
            if rows:
                break
        return [TableRef(catalog=str(r[0]), schema=str(r[1]), name=str(r[2])) for r in rows]

    async def _columns(self, ref: TableRef) -> tuple[TableRef, list[ColumnInfo]]:
        cat = self._catalog(ref.catalog)
        schema = ref.schema_ or self._engine.settings.schema
        if not schema:
            raise NotFoundError(
                f"trino: tabela sem schema ({ref.qualified()}); "
                "informe o schema ou 'schema' na config da stack"
            )
        for schema_name in _variants(schema):
            for table_name in _variants(ref.name):
                rows = await self._rows(
                    "SELECT column_name, data_type, is_nullable, comment "
                    f"FROM {quote_ident(cat)}.information_schema.columns "
                    f"WHERE table_schema = {quote_literal(schema_name)} "
                    f"AND table_name = {quote_literal(table_name)} "
                    "ORDER BY ordinal_position"
                )
                if rows:
                    columns = [
                        ColumnInfo(
                            name=str(r[0]),
                            data_type=str(r[1]),
                            nullable=None if r[2] is None else str(r[2]).upper() == "YES",
                            description=None if r[3] in (None, "") else str(r[3]),
                        )
                        for r in rows
                    ]
                    real = TableRef(catalog=cat, schema=schema_name, name=table_name)
                    return real, columns
        raise NotFoundError(f"trino: tabela não encontrada: {cat}.{schema}.{ref.name}")

    async def describe(self, ref: TableRef) -> TableInfo:
        real, columns = await self._columns(ref)
        cat, schema = str(real.catalog), str(real.schema_)
        count = await self._rows(f"SELECT count(*) FROM {qualified(cat, schema, real.name)}")
        return TableInfo(
            ref=real,
            columns=columns,
            table_format=await self._table_format(cat),
            row_count=int(count[0][0]) if count else None,
            description=await self._table_comment(cat, schema, real.name),
            schema_hash=schema_hash(columns),
        )

    async def column_stats(
        self, ref: TableRef, columns: list[str] | None = None
    ) -> list[ColumnStats]:
        real, all_columns = await self._columns(ref)
        selected = all_columns
        if columns is not None:
            known = {c.name for c in all_columns}
            missing = [c for c in columns if c not in known]
            if missing:
                raise NotFoundError(
                    f"trino: colunas não encontradas em {real.qualified()}: {', '.join(missing)}"
                )
            wanted = set(columns)
            selected = [c for c in all_columns if c.name in wanted]
        parts = ["count(*)"]
        for col in selected:
            parts.extend(stats_expressions(col.name, col.data_type))
        target = qualified(str(real.catalog), str(real.schema_), real.name)
        rows = await self._rows(f"SELECT {', '.join(parts)} FROM {target}")
        return build_stats(selected, rows[0])

    async def lineage(
        self, ref: TableRef, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        raise UnsupportedOperation("Trino não guarda lineage; o core calcula pelo SQL")
