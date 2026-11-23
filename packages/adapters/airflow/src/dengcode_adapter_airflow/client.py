"""Cliente HTTP da API REST estável do Airflow.

- Airflow 3: API em ``/api/v2``; autenticação por token JWT obtido em
  ``POST /auth/token`` com usuário e senha (renovado uma vez em caso de 401).
- Airflow 2: API em ``/api/v1``; autenticação HTTP básica.

A versão é configurada (``api_version: 2|3``) ou detectada sondando
``/api/v2/version`` e depois ``/api/v1/version``.

Ponto de extensão das variantes de implantação: ``AUTH_VARIANTS`` mapeia o nome
da variante (``local``, ``basic``...) para a fábrica da estratégia de
autenticação. ``mwaa`` e ``composer`` (fase 7) entram aqui como novas
estratégias (token da AWS / credencial do Google), sem mudar o restante.

Erros HTTP viram erros de domínio (``dengcode_core.errors``); só os GETs
(idempotentes) são repetidos em erros temporários.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    DengCodeError,
    NotFoundError,
    RateLimitError,
    TransientError,
)
from dengcode_core.llm.retry import RetryPolicy, call_with_retry, parse_retry_after

log = logging.getLogger(__name__)

TOKEN_PATH = "/auth/token"
_DETAIL_MAX = 300


@dataclass(frozen=True)
class ApiFlavor:
    """O que muda entre as versões da API estável."""

    airflow_major: int
    prefix: str
    run_order_by: str  # ordenação "mais recente primeiro" de dagRuns
    trigger_needs_logical_date: bool  # o Airflow 3 exige o campo (pode ser nulo)
    json_logs: bool  # logs estruturados em JSON (v2) ou texto puro (v1)


FLAVORS: dict[int, ApiFlavor] = {
    3: ApiFlavor(3, "/api/v2", "-run_after", trigger_needs_logical_date=True, json_logs=True),
    2: ApiFlavor(
        2, "/api/v1", "-execution_date", trigger_needs_logical_date=False, json_logs=False
    ),
}


@dataclass(frozen=True)
class Credentials:
    username: str | None
    password: str | None


# --------------------------------------------------------------- autenticação
class AuthStrategy(Protocol):
    """Monta os cabeçalhos de autenticação de cada requisição."""

    refreshable: bool

    async def headers(self, client: httpx2.AsyncClient, *, refresh: bool = False) -> dict[str, str]:
        """Cabeçalhos a enviar; ``refresh=True`` descarta credenciais em cache."""
        ...


class NoAuth:
    """Sem autenticação (ex.: Airflow 3 com ``simple_auth_manager_all_admins``)."""

    refreshable = False

    async def headers(self, client: httpx2.AsyncClient, *, refresh: bool = False) -> dict[str, str]:
        return {}


class BasicAuth:
    """HTTP básica (Airflow 2 com ``auth_backends = basic_auth`` ou proxy na frente)."""

    refreshable = False

    def __init__(self, username: str, password: str) -> None:
        raw = f"{username}:{password}".encode()
        self._value = "Basic " + base64.b64encode(raw).decode("ascii")

    def __repr__(self) -> str:
        return "BasicAuth(***)"

    async def headers(self, client: httpx2.AsyncClient, *, refresh: bool = False) -> dict[str, str]:
        return {"Authorization": self._value}


class JwtAuth:
    """Token JWT do Airflow 3 (``POST /auth/token``), guardado em memória."""

    refreshable = True

    def __init__(self, username: str, password: str, *, token_path: str = TOKEN_PATH) -> None:
        self._username = username
        self._password = password
        self._token_path = token_path
        self._token: str | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"JwtAuth(username={self._username!r})"

    async def headers(self, client: httpx2.AsyncClient, *, refresh: bool = False) -> dict[str, str]:
        async with self._lock:
            if refresh:
                self._token = None
            if self._token is None:
                self._token = await self._fetch(client)
        return {"Authorization": f"Bearer {self._token}"}

    async def _fetch(self, client: httpx2.AsyncClient) -> str:
        body = {"username": self._username, "password": self._password}
        resp = await _send_raw(client, "POST", self._token_path, json_body=body)
        if resp.status_code in (400, 401, 403, 422):
            raise AuthError(
                f"airflow {resp.status_code}: usuário ou senha recusados em {self._token_path}"
            )
        if resp.status_code == 404:
            raise AuthError(
                f"airflow 404: {self._token_path} não existe; o auth manager não emite token JWT "
                "(use variant: basic ou confira a versão do Airflow)"
            )
        if resp.status_code >= 400:
            raise status_error(resp, what="token de acesso")
        token = _json_object(resp).get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError("airflow: resposta de /auth/token sem access_token")
        return token


AuthFactory = Callable[[ApiFlavor, Credentials], AuthStrategy]


def _local_auth(flavor: ApiFlavor, cred: Credentials) -> AuthStrategy:
    """Airflow instalado localmente/autogerenciado: JWT no 3, básica no 2."""
    if cred.username is None or cred.password is None:
        return NoAuth()
    if flavor.airflow_major >= 3:
        return JwtAuth(cred.username, cred.password)
    return BasicAuth(cred.username, cred.password)


def _basic_auth(flavor: ApiFlavor, cred: Credentials) -> AuthStrategy:
    if cred.username is None or cred.password is None:
        raise ConfigError("airflow: variant 'basic' exige username e password")
    return BasicAuth(cred.username, cred.password)


AUTH_VARIANTS: dict[str, AuthFactory] = {"local": _local_auth, "basic": _basic_auth}
"""Variantes suportadas. ``mwaa``/``composer`` entram aqui na fase 7."""

PLANNED_VARIANTS = frozenset({"mwaa", "composer"})


# ------------------------------------------------------------------- cliente
class AirflowApi:
    """Requisições à API estável com versão, autenticação, retry e erros de domínio."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        credentials: Credentials,
        variant: str = "local",
        api_version: int | None = None,
        retry: RetryPolicy | None = None,
    ) -> None:
        if variant not in AUTH_VARIANTS:
            if variant in PLANNED_VARIANTS:
                raise ConfigError(f"airflow: variant '{variant}' ainda não suportada (fase 7)")
            raise ConfigError(
                f"airflow: variant '{variant}' desconhecida; use uma de {sorted(AUTH_VARIANTS)}"
            )
        if api_version is not None and api_version not in FLAVORS:
            raise ConfigError(f"airflow: api_version deve ser 2 ou 3 (recebido {api_version!r})")
        if variant == "basic" and (credentials.username is None or credentials.password is None):
            raise ConfigError("airflow: variant 'basic' exige username e password")
        self._client = client
        self._credentials = credentials
        self._variant = variant
        self._retry = retry or RetryPolicy(max_attempts=3)
        self._flavor: ApiFlavor | None = FLAVORS[api_version] if api_version else None
        self._auth: AuthStrategy | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"AirflowApi(url={str(self._client.base_url)!r}, variant={self._variant!r})"

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def flavor(self) -> ApiFlavor:
        """Versão da API em uso (detectada na primeira chamada, se não configurada)."""
        async with self._lock:
            if self._flavor is None:
                self._flavor = await call_with_retry(self._detect, self._retry)
            if self._auth is None:
                self._auth = AUTH_VARIANTS[self._variant](self._flavor, self._credentials)
            return self._flavor

    async def _detect(self) -> ApiFlavor:
        for major in (3, 2):
            flavor = FLAVORS[major]
            resp = await _send_raw(self._client, "GET", f"{flavor.prefix}/version")
            if resp.status_code == 429 or resp.status_code >= 500:
                raise status_error(resp, what="versão do Airflow")
            if resp.status_code in (401, 403):  # existe, mas exige autenticação
                log.debug("airflow detectado", extra={"api": flavor.prefix})
                return flavor
            if resp.status_code == 200:
                try:
                    version = _json_object(resp).get("version")
                except AdapterError:  # HTML da UI (ex.: url com prefixo errado)
                    continue
                log.debug("airflow detectado", extra={"api": flavor.prefix, "version": version})
                return flavor
        raise AdapterError(
            f"airflow: {self.base_url} não responde /api/v2/version nem /api/v1/version; "
            "confira a url (é o endereço da UI/API do Airflow?)"
        )

    async def get_json(
        self, path: str, *, params: Mapping[str, Any] | None = None, what: str
    ) -> dict[str, Any]:
        """GET idempotente (com retry) que devolve um objeto JSON."""

        async def once() -> dict[str, Any]:
            return _json_object(await self._request("GET", path, params=params, what=what))

        return await call_with_retry(once, self._retry)

    async def get_text(
        self, path: str, *, params: Mapping[str, Any] | None = None, what: str
    ) -> str:
        """GET idempotente (com retry) que devolve texto puro."""

        async def once() -> str:
            resp = await self._request("GET", path, params=params, what=what, accept="text/plain")
            return resp.text

        return await call_with_retry(once, self._retry)

    async def post_json(self, path: str, body: Mapping[str, Any], *, what: str) -> dict[str, Any]:
        """POST sem retry (não idempotente: repetir poderia criar dois runs)."""
        return _json_object(await self._request("POST", path, json_body=body, what=what))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        what: str,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> httpx2.Response:
        await self.flavor()
        auth = self._auth
        assert auth is not None
        headers = {"Accept": accept, **await auth.headers(self._client)}
        resp = await _send_raw(
            self._client, method, path, params=params, json_body=json_body, headers=headers
        )
        if resp.status_code == 401 and auth.refreshable:
            # token expirado: renova uma vez (a requisição foi recusada, não executada)
            headers.update(await auth.headers(self._client, refresh=True))
            resp = await _send_raw(
                self._client, method, path, params=params, json_body=json_body, headers=headers
            )
        if resp.status_code >= 400:
            raise status_error(resp, what=what)
        return resp


