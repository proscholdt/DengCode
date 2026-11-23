"""CatalogPort sobre DuckDB (``information_schema`` + ``SUMMARIZE``)."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Literal

import duckdb

from dengcode_adapter_duckdb.engine import DuckDBQueryEngine, map_error
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


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class DuckDBCatalog:
    info: ClassVar[AdapterInfo] = AdapterInfo(name="duckdb", port=PortKind.CATALOG, version="0.1.0")
    name = "duckdb"

    def __init__(self, engine: DuckDBQueryEngine) -> None:
        self._engine = engine

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> DuckDBCatalog:
        peer = ctx.peers.get("query_engine")
        if isinstance(peer, DuckDBQueryEngine) and "database" not in cfg:
            return cls(peer)  # mesmo banco do motor de consulta
        return cls(DuckDBQueryEngine.from_config(cfg, ctx))

    def capabilities(self) -> CatalogCapabilities:
        return CatalogCapabilities(
            supports_lineage=False, supports_column_stats=True, supports_tags=False
        )

    def _query(
        self, sql: str, params: list[Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        with self._engine.session() as cur:
            try:
                rows = cur.execute(sql, params or []).fetchall()
                return [d[0] for d in cur.description or []], rows
            except duckdb.Error as exc:
                raise map_error(exc) from exc

    async def _aquery(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        return (await asyncio.to_thread(self._query, sql, params))[1]

    async def list_schemas(self, catalog: str | None = None) -> list[str]:
        sql = "SELECT DISTINCT schema_name FROM information_schema.schemata"
        params: list[Any] = []
        if catalog:
            sql += " WHERE catalog_name = ?"
            params.append(catalog)
        rows = await self._aquery(sql + " ORDER BY 1", params)
        return [str(r[0]) for r in rows if r[0] not in ("information_schema", "pg_catalog")]

    async def list_tables(self, schema: str, *, catalog: str | None = None) -> list[TableRef]:
        sql = (
            "SELECT table_catalog, table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema = ?"
        )
        params: list[Any] = [schema]
        if catalog:
            sql += " AND table_catalog = ?"
            params.append(catalog)
        rows = await self._aquery(sql + " ORDER BY table_name", params)
        return [TableRef(catalog=r[0], schema=r[1], name=r[2]) for r in rows]

    def _where(self, ref: TableRef) -> tuple[str, list[Any]]:
        clauses, params = ["table_name = ?"], [ref.name]
        if ref.schema_:
            clauses.append("table_schema = ?")
            params.append(ref.schema_)
        if ref.catalog:
            clauses.append("table_catalog = ?")
            params.append(ref.catalog)
        return " AND ".join(clauses), params

    def _fq(self, ref: TableRef) -> str:
        return ".".join(_quote(p) for p in (ref.catalog, ref.schema_, ref.name) if p)

    async def describe(self, ref: TableRef) -> TableInfo:
        where, params = self._where(ref)
        rows = await self._aquery(
            "SELECT column_name, data_type, is_nullable, table_catalog, table_schema "
            f"FROM information_schema.columns WHERE {where} ORDER BY ordinal_position",
            params,
        )
        if not rows:
            raise NotFoundError(f"tabela não encontrada: {ref.qualified()}")
        columns = [ColumnInfo(name=r[0], data_type=r[1], nullable=r[2] == "YES") for r in rows]
        real = TableRef(catalog=rows[0][3], schema=rows[0][4], name=ref.name)
        count = await self._aquery(f"SELECT count(*) FROM {self._fq(real)}")
        return TableInfo(
            ref=real,
            columns=columns,
            table_format="duckdb",
            row_count=int(count[0][0]),
            schema_hash=schema_hash(columns),
        )

    async def column_stats(
        self, ref: TableRef, columns: list[str] | None = None
    ) -> list[ColumnStats]:
        info = await self.describe(ref)
        names, rows = await asyncio.to_thread(self._query, f"SUMMARIZE {self._fq(info.ref)}")
        idx = {n: i for i, n in enumerate(names)}
        wanted = set(columns) if columns else None
        out: list[ColumnStats] = []
        for r in rows:
            col = str(r[idx["column_name"]])
            if wanted is not None and col not in wanted:
                continue
            null_pct = r[idx["null_percentage"]]
            out.append(
                ColumnStats(
                    column=col,
                    null_fraction=float(null_pct) / 100 if null_pct is not None else None,
                    distinct_count=int(r[idx["approx_unique"]])
                    if r[idx["approx_unique"]] is not None
                    else None,
                    min=None if r[idx["min"]] is None else str(r[idx["min"]]),
                    max=None if r[idx["max"]] is None else str(r[idx["max"]]),
                )
            )
        return out

    async def lineage(
        self, ref: TableRef, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        raise UnsupportedOperation("DuckDB não guarda lineage; o core calcula pelo SQL")
