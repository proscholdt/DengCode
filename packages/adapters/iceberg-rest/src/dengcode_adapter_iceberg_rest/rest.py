"""Cliente HTTP mínimo da especificação Iceberg REST Catalog (só leitura).

Por que não o ``pyiceberg``: o adapter só precisa de três rotas de leitura
(``/v1/config``, namespaces, tabelas) e do JSON de metadados da tabela; o
``pyiceberg`` traria leitores de arquivos, FileIO e dependências nativas que o
catálogo nunca usa. Com o ``httpx2`` (já no workspace) basta seguir a spec.

Cada operação abre o próprio ``AsyncClient`` e o fecha no fim (nada preso a um
event loop antigo). ``token`` e ``credential`` nunca vão para log, ``repr`` ou
mensagem de erro.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx2

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    DengCodeError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    TransientError,
)

JsonDict = dict[str, Any]
TransportFactory = Callable[[], httpx2.AsyncBaseTransport | None]

# separador de níveis de namespace na URL (spec: 0x1F, "unit separator")
DEFAULT_NAMESPACE_SEPARATOR = "\x1f"
DEFAULT_OAUTH_SCOPE = "catalog"
_MAX_PAGES = 1000


@dataclass(frozen=True)
class RestSettings:
    """Como falar com o catálogo. Segredos ficam fora do ``repr``."""

    url: str  # ex.: "http://localhost:8181" (sem o "/v1")
    warehouse: str | None = None
    prefix: str | None = None
    token: str | None = field(default=None, repr=False)
    credential: str | None = field(default=None, repr=False)  # "client_id:client_secret"
    oauth2_server_uri: str | None = None
    scope: str = DEFAULT_OAUTH_SCOPE
    timeout_s: float = 30.0


def _error_message(body: str) -> str:
    """Mensagem e tipo do ``ErrorModel`` da spec (sem o stack trace do servidor)."""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return " ".join(body.split())[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "").strip()
        kind = str(error.get("type") or "").strip()
        return f"{message} ({kind})" if kind else message
    return " ".join(body.split())[:300]


def map_status(status: int, body: str, *, retry_after: str | None = None) -> DengCodeError:
    """Classifica uma resposta de erro do catálogo num erro de domínio."""
    detail = _error_message(body)
    text = f"iceberg-rest: {detail} (HTTP {status})" if detail else f"iceberg-rest: HTTP {status}"
    if status == 404:
        return NotFoundError(text)
    if status in (401, 419):  # 419 = credencial expirada (spec)
        return AuthError(text)
    if status == 403:
        return PermissionDeniedError(text)
    if status == 429:
        seconds: float | None = None
        with contextlib.suppress(TypeError, ValueError):
            seconds = float(retry_after) if retry_after is not None else None
        return RateLimitError(text, retry_after_s=seconds)
    if status >= 500:
        return TransientError(text)
    return AdapterError(text)


def map_transport_error(exc: httpx2.HTTPError) -> DengCodeError:
    if isinstance(exc, httpx2.TimeoutException):
        return TransientError(f"iceberg-rest: tempo esgotado na comunicação ({exc})")
    if isinstance(exc, httpx2.TransportError):
        return TransientError(f"iceberg-rest: falha de conexão com o catálogo ({exc})")
    return AdapterError(f"iceberg-rest: falha HTTP ({exc})")


def encode_namespace(parts: Sequence[str], separator: str = DEFAULT_NAMESPACE_SEPARATOR) -> str:
    """Namespace multinível para a URL: níveis unidos pelo separador, tudo escapado."""
    return quote(separator.join(parts), safe="")


class IcebergRestClient:
    """Leituras da spec com o ``prefix`` e o separador vindos de ``/v1/config``."""

    def __init__(
        self, settings: RestSettings, *, transport_factory: TransportFactory | None = None
    ) -> None:
        self.settings = settings
        self._transport_factory = transport_factory
        self._prefix: str | None = settings.prefix
        self._separator = DEFAULT_NAMESPACE_SEPARATOR
        self._configured = False
        self._oauth_token: str | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(url={self.settings.url!r})"

    @property
    def separator(self) -> str:
        return self._separator

    @contextlib.asynccontextmanager
    async def _http(self) -> AsyncIterator[httpx2.AsyncClient]:
        transport = self._transport_factory() if self._transport_factory else None
        client = httpx2.AsyncClient(
            base_url=self.settings.url.rstrip("/"),
            timeout=self.settings.timeout_s,
            transport=transport,
            headers={"Accept": "application/json"},
        )
        try:
            yield client
        finally:
            await client.aclose()

    async def _send(
        self, client: httpx2.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx2.Response:
        try:
            return await client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise map_transport_error(exc) from exc

    @staticmethod
    def _json(resp: httpx2.Response) -> JsonDict:
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AdapterError(f"iceberg-rest: resposta inválida do catálogo: {exc}") from exc
        if not isinstance(data, dict):
            raise AdapterError("iceberg-rest: resposta inesperada do catálogo (não é objeto)")
        return data

    # ------------------------------------------------------------ autenticação
    async def _fetch_token(self, client: httpx2.AsyncClient) -> str:
        """OAuth2 client credentials (``credential`` = ``client_id:client_secret``)."""
        credential = self.settings.credential or ""
        client_id, sep, secret = credential.partition(":")
        if not sep:
            client_id, secret = "", credential
        form = {"grant_type": "client_credentials", "client_secret": secret}
        if client_id:
            form["client_id"] = client_id
        if self.settings.scope:
            form["scope"] = self.settings.scope
        url = self.settings.oauth2_server_uri or "/v1/oauth/tokens"
        resp = await self._send(client, "POST", url, data=form)
        if resp.status_code != 200:
            raise map_status(resp.status_code, resp.text)
        token = self._json(resp).get("access_token")
        if not token:
            raise AuthError("iceberg-rest: o servidor OAuth2 não devolveu access_token")
        return str(token)

    async def _auth_headers(self, client: httpx2.AsyncClient, *, refresh: bool) -> dict[str, str]:
        if self.settings.token:
            return {"Authorization": f"Bearer {self.settings.token}"}
        if self.settings.credential:
            if refresh or self._oauth_token is None:
                self._oauth_token = await self._fetch_token(client)
            return {"Authorization": f"Bearer {self._oauth_token}"}
        return {}

    async def _get(
        self, client: httpx2.AsyncClient, path: str, params: Mapping[str, str] | None = None
    ) -> JsonDict:
        headers = await self._auth_headers(client, refresh=False)
        resp = await self._send(client, "GET", path, params=params, headers=headers)
        if resp.status_code == 401 and self.settings.credential and not self.settings.token:
            headers = await self._auth_headers(client, refresh=True)  # token expirado
            resp = await self._send(client, "GET", path, params=params, headers=headers)
        if resp.status_code != 200:
            raise map_status(
                resp.status_code, resp.text, retry_after=resp.headers.get("Retry-After")
            )
        return self._json(resp)

    async def _configure(self, client: httpx2.AsyncClient) -> None:
        if self._configured:
            return
        params = {"warehouse": self.settings.warehouse} if self.settings.warehouse else None
        data = await self._get(client, "/v1/config", params)
        merged: dict[str, Any] = {**(data.get("defaults") or {}), **(data.get("overrides") or {})}
        if self._prefix is None and merged.get("prefix"):
            self._prefix = str(merged["prefix"])
        if merged.get("namespace-separator"):
            self._separator = str(merged["namespace-separator"])
        self._configured = True

    def _path(self, *segments: str) -> str:
        base = "/v1"
        if self._prefix:
            base += "/" + quote(self._prefix.strip("/"), safe="/")
        return "/".join([base, *segments])

    # ------------------------------------------------------------ leituras
    async def _paged(
        self, client: httpx2.AsyncClient, path: str, key: str, params: Mapping[str, str]
    ) -> list[Any]:
        items: list[Any] = []
        query = dict(params)
        for _ in range(_MAX_PAGES):
            data = await self._get(client, path, query)
            items.extend(data.get(key) or [])
            token = data.get("next-page-token")
            if not token:
                return items
            query["pageToken"] = str(token)
        return items

    async def list_namespaces(self) -> list[list[str]]:
        """Todos os namespaces, inclusive aninhados (busca em largura por ``parent``)."""
        async with self._http() as client:
            await self._configure(client)
            path = self._path("namespaces")
            found: list[list[str]] = []
            seen: set[tuple[str, ...]] = set()
            pending: list[list[str] | None] = [None]
            while pending:
                parent = pending.pop(0)
                params = {} if parent is None else {"parent": self._separator.join(parent)}
                for raw in await self._paged(client, path, "namespaces", params):
                    parts = [str(p) for p in raw]
                    key = tuple(parts)
                    # servidores sem suporte a "parent" devolvem tudo de novo: ignora repetidos
                    if not parts or key in seen:
                        continue
                    if parent is not None and parts[: len(parent)] != parent:
                        continue
                    seen.add(key)
                    found.append(parts)
                    pending.append(parts)
            return found

    async def list_tables(self, namespace: Sequence[str]) -> list[JsonDict]:
        async with self._http() as client:
            await self._configure(client)
            ns = encode_namespace(namespace, self._separator)
            path = self._path("namespaces", ns, "tables")
            return [i for i in await self._paged(client, path, "identifiers", {}) if i]

    async def load_table(self, namespace: Sequence[str], table: str) -> JsonDict:
        async with self._http() as client:
            await self._configure(client)
            ns = encode_namespace(namespace, self._separator)
            return await self._get(
                client, self._path("namespaces", ns, "tables", quote(table, safe=""))
            )
