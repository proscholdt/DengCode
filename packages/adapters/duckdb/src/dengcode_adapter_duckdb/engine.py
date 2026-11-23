"""QueryEnginePort sobre DuckDB.

DuckDB é síncrono: cada consulta roda numa thread com um cursor próprio; o
timeout interrompe a conexão (``interrupt``).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Iterator
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
    """Banco em memória: conexão persistente. Banco em arquivo: uma conexão por
    operação, para outro processo (ex.: dbt) poder escrever no mesmo arquivo
    entre as consultas — o DuckDB só permite um processo escritor por arquivo."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="duckdb",
        port=PortKind.QUERY_ENGINE,
        version="0.1.0",
        knowledge_pack="dengcode_adapter_duckdb:knowledge/duckdb.md",
    )
    name = "duckdb"

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection | None = None,
        *,
        database: str = ":memory:",
        search_path: Path | None = None,
        read_only: bool = False,
    ) -> None:
        if conn is None and database == ":memory:":
            conn = duckdb.connect(":memory:")
        self._conn = conn
        self.database = database
        self._search_path = search_path
        self._read_only = read_only
        self._running: dict[str, duckdb.DuckDBPyConnection] = {}
        if conn is not None:
            self._configure(conn)

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> DuckDBQueryEngine:
        database = str(cfg.get("database", ":memory:"))
        if database != ":memory:" and not Path(database).is_absolute():
            base = ctx.project_dir or Path.cwd()
            database = str((base / database).resolve())
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        search = ctx.project_dir.resolve() if ctx.project_dir is not None else None
        if cfg.get("file_search_path"):  # ex.: a pasta do projeto dbt, de onde partem os caminhos
            custom = Path(str(cfg["file_search_path"])).expanduser()
            search = (custom if custom.is_absolute() else (search or Path.cwd()) / custom).resolve()
        try:
            return cls(
                database=database, search_path=search, read_only=bool(cfg.get("read_only", False))
            )
        except duckdb.Error as exc:
            raise map_error(exc) from exc

    def _configure(self, conn: duckdb.DuckDBPyConnection) -> None:
        if self._search_path is not None:
            # caminhos relativos em read_csv('dados/x.csv') partem do projeto, não do cwd
            search = str(self._search_path).replace("'", "''")
            conn.execute(f"SET GLOBAL file_search_path = '{search}'")

    @contextlib.contextmanager
    def session(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Cursor/conexão para uma operação (fechado ao final)."""
        try:
            if self._conn is not None:
                cur = self._conn.cursor()
            else:
                cur = duckdb.connect(self.database, read_only=self._read_only)
                self._configure(cur)
        except duckdb.Error as exc:
            raise map_error(exc) from exc
        try:
            yield cur
        finally:
            cur.close()

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
        with self.session() as cur:
            return self._run_on(cur, sql, max_rows, query_id)

    def _run_on(
        self, cur: duckdb.DuckDBPyConnection, sql: str, max_rows: int, query_id: str
    ) -> tuple[pa.Table, bool]:
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
            with self.session() as cur:
                try:
                    rows = cur.execute(f"EXPLAIN {sql}").fetchall()
                except duckdb.Error as exc:
                    raise map_error(exc) from exc
            return "\n".join(str(r[-1]) for r in rows)

        return await asyncio.to_thread(run)

    async def dry_run(self, sql: str) -> CostEstimate:
        raise UnsupportedOperation("DuckDB não tem dry-run com estimativa; use explain")

    async def cancel(self, query_id: str) -> None:
        cur = self._running.get(query_id)
        if cur is not None:
            cur.interrupt()
