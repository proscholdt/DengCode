"""LLMPort: a única forma de o core falar com um modelo."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from dengcode_core.llm.events import StreamEvent
from dengcode_core.llm.messages import ChatRequest
from dengcode_core.llm.profile import ModelProfile


@runtime_checkable
class LLMPort(Protocol):
    name: str

    def profile(self, model: str) -> ModelProfile: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]: ...

    async def count_tokens(self, request: ChatRequest) -> int: ...


def estimate_tokens(text: str) -> int:
    """Estimativa barata e agnóstica: ~4 caracteres por token."""
    return max(1, len(text) // 4) if text else 0


def estimate_request_tokens(request: ChatRequest) -> int:
    total = 0
    for msg in request.messages:
        total += 4 + estimate_tokens(msg.model_dump_json())
    for tool in request.tools:
        total += estimate_tokens(tool.model_dump_json())
    return total
