"""Conversão de falhas do Trino em erros de domínio do DengCode.

Duas origens:

- **erro da consulta** (campo ``error`` da resposta JSON do protocolo): a
  classificação usa ``errorName`` (estável entre versões) e ``errorType``;
- **erro HTTP/transporte** (status 401/403/5xx, conexão recusada, timeout).

Nenhuma exceção do ``httpx2`` atravessa a porta.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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

_NOT_FOUND = frozenset(
    {
        "TABLE_NOT_FOUND",
        "SCHEMA_NOT_FOUND",
        "CATALOG_NOT_FOUND",
        "COLUMN_NOT_FOUND",
        "FUNCTION_NOT_FOUND",
        "MISSING_TABLE",
        "MISSING_SCHEMA_NAME",
        "MISSING_CATALOG_NAME",
        "NOT_FOUND",
    }
)
_PERMISSION = frozenset({"PERMISSION_DENIED"})
_CANCELED = frozenset({"USER_CANCELED", "ADMINISTRATIVELY_KILLED"})
_TIMEOUT = frozenset({"EXCEEDED_TIME_LIMIT", "EXCEEDED_CPU_LIMIT", "EXCEEDED_SCAN_LIMIT"})
_TRANSIENT = frozenset(
    {
        "ABANDONED_QUERY",
        "SERVER_STARTING_UP",
        "SERVER_SHUTTING_DOWN",
        "NO_NODES_AVAILABLE",
        "REMOTE_HOST_GONE",
        "TOO_MANY_REQUESTS_FAILED",
        "PAGE_TRANSPORT_TIMEOUT",
        "QUERY_QUEUE_FULL",
        "QUERY_EXPIRED",
    }
)
_RETRYABLE_STATUS = frozenset({502, 503, 504})


def map_query_error(error: Mapping[str, Any]) -> DengCodeError:
    """Classifica o objeto ``error`` de uma resposta do protocolo do Trino."""
    name = str(error.get("errorName") or "")
    kind = str(error.get("errorType") or "")
    message = str(error.get("message") or "erro sem mensagem").strip()
    text = f"trino: {message} ({name})" if name else f"trino: {message}"
    if name in _NOT_FOUND:
        return NotFoundError(text)
    if name in _PERMISSION:
        return PermissionDeniedError(text)
    if name in _CANCELED:
        return TransientError(f"consulta cancelada: {text}")
    if name in _TIMEOUT:
        return TransientError(f"tempo esgotado no servidor: {text}")
    if name in _TRANSIENT or kind == "INSUFFICIENT_RESOURCES":
        return TransientError(text)
    return AdapterError(text)


def _body_hint(body: str) -> str:
    snippet = " ".join(body.split())[:300]
    return f": {snippet}" if snippet else ""


def map_status(status: int, body: str, *, retry_after: str | None = None) -> DengCodeError:
    """Classifica uma resposta HTTP de erro (o corpo entra resumido na mensagem)."""
    hint = _body_hint(body)
    if status == 401:
        return AuthError(f"trino: autenticação recusada (HTTP 401){hint}")
    if status == 403:
        return PermissionDeniedError(f"trino: acesso negado (HTTP 403){hint}")
    if status == 429:
        return RateLimitError(
            f"trino: muitas requisições (HTTP 429){hint}", retry_after_s=_seconds(retry_after)
        )
    if status == 410:
        return TransientError(f"trino: a consulta expirou no servidor (HTTP 410){hint}")
    if status in _RETRYABLE_STATUS:
        return TransientError(f"trino: servidor indisponível (HTTP {status}){hint}")
    return AdapterError(f"trino: resposta inesperada (HTTP {status}){hint}")


def map_transport_error(exc: httpx2.HTTPError) -> DengCodeError:
    """Falhas de rede: conexão recusada, timeout, conexão perdida → ``TransientError``."""
    if isinstance(exc, httpx2.TimeoutException):
        return TransientError(f"trino: tempo esgotado na comunicação com o servidor ({exc})")
    if isinstance(exc, httpx2.TransportError):
        return TransientError(f"trino: falha de conexão com o servidor ({exc})")
    return AdapterError(f"trino: falha HTTP ({exc})")


def is_retryable_status(status: int) -> bool:
    return status in _RETRYABLE_STATUS


def _seconds(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
