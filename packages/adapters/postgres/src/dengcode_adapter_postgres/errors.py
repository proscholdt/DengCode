"""Conversão de exceções do psycopg em erros de domínio do DengCode.

Nenhuma exceção crua do psycopg atravessa a porta: tudo passa por
:func:`map_error`. A classificação usa o SQLSTATE (estável entre versões do
PostgreSQL) e, para falhas de conexão sem SQLSTATE, o texto da mensagem.
"""

from __future__ import annotations

import psycopg

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    DengCodeError,
    NotFoundError,
    PermissionDeniedError,
    TransientError,
)

# tabela, coluna, schema ou banco inexistente
_NOT_FOUND = frozenset({"42P01", "42703", "3F000", "3D000"})
# senha inválida / especificação de autorização inválida
_AUTH = frozenset({"28P01", "28000"})
_PERMISSION = frozenset({"42501"})
# serialização, deadlock, lock indisponível, servidor desligando
_TRANSIENT = frozenset({"40001", "40P01", "55P03", "57P01", "57P02", "57P03"})
# classes inteiras: 08 = conexão, 53 = recursos insuficientes
_TRANSIENT_CLASSES = ("08", "53")

_AUTH_HINTS = (
    "password authentication failed",
    "authentication failed",
    "no password supplied",
    "password is required",
)

_PROACTOR_HINT = (
    "o psycopg assíncrono não funciona com o ProactorEventLoop (padrão do asyncio no "
    "Windows); rode com asyncio.run(..., loop_factory=asyncio.SelectorEventLoop)"
)


def _message(exc: psycopg.Error, code: str | None) -> str:
    msg = str(exc).strip() or type(exc).__name__
    return f"{msg} (SQLSTATE {code})" if code else msg


def map_error(exc: psycopg.Error) -> DengCodeError:
    """Classifica uma exceção do psycopg num erro de domínio."""
    code = exc.sqlstate or None
    msg = _message(exc, code)
    if isinstance(exc, psycopg.InterfaceError) and "ProactorEventLoop" in str(exc):
        return AdapterError(_PROACTOR_HINT)
    if code in _NOT_FOUND:
        return NotFoundError(msg)
    if code in _AUTH:
        return AuthError(msg)
    if code in _PERMISSION:
        return PermissionDeniedError(msg)
    if code == "57014":
        return TransientError(f"consulta cancelada (tempo esgotado ou cancelamento): {msg}")
    if code in _TRANSIENT or (code is not None and code.startswith(_TRANSIENT_CLASSES)):
        return TransientError(msg)
    if isinstance(exc, psycopg.errors.CancellationTimeout):
        return TransientError(f"o cancelamento não respondeu a tempo: {msg}")
    if isinstance(exc, psycopg.OperationalError) and code is None:
        lowered = msg.lower()
        if any(hint in lowered for hint in _AUTH_HINTS):
            return AuthError(msg)
        if "does not exist" in lowered and "database" in lowered:
            return NotFoundError(msg)
        # recusa de conexão, timeout de conexão, conexão perdida
        return TransientError(msg)
    return AdapterError(msg)
