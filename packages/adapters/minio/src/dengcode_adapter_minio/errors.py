"""Tradução das exceções do SDK S3 para a hierarquia de erros do DengCode.

Nenhuma exceção do ``obstore`` atravessa a porta: tudo passa por ``map_error``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from obstore.exceptions import (
    BaseError,
    InvalidPathError,
    NotSupportedError,
    PermissionDeniedError,
    UnauthenticatedError,
)

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    DengCodeError,
    NotFoundError,
    RateLimitError,
    TransientError,
    UnsupportedOperation,
)

_MAX_DETAIL = 300

_STATUS = re.compile(r"status(?: code)?:?\s*(\d{3})\b", re.IGNORECASE)
_NOT_FOUND = re.compile(r"NoSuchBucket|NoSuchKey", re.IGNORECASE)
_AUTH = re.compile(
    r"InvalidAccessKeyId|SignatureDoesNotMatch|AccessDenied|ExpiredToken|InvalidToken",
    re.IGNORECASE,
)
_RATE = re.compile(r"SlowDown|TooManyRequests", re.IGNORECASE)
_TRANSIENT = re.compile(
    r"error sending request|connection|connect|timed out|timeout|dns|broken pipe"
    r"|ServiceUnavailable|InternalError|RequestTimeout",
    re.IGNORECASE,
)


def _http_status(detail: str) -> int | None:
    match = _STATUS.search(detail)
    return int(match.group(1)) if match else None


def is_invalid_range(exc: BaseException) -> bool:
    """O servidor recusou a faixa pedida (416), típico de objeto vazio."""
    text = str(exc)
    return _http_status(text) == 416 or "InvalidRange" in text


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Remove credenciais conhecidas do texto e limita o tamanho."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = " ".join(text.split())
    return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL] + "…"


def map_error(exc: BaseException, uri: str, *, secrets: Iterable[str | None] = ()) -> DengCodeError:
    """Converte uma exceção do SDK (ou de rede) no erro de domínio adequado."""
    if isinstance(exc, DengCodeError):
        return exc
    detail = redact(str(exc), secrets)
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(f"não encontrado: {uri}")
    if isinstance(exc, UnauthenticatedError | PermissionDeniedError):
        return AuthError(f"credencial inválida ou sem permissão para {uri}: {detail}")
    if isinstance(exc, NotSupportedError):
        return UnsupportedOperation(f"operação não suportada pelo endpoint em {uri}: {detail}")
    if isinstance(exc, InvalidPathError):
        return AdapterError(f"caminho inválido {uri}: {detail}")
    if isinstance(exc, BaseError | OSError):
        # o SDK agrupa muitos casos em GenericError: decide pelo status HTTP e pelo
        # código de erro S3 presentes na mensagem
        status = _http_status(detail)
        if status == 404 or _NOT_FOUND.search(detail):
            return NotFoundError(f"não encontrado: {uri}")
        if status in (401, 403) or _AUTH.search(detail):
            return AuthError(f"credencial inválida ou sem permissão para {uri}: {detail}")
        if status == 429 or _RATE.search(detail):
            return RateLimitError(f"limite de requisições do storage em {uri}: {detail}")
        if (
            (status is not None and (status >= 500 or status == 408))
            or (status is None and _TRANSIENT.search(detail))
            or isinstance(exc, ConnectionError | TimeoutError)
        ):
            return TransientError(f"falha temporária de rede/servidor em {uri}: {detail}")
    return AdapterError(f"falha no storage S3 em {uri}: {detail}")
