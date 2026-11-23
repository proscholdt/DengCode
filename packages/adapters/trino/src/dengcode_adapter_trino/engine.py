"""QueryEnginePort sobre Trino (protocolo HTTP do cliente, via ``httpx2``).

Tempo limite em duas camadas: no servidor, a propriedade de sessão
``query_max_run_time`` = ``timeout_s``; no cliente, ``asyncio.timeout`` com
folga, que cancela a consulta no servidor (``DELETE nextUri``). ``max_rows``
para a leitura das páginas assim que o limite é passado e cancela o resto.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from dengcode_adapter_trino.arrow import rows_to_table
from dengcode_adapter_trino.client import (
    StatementResult,
    TransportFactory,
    TrinoClient,
    TrinoSettings,
)
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

DEFAULT_PORT = 8080
DEFAULT_USER = "dengcode"
DEFAULT_REQUEST_TIMEOUT_S = 60
CONNECTION_KEYS = ("host", "port", "http_scheme", "user", "password", "catalog", "schema")
# folga do timeout do cliente sobre o query_max_run_time do servidor
_CLIENT_GRACE_S = 3.0
KNOWLEDGE_PACK = "dengcode_adapter_trino:knowledge/trino.md"


def _text(cfg: Mapping[str, Any], key: str) -> str | None:
    value = cfg.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ConfigError(f"trino: '{key}' deve ser texto")
    return value


def _positive_int(cfg: Mapping[str, Any], key: str, default: int) -> int:
    raw = cfg.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"trino: '{key}' deve ser um número inteiro") from None
    if value <= 0:
        raise ConfigError(f"trino: '{key}' deve ser maior que zero")
    return value


def build_settings(cfg: Mapping[str, Any]) -> TrinoSettings:
    """Valida o bloco de config da stack. Mensagens de erro nunca mostram a senha."""
    host = _text(cfg, "host") or "localhost"
    if "://" in host or "/" in host:
        raise ConfigError("trino: 'host' é só o nome/IP; use 'http_scheme' e 'port'")
    port = _positive_int(cfg, "port", DEFAULT_PORT)
    if port > 65535:
        raise ConfigError("trino: 'port' fora do intervalo 1–65535")
    scheme = (_text(cfg, "http_scheme") or "http").lower()
    if scheme not in ("http", "https"):
        raise ConfigError("trino: 'http_scheme' deve ser 'http' ou 'https'")
    password = cfg.get("password")
    if password is not None and not isinstance(password, str):
        raise ConfigError("trino: 'password' deve ser texto (valor omitido por segurança)")
    return TrinoSettings(
        base_url=f"{scheme}://{host}:{port}",
        user=_text(cfg, "user") or DEFAULT_USER,
        catalog=_text(cfg, "catalog"),
        schema=_text(cfg, "schema"),
        password=password or None,
        request_timeout_s=float(_positive_int(cfg, "timeout_s", DEFAULT_REQUEST_TIMEOUT_S)),
    )


def _cost(stats: Mapping[str, Any]) -> CostEstimate | None:
    scanned = stats.get("physicalInputBytes", stats.get("processedBytes"))
    if scanned is None:
        return None
    return CostEstimate(bytes_scanned=int(scanned), confidence="exact")


class TrinoQueryEngine:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="trino",
        port=PortKind.QUERY_ENGINE,
        version="0.1.0",
        knowledge_pack=KNOWLEDGE_PACK,
    )
    name = "trino"

    def __init__(self, client: TrinoClient, *, logger: logging.Logger | None = None) -> None:
        self._client = client
        self._logger = logger or logging.getLogger("dengcode.adapter.trino")

    def __repr__(self) -> str:  # nunca expõe a senha
        return f"{type(self).__name__}({self._client.settings.base_url!r})"

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        ctx: AdapterContext,
        *,
        transport_factory: TransportFactory | None = None,
    ) -> TrinoQueryEngine:
        client = TrinoClient(
            build_settings(cfg), transport_factory=transport_factory, logger=ctx.logger
        )
        return cls(client, logger=ctx.logger)

    @property
    def client(self) -> TrinoClient:
        return self._client

    @property
    def settings(self) -> TrinoSettings:
        return self._client.settings

    def capabilities(self) -> QueryEngineCapabilities:
        return QueryEngineCapabilities(
            sql_dialect="trino",
            supports_dry_run=False,
            supports_explain=True,
            supports_cost_estimate=False,
            supports_transactions=False,
            supports_cancel=True,
        )

    async def run(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
        timeout_s: int = 300,
        catalog: str | None = None,
    ) -> StatementResult:
        """Execução crua (linhas JSON), com os dois tempos limite aplicados."""
        seconds = max(int(timeout_s), 1)
        session = {"query_max_run_time": f"{seconds}s"}
        try:
            async with asyncio.timeout(seconds + _CLIENT_GRACE_S):
                return await self._client.execute(
                    sql, max_rows=max_rows, session=session, catalog=catalog
                )
        except TimeoutError as exc:
            raise TransientError(f"trino: tempo esgotado após {seconds}s") from exc

    async def execute(
        self, sql: str, *, max_rows: int = 10_000, timeout_s: int = 300
    ) -> QueryResult:
        started = time.perf_counter()
        # o SQL é arbitrário por definição: já passou pelo SqlGuard antes daqui
        result = await self.run(sql, max_rows=max_rows, timeout_s=timeout_s)
        table = rows_to_table(result.columns, result.rows)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self._logger.debug(
            "trino: consulta %s concluída em %d ms (%d linhas)",
            result.query_id,
            elapsed_ms,
            table.num_rows,
        )
        return QueryResult(
            query_id=result.query_id,
            columns=[ColumnInfo(name=c.name, data_type=c.type) for c in result.columns],
            preview=table,
            total_rows=None if result.truncated else table.num_rows,
            elapsed_ms=elapsed_ms,
            cost=_cost(result.stats),
        )

    async def explain(self, sql: str) -> str:
        """``EXPLAIN`` (plano distribuído, sem executar)."""
        result = await self.run(f"EXPLAIN {sql}", timeout_s=60)
        return "\n".join(str(row[0]) for row in result.rows if row)

    async def dry_run(self, sql: str) -> CostEstimate:
        raise UnsupportedOperation(
            "Trino não tem dry-run com estimativa de custo; use explain "
            "(ou EXPLAIN (TYPE IO) para ver as tabelas e partições lidas)"
        )

    async def cancel(self, query_id: str) -> None:
        await self._client.cancel(query_id)
