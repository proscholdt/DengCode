"""FakeLLM: emite eventos de streaming a partir de um roteiro.

Cada chamada a ``stream`` consome o próximo ``FakeTurn``. As requisições
recebidas ficam em ``requests`` (útil para inspecionar o que iria ao LLM).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from dengcode_core.errors import AdapterError, DengCodeError
from dengcode_core.llm.events import (
    Stop,
    StreamEvent,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)
from dengcode_core.llm.messages import ChatRequest
from dengcode_core.llm.port import estimate_request_tokens
from dengcode_core.llm.profile import ModelProfile
from dengcode_core.ports.models import AdapterInfo, PortKind


@dataclass
class FakeToolCall:
    name: str
    arguments: dict[str, Any]
    id: str | None = None


@dataclass
class FakeTurn:
    text: str = ""
    tool_calls: list[FakeToolCall] = field(default_factory=lambda: list[FakeToolCall]())
    input_tokens: int = 100
    output_tokens: int = 20
    stop: str | None = None
    error: DengCodeError | None = None


def text(t: str) -> FakeTurn:
    return FakeTurn(text=t)


def call(name: str, **arguments: Any) -> FakeTurn:
    return FakeTurn(tool_calls=[FakeToolCall(name, arguments)])


class FakeLLM:
    name = "fake"

    def __init__(
        self,
        script: Iterable[FakeTurn],
        *,
        context_window: int = 200_000,
        supports_tools: bool = True,
        price_in: float | None = 3.0,
        price_out: float | None = 15.0,
    ) -> None:
        self.script = list(script)
        self.requests: list[ChatRequest] = []
        self._seq = 0
        self._profile_args: dict[str, Any] = {
            "context_window": context_window,
            "supports_tools": supports_tools,
            "price_input_per_mtok_usd": price_in,
            "price_output_per_mtok_usd": price_out,
        }

    def profile(self, model: str) -> ModelProfile:
        return ModelProfile(
            provider="fake",
            model=model,
            max_output_tokens=4096,
            supports_parallel_tools=True,
            supports_prompt_caching=False,
            supports_vision=False,
            supports_extended_thinking=False,
            **self._profile_args,
        )

    async def count_tokens(self, request: ChatRequest) -> int:
        return estimate_request_tokens(request)

    def sent_text(self) -> str:
        """Tudo que foi enviado ao "LLM", serializado (para checar vazamentos)."""
        return "\n".join(r.model_dump_json() for r in self.requests)

    async def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if not self.script:
            raise AdapterError("FakeLLM: roteiro esgotado")
        turn = self.script.pop(0)
        if turn.error is not None:
            raise turn.error
        if turn.text:
            mid = max(1, len(turn.text) // 2)
            for chunk in (turn.text[:mid], turn.text[mid:]):
                if chunk:
                    yield TextDelta(text=chunk)
        for tc in turn.tool_calls:
            self._seq += 1
            call_id = tc.id or f"call_{self._seq}"
            payload = json.dumps(tc.arguments, ensure_ascii=False)
            yield ToolCallStart(id=call_id, name=tc.name)
            yield ToolCallDelta(id=call_id, arguments_json_fragment=payload)
            yield ToolCallEnd(id=call_id, arguments=tc.arguments)
        yield Usage(input_tokens=turn.input_tokens, output_tokens=turn.output_tokens)
        reason = turn.stop or ("tool_use" if turn.tool_calls else "end")
        yield Stop(reason=reason)  # type: ignore[arg-type]


class FakeLLMAdapter:
    """Classe registrável (``info`` + ``from_config``) para testes do motor.

    ``cfg["script"]``: lista de textos; cada um vira um turno de texto.
    """

    info: ClassVar[AdapterInfo] = AdapterInfo(name="fake", port=PortKind.LLM, version="0")
    default_models: ClassVar[dict[str, str]] = {"main": "fake-main", "fast": "fake-fast"}

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: Any) -> FakeLLM:
        llm = FakeLLM([FakeTurn(text=t) for t in cfg.get("script", ["ok"])])
        llm.name = str(cfg.get("name", "fake"))
        return llm
