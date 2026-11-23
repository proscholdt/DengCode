"""QueryEnginePort sobre PostgreSQL (psycopg 3, assíncrono).

Cada execução abre a própria conexão (autocommit) e a fecha no fim: não há
estado compartilhado entre consultas, o cancelamento é por conexão e uma
conexão quebrada nunca contamina a próxima consulta.

O tempo limite é aplicado no servidor (``statement_timeout`` da sessão) e, como
rede de segurança, no cliente (``asyncio.timeout`` com folga), que também
cancela a consulta no servidor.

No Windows o psycopg assíncrono exige um ``SelectorEventLoop`` (ver README).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, ClassVar, LiteralString, cast

import psycopg
from psycopg import sql as pgsql
from psycopg.abc import QueryNoTemplate as Query
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from dengcode_adapter_postgres.arrow import ColumnSpec, rows_to_table
from dengcode_adapter_postgres.errors import map_error
from dengcode_core.errors import ConfigError, TransientError, UnsupportedOperation
from dengcode_core.ports.models import (
    AdapterContext,
    AdapterInfo,
    ColumnInfo,
    CostEstimate,
    PortKind,
    QueryEngineCapabilities,
    QueryResult,
)

Row = tuple[Any, ...]
Connection = psycopg.AsyncConnection[Row]

CONNECTION_KEYS = ("dsn", "host", "port", "dbname", "user", "password")
_PARAM_KEYS = ("host", "port", "dbname", "user", "password")
DEFAULT_CONNECT_TIMEOUT_S = 10
# folga do timeout do cliente sobre o statement_timeout do servidor
_CLIENT_GRACE_S = 5.0
_CANCEL_TIMEOUT_S = 5.0
_SET_TIMEOUT = pgsql.SQL("SELECT pg_catalog.set_config('statement_timeout', {}, false)")


def build_conninfo(cfg: Mapping[str, Any]) -> str:
    """Monta a string de conexão a partir de ``dsn`` e/ou das chaves soltas.

    As chaves soltas (``host``, ``port``, ``dbname``, ``user``, ``password``)
    sobrescrevem as do ``dsn``. Chaves ausentes ficam com o padrão da libpq
    (variáveis ``PG*``, porta 5432). Mensagens de erro nunca incluem o DSN.
    """
    dsn = cfg.get("dsn") or ""
    if not isinstance(dsn, str):
        raise ConfigError("postgres: 'dsn' deve ser texto")
    params: dict[str, str] = {}
    for key in _PARAM_KEYS:
        value = cfg.get(key)
        if value is not None and value != "":
            params[key] = str(value)
    if "port" in params:
        try:
            port = int(params["port"])
        except ValueError:
            raise ConfigError("postgres: 'port' deve ser um número inteiro") from None
        if not 0 < port < 65536:
            raise ConfigError("postgres: 'port' fora do intervalo 1–65535")
    try:
        conninfo = make_conninfo(dsn, **params)
        parsed = conninfo_to_dict(conninfo)
    except psycopg.ProgrammingError:
        raise ConfigError("postgres: 'dsn' inválido (conteúdo omitido por segurança)") from None
    if "application_name" not in parsed:
        conninfo = make_conninfo(conninfo, application_name="dengcode")
    return conninfo


def parse_connect_timeout(cfg: Mapping[str, Any]) -> int:
    raw = cfg.get("connect_timeout_s", DEFAULT_CONNECT_TIMEOUT_S)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError("postgres: 'connect_timeout_s' deve ser um número inteiro") from None
    if value <= 0:
        raise ConfigError("postgres: 'connect_timeout_s' deve ser maior que zero")
    return value


def column_specs(description: Sequence[psycopg.Column]) -> list[ColumnSpec]:
    return [
        ColumnSpec(
            name=col.name,
            type_code=col.type_code,
            type_display=col.type_display,
            precision=col.precision,
            scale=col.scale,
        )
        for col in description
    ]


class PostgresQueryEngine:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="postgres", port=PortKind.QUERY_ENGINE, version="0.1.0"
    )
    name = "postgres"

    def __init__(
        self,
        conninfo: str,
        *,
        connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
        logger: logging.Logger | None = None,
    ) -> None:
        self._conninfo = conninfo
        self._connect_timeout_s = connect_timeout_s
        self._logger = logger or logging.getLogger("dengcode.adapter.postgres")
        self._running: dict[str, Connection] = {}

    def __repr__(self) -> str:  # nunca expõe o DSN (pode conter senha)
        return f"{type(self).__name__}(connect_timeout_s={self._connect_timeout_s})"

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> PostgresQueryEngine:
        return cls(
            build_conninfo(cfg),
            connect_timeout_s=parse_connect_timeout(cfg),
            logger=ctx.logger,
        )

    def capabilities(self) -> QueryEngineCapabilities:
        return QueryEngineCapabilities(
            sql_dialect="postgres",
            supports_dry_run=False,
            supports_explain=True,
            supports_cost_estimate=False,
            supports_transactions=True,
            supports_cancel=True,
        )

    @contextlib.asynccontextmanager
    async def connect(self, *, timeout_s: int | None = None) -> AsyncIterator[Connection]:
        """Conexão nova em autocommit; erros do psycopg saem como erros de domínio.

        ``timeout_s`` vira o ``statement_timeout`` da sessão.
        """
        try:
            conn = await psycopg.AsyncConnection.connect(
                self._conninfo, autocommit=True, connect_timeout=self._connect_timeout_s
            )
        except psycopg.Error as exc:
            raise map_error(exc) from exc
        try:
            if timeout_s is not None:
                ms = str(max(int(timeout_s), 1) * 1000)
                await conn.execute(_SET_TIMEOUT.format(pgsql.Literal(ms)))
            yield conn
        except psycopg.Error as exc:
            raise map_error(exc) from exc
        finally:
            with contextlib.suppress(psycopg.Error):
                await conn.close()

    async def fetch_all(
        self, query: Query, params: Sequence[Any] | None = None, *, timeout_s: int = 300
    ) -> tuple[list[str], list[Row]]:
        """Executa uma consulta curta (metadados) e devolve nomes + todas as linhas."""
        async with self.connect(timeout_s=timeout_s) as conn, conn.cursor() as cur:
            await cur.execute(query, params)
            names = [c.name for c in cur.description or []]
            rows = await cur.fetchall() if cur.description is not None else []
            return names, rows

    async def _run(
        self, query: Query, *, max_rows: int, timeout_s: int, query_id: str
    ) -> tuple[list[ColumnSpec], list[Row], bool]:
        async with self.connect(timeout_s=timeout_s) as conn:
            self._running[query_id] = conn
            try:
                async with conn.cursor() as cur:
                    await cur.execute(query)
                    while cur.nextset():  # várias instruções: vale a última
                        pass
                    if cur.description is None:  # DDL/DML sem resultado
                        return [], [], False
                    specs = column_specs(cur.description)
                    limit = max(max_rows, 0)
                    rows = await cur.fetchmany(limit + 1)
                    return specs, rows[:limit], len(rows) > limit
            finally:
                self._running.pop(query_id, None)

    async def execute(
        self, sql: str, *, max_rows: int = 10_000, timeout_s: int = 300
    ) -> QueryResult:
        query_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        # o SQL é arbitrário por definição: já passou pelo SqlGuard antes daqui
        query = cast(LiteralString, sql)
        try:
            async with asyncio.timeout(max(timeout_s, 1) + _CLIENT_GRACE_S):
                specs, rows, truncated = await self._run(
                    query, max_rows=max_rows, timeout_s=timeout_s, query_id=query_id
                )
        except TimeoutError as exc:
            raise TransientError(f"tempo esgotado após {timeout_s}s") from exc
        table = rows_to_table(specs, rows)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._logger.debug(
            "postgres: consulta %s concluída em %d ms (%d linhas)",
            query_id,
            elapsed_ms,
            table.num_rows,
        )
        return QueryResult(
            query_id=query_id,
            columns=[ColumnInfo(name=s.name, data_type=s.type_display) for s in specs],
            preview=table,
            total_rows=None if truncated else table.num_rows,
            elapsed_ms=elapsed_ms,
        )

    async def explain(self, sql: str) -> str:
        query = cast(LiteralString, f"EXPLAIN {sql}")
        _, rows = await self.fetch_all(query)
        return "\n".join(str(r[0]) for r in rows)

    async def dry_run(self, sql: str) -> CostEstimate:
        raise UnsupportedOperation("PostgreSQL não tem dry-run com estimativa; use explain")

    async def cancel(self, query_id: str) -> None:
        conn = self._running.get(query_id)
        if conn is None:
            return
        try:
            await conn.cancel_safe(timeout=_CANCEL_TIMEOUT_S)
        except psycopg.Error as exc:
            raise map_error(exc) from exc
