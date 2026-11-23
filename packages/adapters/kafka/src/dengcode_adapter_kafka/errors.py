"""Tradução das exceções do ``aiokafka`` (e da rede) para os erros do DengCode.

Nenhuma exceção do SDK atravessa a porta: tudo passa por ``map_error``.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiokafka.errors import (
    AuthenticationFailedError,
    AuthenticationMethodNotSupported,
    ClusterAuthorizationFailedError,
    GroupAuthorizationFailedError,
    IllegalSaslStateError,
    KafkaConnectionError,
    KafkaError,
    KafkaTimeoutError,
    NoBrokersAvailable,
    SaslAuthenticationFailed,
    TopicAuthorizationFailedError,
    UnknownTopicOrPartitionError,
    UnsupportedSaslMechanismError,
)

from dengcode_core.errors import (
    AdapterError,
    AuthError,
    DengCodeError,
    NotFoundError,
    TransientError,
)

_MAX_DETAIL = 300

_AUTH_ERRORS = (
    AuthenticationFailedError,
    AuthenticationMethodNotSupported,
    SaslAuthenticationFailed,
    UnsupportedSaslMechanismError,
    IllegalSaslStateError,
    TopicAuthorizationFailedError,
    GroupAuthorizationFailedError,
    ClusterAuthorizationFailedError,
)
_TRANSIENT_ERRORS = (
    KafkaConnectionError,
    NoBrokersAvailable,
    KafkaTimeoutError,
    TimeoutError,
    ConnectionError,
)


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Remove credenciais conhecidas do texto e limita o tamanho."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    text = " ".join(text.split())
    return text if len(text) <= _MAX_DETAIL else text[:_MAX_DETAIL] + "…"


def map_error(
    exc: BaseException, what: str, *, secrets: Iterable[str | None] = ()
) -> DengCodeError:
    """Converte uma exceção do SDK (ou de rede) no erro de domínio adequado."""
    if isinstance(exc, DengCodeError):
        return exc
    detail = redact(str(exc) or type(exc).__name__, secrets)
    if isinstance(exc, UnknownTopicOrPartitionError):
        return NotFoundError(f"tópico ou partição inexistente em {what}: {detail}")
    if isinstance(exc, _AUTH_ERRORS):
        return AuthError(f"credencial inválida ou sem permissão no Kafka ({what}): {detail}")
    if isinstance(exc, _TRANSIENT_ERRORS) or (
        isinstance(exc, KafkaError) and getattr(exc, "retriable", False)
    ):
        return TransientError(f"broker Kafka indisponível ou lento ({what}): {detail}")
    if isinstance(exc, OSError):
        # falhas de socket que não são ConnectionError (DNS, rede inalcançável...)
        return TransientError(f"falha de rede ao falar com o Kafka ({what}): {detail}")
    return AdapterError(f"falha no Kafka ({what}): {detail}")


def is_retriable(exc: BaseException) -> bool:
    """O erro do SDK costuma sumir sozinho (coordenador subindo, líder trocando).

    Falha de bootstrap (broker inalcançável) e tópico inexistente não entram:
    repetir só atrasaria a resposta."""
    if isinstance(exc, KafkaConnectionError | NoBrokersAvailable | UnknownTopicOrPartitionError):
        return False
    return isinstance(exc, KafkaError) and bool(getattr(exc, "retriable", False))
