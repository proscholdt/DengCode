"""Retry com backoff exponencial + jitter, só para erros temporários.

Usado pelos adapters de LLM (e por quem mais precisar). Regras do plano:
base 1 s, fator 2, teto 30 s, no máximo 5 tentativas; respeita ``Retry-After``.
Nunca repete 400/401/403/404 (esses viram erros não temporários no adapter).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from dengcode_core.errors import TransientError

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5
    base_s: float = 1.0
    factor: float = 2.0
    cap_s: float = 30.0
    sleep: Sleep = asyncio.sleep
    rand: Callable[[], float] = field(default=random.random)

    def delay(self, attempt: int, retry_after_s: float | None = None) -> float:
        """Atraso antes da tentativa ``attempt + 1`` (attempt começa em 1)."""
        if retry_after_s is not None and retry_after_s >= 0:
            return min(retry_after_s, self.cap_s)
        exp = min(self.cap_s, self.base_s * self.factor ** (attempt - 1))
        # "full jitter" limitado: entre 50% e 100% do exponencial
        return exp * (0.5 + 0.5 * self.rand())


async def call_with_retry[T](fn: Callable[[], Awaitable[T]], policy: RetryPolicy) -> T:
    attempt = 1
    while True:
        try:
            return await fn()
        except TransientError as exc:
            if attempt >= policy.max_attempts:
                raise
            wait = policy.delay(attempt, exc.retry_after_s)
            log.info("erro temporário (%s); tentativa %d em %.1fs", exc, attempt + 1, wait)
            await policy.sleep(wait)
            attempt += 1


async def stream_with_retry[T](
    open_stream: Callable[[], AsyncIterator[T]], policy: RetryPolicy
) -> AsyncIterator[T]:
    """Repete a abertura do stream enquanto nenhum evento foi entregue.

    Depois do primeiro evento, um erro não é repetido (evita duplicar texto).
    """
    attempt = 1
    while True:
        delivered = False
        try:
            async for item in open_stream():
                delivered = True
                yield item
            return
        except TransientError as exc:
            if delivered or attempt >= policy.max_attempts:
                raise
            wait = policy.delay(attempt, exc.retry_after_s)
            log.info("erro temporário (%s); tentativa %d em %.1fs", exc, attempt + 1, wait)
            await policy.sleep(wait)
            attempt += 1


def parse_retry_after(value: str | None) -> float | None:
    """Converte o cabeçalho ``Retry-After`` (segundos) em float."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
