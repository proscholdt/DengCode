"""Compatibilidade com o laço padrão do Windows (``ProactorEventLoop``).

O psycopg assíncrono só funciona sobre ``SelectorEventLoop``; o resto do
DengCode precisa do Proactor no Windows (subprocessos do ``shell_run``). Como
cada operação deste adapter abre e fecha a própria conexão, ela é autocontida:
sob o Proactor, roda inteira num ``SelectorEventLoop`` numa thread auxiliar.
Sob qualquer outro laço, roda direto.

Limitação: cancelar a tarefa externa não interrompe a consulta na thread; o
``statement_timeout`` do servidor continua valendo.
"""

from __future__ import annotations

import asyncio
import functools
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def _needs_selector() -> bool:
    if sys.platform != "win32":
        return False
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is not None and isinstance(asyncio.get_running_loop(), proactor)


async def run_portable[T](factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    if not _needs_selector():
        return await factory()

    def in_thread() -> T:
        return asyncio.run(factory(), loop_factory=asyncio.SelectorEventLoop)

    return await asyncio.to_thread(in_thread)


def portable[**P, T](
    fn: Callable[P, Coroutine[Any, Any, T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    """Decorador para os métodos públicos assíncronos do adapter."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        return await run_portable(lambda: fn(*args, **kwargs))

    return wrapper
