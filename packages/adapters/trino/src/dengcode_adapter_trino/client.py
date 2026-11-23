"""Cliente assíncrono mínimo do protocolo HTTP do Trino (``/v1/statement``).

Por que não o cliente oficial ``trino`` (DBAPI): ele é síncrono (``requests``)
e exigiria uma thread por consulta; cancelar a tarefa asyncio não interromperia
a thread. O protocolo é pequeno e estável — ``POST /v1/statement`` com o SQL,
depois ``GET nextUri`` até acabar, ``DELETE nextUri`` para cancelar — e com o
``httpx2`` (já usado no workspace) tudo é nativo do asyncio: o cancelamento da
tarefa vira um ``DELETE`` no servidor, sem threads nem dependências novas.

Cada operação abre o próprio ``AsyncClient`` e o fecha no fim: nada de pool
preso a um event loop antigo.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx2

from dengcode_adapter_trino.arrow import TrinoColumn
from dengcode_adapter_trino.errors import (
    is_retryable_status,
    map_query_error,
    map_status,
    map_transport_error,
)
from dengcode_core.errors import AdapterError, TransientError

JsonDict = dict[str, Any]

SOURCE = "dengcode"
# o cliente entende tipos de data/hora com precisão (timestamp(6) etc.)
CLIENT_CAPABILITIES = "PARAMETRIC_DATETIME"
_MAX_RETRIES = 5
_RETRY_DELAY_S = 0.5
_ABORT_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class TrinoSettings:
    """Como falar com o coordenador. A senha nunca aparece no ``repr``."""

    base_url: str  # ex.: "http://localhost:8080"
    user: str = SOURCE
    catalog: str | None = None
    schema: str | None = None
    password: str | None = field(default=None, repr=False)
    request_timeout_s: float = 60.0


@dataclass
class StatementResult:
    """O que uma execução devolve (linhas ainda em JSON cru)."""

    query_id: str | None
    columns: list[TrinoColumn]
    rows: list[list[Any]]
    truncated: bool
    update_type: str | None = None
    stats: JsonDict = field(default_factory=lambda: JsonDict())


TransportFactory = Callable[[], httpx2.AsyncBaseTransport | None]


class TrinoClient:
    """Executa instruções seguindo ``nextUri``; erros saem como erros de domínio."""

    def __init__(
        self,
        settings: TrinoSettings,
        *,
        transport_factory: TransportFactory | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self._transport_factory = transport_factory
        self._logger = logger or logging.getLogger("dengcode.adapter.trino")
        # query_id → nextUri atual (para cancelar) e ids cancelados a pedido
        self._running: dict[str, str] = {}
        self._canceled: set[str] = set()

    def __repr__(self) -> str:  # a senha fica de fora (dataclass com repr=False)
        return f"{type(self).__name__}({self.settings!r})"

    @property
    def running(self) -> Mapping[str, str]:
        return self._running

    # ------------------------------------------------------------ HTTP
    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx2.AsyncClient]:
        auth = None
        if self.settings.password is not None:
            auth = httpx2.BasicAuth(self.settings.user, self.settings.password)
        transport = self._transport_factory() if self._transport_factory else None
        client = httpx2.AsyncClient(
            base_url=self.settings.base_url,
            auth=auth,
            timeout=self.settings.request_timeout_s,
            transport=transport,
            headers={"X-Trino-User": self.settings.user, "X-Trino-Source": SOURCE},
        )
        try:
            yield client
        finally:
            await client.aclose()

    def statement_headers(
        self, session: Mapping[str, str] | None = None, *, catalog: str | None = None
    ) -> dict[str, str]:
        headers = {
            "X-Trino-Client-Capabilities": CLIENT_CAPABILITIES,
            "Content-Type": "text/plain; charset=utf-8",
        }
        target_catalog = catalog or self.settings.catalog
        if target_catalog:
            headers["X-Trino-Catalog"] = target_catalog
            if self.settings.schema and (catalog is None or catalog == self.settings.catalog):
                headers["X-Trino-Schema"] = self.settings.schema
        if session:
            headers["X-Trino-Session"] = ",".join(
                f"{key}={quote(value, safe='')}" for key, value in session.items()
            )
        return headers

    async def _send(
        self,
        client: httpx2.AsyncClient,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonDict:
        """Uma requisição com novas tentativas em 502/503/504 (pedido do protocolo)."""
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.request(method, url, content=content, headers=headers)
            except httpx2.HTTPError as exc:
                raise map_transport_error(exc) from exc
            if resp.status_code == 200:
                try:
                    data: JsonDict = resp.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise AdapterError(f"trino: resposta inválida do servidor: {exc}") from exc
                return data
            if is_retryable_status(resp.status_code) and attempt < _MAX_RETRIES:
                await asyncio.sleep(_RETRY_DELAY_S * (attempt + 1))
                continue
            raise map_status(
                resp.status_code, resp.text, retry_after=resp.headers.get("Retry-After")
            )
        raise TransientError("trino: servidor indisponível")  # pragma: no cover

    async def _abort(self, next_uri: str) -> None:
        """``DELETE nextUri``: cancela no servidor. Melhor esforço, nunca levanta."""
        with contextlib.suppress(Exception):
            async with asyncio.timeout(_ABORT_TIMEOUT_S), self._http() as client:
                await client.delete(next_uri)

    # ------------------------------------------------------------ execução
    async def execute(
        self,
        sql: str,
        *,
        max_rows: int | None = None,
        session: Mapping[str, str] | None = None,
        catalog: str | None = None,
    ) -> StatementResult:
        """Executa ``sql``; com ``max_rows``, para de ler (e cancela) ao passar do limite."""
        limit = None if max_rows is None else max(max_rows, 0)
        columns: list[TrinoColumn] = []
        rows: list[list[Any]] = []
        query_id: str | None = None
        next_uri: str | None = None
        page: JsonDict = {}
        finished = False
        try:
            async with self._http() as client:
                page = await self._send(
                    client,
                    "POST",
                    "/v1/statement",
                    content=sql.encode("utf-8"),
                    headers=self.statement_headers(session, catalog=catalog),
                )
                query_id = str(page["id"]) if page.get("id") else None
                while True:
                    error = page.get("error")
                    if error:
                        raise map_query_error(error)
                    if not columns and page.get("columns"):
                        columns = [
                            TrinoColumn(name=str(c["name"]), type=str(c["type"]))
                            for c in page["columns"]
                        ]
                    rows.extend(page.get("data") or [])
                    next_uri = page.get("nextUri")
                    if limit is not None and len(rows) > limit:
                        finished = True  # já temos o bastante: cancela o resto
                        if next_uri:
                            await self._abort(next_uri)
                        return StatementResult(
                            query_id=query_id,
                            columns=columns,
                            rows=rows[:limit],
                            truncated=True,
                            update_type=page.get("updateType"),
                            stats=page.get("stats") or {},
                        )
                    if not next_uri:
                        finished = True
                        break
                    if query_id:
                        self._running[query_id] = next_uri
                    page = await self._send(client, "GET", next_uri)
                    if query_id in self._canceled:
                        raise TransientError(f"consulta cancelada: {query_id}")
        except AdapterError:
            if query_id in self._canceled:
                raise TransientError(f"consulta cancelada: {query_id}") from None
            raise
        finally:
            if query_id:
                self._running.pop(query_id, None)
                self._canceled.discard(query_id)
            if not finished and next_uri:
                # erro, timeout do cliente ou tarefa cancelada: não deixa a consulta órfã
                await self._abort(next_uri)
        return StatementResult(
            query_id=query_id,
            columns=columns,
            rows=rows,
            truncated=False,
            update_type=page.get("updateType"),
            stats=page.get("stats") or {},
        )

    async def cancel(self, query_id: str) -> None:
        """Cancela a consulta: pelo ``nextUri`` se for nossa, senão pela API de consultas."""
        next_uri = self._running.get(query_id)
        if next_uri is not None:
            self._canceled.add(query_id)
            await self._abort(next_uri)
            return
        async with self._http() as client:
            try:
                resp = await client.delete(f"/v1/query/{quote(query_id, safe='')}")
            except httpx2.HTTPError as exc:
                raise map_transport_error(exc) from exc
        if resp.status_code in (200, 204, 404, 410):
            return  # cancelada, ou já terminou/não existe
        raise map_status(resp.status_code, resp.text)