async def _send_raw(
    client: httpx2.AsyncClient,
    method: str,
    path: str,
    *,
    params: Mapping[str, Any] | None = None,
    json_body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> httpx2.Response:
    """Uma requisição; falhas de rede viram ``TransientError``/``AdapterError``."""
    try:
        return await client.request(
            method, path, params=dict(params or {}), json=json_body, headers=dict(headers or {})
        )
    except httpx2.TimeoutException as exc:
        raise TransientError(f"airflow: tempo esgotado em {method} {path}") from exc
    except httpx2.ConnectError as exc:
        raise TransientError(
            f"airflow: não foi possível conectar em {client.base_url}; ele está rodando?"
        ) from exc
    except httpx2.TransportError as exc:
        raise TransientError(f"airflow: falha de rede em {method} {path}: {exc}") from exc
    except httpx2.HTTPError as exc:
        raise AdapterError(f"airflow: erro HTTP em {method} {path}: {exc}") from exc


def status_error(resp: httpx2.Response, *, what: str) -> DengCodeError:
    """Converte uma resposta HTTP de erro no erro de domínio correspondente."""
    status = resp.status_code
    detail = _detail(resp)
    if status in (401, 403):
        return AuthError(f"airflow {status}: acesso negado a {what} ({detail})")
    if status == 404:
        return NotFoundError(f"airflow 404: {what} não encontrado ({detail})")
    retry_after = parse_retry_after(resp.headers.get("retry-after"))
    if status == 429:
        return RateLimitError(
            f"airflow 429: limite de requisições ({detail})", retry_after_s=retry_after
        )
    if status >= 500:
        return TransientError(f"airflow {status}: {detail}", retry_after_s=retry_after)
    return AdapterError(f"airflow {status} em {what}: {detail}")


def _detail(resp: httpx2.Response) -> str:
    text = resp.text
    try:
        body: Any = json.loads(text)
    except ValueError:
        return text[:_DETAIL_MAX] or resp.reason_phrase
    if isinstance(body, dict):
        detail: Any = body.get("detail") or body.get("title") or body
        if not isinstance(detail, str):
            detail = json.dumps(detail, ensure_ascii=False, default=str)
        return detail[:_DETAIL_MAX]
    return text[:_DETAIL_MAX]


def _json_object(resp: httpx2.Response) -> dict[str, Any]:
    try:
        body: Any = resp.json()
    except ValueError as exc:
        raise AdapterError(f"airflow: resposta não é JSON ({resp.text[:_DETAIL_MAX]!r})") from exc
    if not isinstance(body, dict):
        raise AdapterError("airflow: resposta JSON inesperada (esperava um objeto)")
    return body
