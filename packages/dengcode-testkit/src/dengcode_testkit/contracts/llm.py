"""Contrato da LLMPort.

O adapter herda ``LLMContract`` numa classe ``Test...`` e fornece fixtures que
devolvem o adapter apontando para respostas gravadas (sem rede):

- ``known_model``: um modelo da tabela de perfis do adapter;
- ``llm_text``: responde "Olá, mundo" a qualquer pedido;
- ``llm_tool``: responde uma chamada ``get_weather`` com ``{"city": "Recife"}``;
- ``llm_flaky``: ``(llm, contador)`` — 429 nas duas primeiras chamadas, depois texto;
- ``llm_unauthorized``: ``(llm, contador)`` — sempre 401.
O adapter deve usar uma ``RetryPolicy`` com ``sleep`` falso nessas fixtures.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from dengcode_core.errors import AuthError
from dengcode_core.llm.events import Stop, StreamEvent, TextDelta, ToolCallEnd, ToolCallStart, Usage
from dengcode_core.llm.messages import ChatRequest, Message, ToolSpec
from dengcode_core.llm.port import LLMPort

WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Clima atual de uma cidade.",
    json_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)

Counter = Callable[[], int]


def contract_request(model: str) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[
            Message.text("system", "Você é um assistente de teste."),
            Message.text("user", "Como está o tempo em Recife?"),
        ],
        tools=[WEATHER_TOOL],
        max_output_tokens=256,
    )


async def collect(llm: LLMPort, request: ChatRequest) -> list[StreamEvent]:
    return [ev async for ev in llm.stream(request)]


class LLMContract:
    async def test_text_stream(self, llm_text: LLMPort, known_model: str) -> None:
        events = await collect(llm_text, contract_request(known_model))
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "Olá, mundo"
        assert any(isinstance(e, Usage) for e in events)
        last = events[-1]
        assert isinstance(last, Stop)
        assert last.reason == "end"

    async def test_tool_call_stream(self, llm_tool: LLMPort, known_model: str) -> None:
        events = await collect(llm_tool, contract_request(known_model))
        starts = [e for e in events if isinstance(e, ToolCallStart)]
        ends = [e for e in events if isinstance(e, ToolCallEnd)]
        assert [s.name for s in starts] == ["get_weather"]
        assert len(ends) == 1
        assert ends[0].id == starts[0].id
        assert ends[0].arguments == {"city": "Recife"}
        last = events[-1]
        assert isinstance(last, Stop)
        assert last.reason == "tool_use"

    def test_profiles(self, llm_text: LLMPort, known_model: str) -> None:
        known = llm_text.profile(known_model)
        assert known.model == known_model
        assert known.context_window > 0
        unknown = llm_text.profile("modelo-que-nao-existe-xyz")
        assert unknown.context_window == 32_000
        assert not unknown.supports_parallel_tools

    async def test_count_tokens(self, llm_text: LLMPort, known_model: str) -> None:
        assert await llm_text.count_tokens(contract_request(known_model)) > 0

    async def test_retry_429_succeeds_on_third_attempt(
        self, llm_flaky: tuple[LLMPort, Counter], known_model: str
    ) -> None:
        llm, calls = llm_flaky
        events = await collect(llm, contract_request(known_model))
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Olá, mundo"
        assert calls() == 3

    async def test_401_fails_immediately(
        self, llm_unauthorized: tuple[LLMPort, Counter], known_model: str
    ) -> None:
        llm, calls = llm_unauthorized
        with pytest.raises(AuthError):
            await collect(llm, contract_request(known_model))
        assert calls() == 1
