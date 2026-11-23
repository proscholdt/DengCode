"""LLM com fallback: esgotadas as tentativas do principal, troca de provedor."""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from dengcode_core.errors import TransientError
from dengcode_core.llm.events import StreamEvent
from dengcode_core.llm.messages import ChatRequest
from dengcode_core.llm.port import LLMPort
from dengcode_core.llm.profile import ModelProfile

log = logging.getLogger(__name__)

OnFallback = Callable[[str, str, str], Awaitable[None] | None]
"""(provedor_origem, provedor_destino, motivo); pode ser síncrono ou assíncrono."""


class FallbackLLM:
    """Envolve um LLM principal e um reserva com mapeamento de modelos.

    ``model_map`` traduz o modelo pedido (do principal) para o do reserva.
    A troca vale para o resto da sessão.
    """

    def __init__(
        self,
        primary: LLMPort,
        fallback: LLMPort,
        model_map: dict[str, str],
        on_fallback: OnFallback | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._map = model_map
        self._on_fallback = on_fallback
        self._switched = False
        self.name = primary.name

    @property
    def active(self) -> LLMPort:
        return self._fallback if self._switched else self._primary

    def _model(self, model: str) -> str:
        return self._map.get(model, model) if self._switched else model

    def profile(self, model: str) -> ModelProfile:
        return self.active.profile(self._model(model))

    async def count_tokens(self, request: ChatRequest) -> int:
        return await self.active.count_tokens(
            request.model_copy(update={"model": self._model(request.model)})
        )

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        if self._switched:
            async for ev in self._fallback.stream(
                request.model_copy(update={"model": self._model(request.model)})
            ):
                yield ev
            return
        delivered = False
        try:
            async for ev in self._primary.stream(request):
                delivered = True
                yield ev
            return
        except TransientError as exc:
            if delivered:
                raise
            reason = f"{type(exc).__name__}: {exc}"
            log.warning(
                "provedor %s falhou (%s); usando %s",
                self._primary.name,
                reason,
                self._fallback.name,
            )
            self._switched = True
            self.name = self._fallback.name
            if self._on_fallback:
                result = self._on_fallback(self._primary.name, self._fallback.name, reason)
                if inspect.isawaitable(result):
                    await result
        async for ev in self._fallback.stream(
            request.model_copy(update={"model": self._model(request.model)})
        ):
            yield ev
