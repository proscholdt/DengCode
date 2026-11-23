"""QueryEnginePort sobre DuckDB.

DuckDB é síncrono: cada consulta roda numa thread com um cursor próprio; o
timeout interrompe a conexão (``interrupt``).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import pyarrow as pa

from dengcode_core.errors import (
    AdapterError,
    NotFoundError,
    PermissionDeniedError,
    TransientError,
    UnsupportedOperation,
)
from dengcode_core.ports.models import (
    AdapterContext,
    AdapterInfo,
    ColumnInfo,
    CostEstimate,
    PortKind,
    QueryEngineCapabilities,
    QueryResult,
)


def map_error(exc: duckdb.Error) -> Exception:
    msg = str(exc).strip()
    if isinstance(exc, duckdb.CatalogException):
        return NotFoundError(msg)
    if isinstance(exc, duckdb.PermissionException):
        return PermissionDeniedError(msg)
    if isinstance(exc, duckdb.InterruptException):
        return TransientError(f"consulta interrompida: {msg}")
    return AdapterError(msg)


class DuckDBQueryEngine:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="duckdb",
        port=PortKind.QUERY_ENGINE,
        version="0.1.0",
        knowledge_pack="dengcode_adapter_duckdb:knowledge/duckdb.md",
    )
    name = "duckdb"

    def __init__(self, conn: duckdb.DuckDBPyConnection, *, database: str = ":memory:") -> None:
        self.conn = conn
        self.database = database
        self._running: dict[str, duckdb.DuckDBPyConnection] = {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> DuckDBQueryEngine:
        database = str(cfg.get("database", ":memory:"))
        if database != ":memory:" and not Path(database).is_absolute():
            base = ctx.project_dir or Path.cwd()
            database = str((base / database).resolve())
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = duckdb.connect(database, read_only=bool(cfg.get("read_only", False)))
        except duckdb.Error as exc:
            raise map_error(exc) from exc
        return cls(conn, database=database)

    def capabilities(self) -> QueryEngineCapabilities:
        return QueryEngineCapabilities(
            sql_dialect="duckdb",
            supports_dry_run=False,
            supports_explain=True,
            supports_cost_estimate=False,
            supports_transactions=True,
            supports_cancel=True,
        )

    def _run(self, sql: str, max_rows: int, query_id: str) -> tuple[pa.Table, bool]:
        cur = self.conn.cursor()
        self._running[query_id] = cur
        try:
            rel = cur.sql(sql)
            if rel is None:  # DDL/DML: já executou
                return pa.table({}), False
            reader = rel.to_arrow_reader(min(max_rows, 10_000) or 1)
            batches: list[pa.RecordBatch] = []
            rows = 0
            truncated = False
            for batch in reader:
                take = min(batch.num_rows, max_rows - rows)
                batches.append(batch.slice(0, take))
                rows += take
                if rows >= max_rows:
                    truncated = True
                    break
            if not batches:
                return reader.schema.empty_table(), False
            return pa.Table.from_batches(batches, schema=reader.schema), truncated
        except duckdb.Error as exc:
            raise map_error(exc) from exc
        finally:
            self._running.pop(query_id, None)
            cur.close()

    async def execute(
        self, sql: str, *, max_rows: int = 10_000, timeout_s: int = 300
    ) -> QueryResult:
        query_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        task = asyncio.create_task(asyncio.to_thread(self._run, sql, max_rows, query_id))
        try:
            table, truncated = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError as exc:
            await self.cancel(query_id)
            await asyncio.gather(task, return_exceptions=True)
            raise TransientError(f"tempo esgotado após {timeout_s}s") from exc
        except asyncio.CancelledError:
            await self.cancel(query_id)
            raise
        columns = [
            ColumnInfo(name=f.name, data_type=str(f.type), nullable=f.nullable)
            for f in table.schema
        ]
        return QueryResult(
            query_id=query_id,
            columns=columns,
            preview=table,
            total_rows=None if truncated else table.num_rows,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    async def explain(self, sql: str) -> str:
        def run() -> str:
            cur = self.conn.cursor()
            try:
                rows = cur.execute(f"EXPLAIN {sql}").fetchall()
            except duckdb.Error as exc:
                raise map_error(exc) from exc
            finally:
                cur.close()
            return "\n".join(str(r[-1]) for r in rows)

        return await asyncio.to_thread(run)

    async def dry_run(self, sql: str) -> CostEstimate:
        raise UnsupportedOperation("DuckDB não tem dry-run com estimativa; use explain")

    async def cancel(self, query_id: str) -> None:
        cur = self._running.get(query_id)
        if cur is not None:
            cur.interrupt()
