from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from dengcode_adapter_llm_anthropic.llm import AnthropicLLM, _to_wire
from dengcode_core.llm.events import ProviderState
from dengcode_core.llm.messages import Message, ProviderStatePart, TextPart, ToolCallPart
from dengcode_core.llm.retry import RetryPolicy
from dengcode_testkit.contracts.llm import LLMContract, collect, contract_request

MODEL = "claude-opus-5-5"


def sse(*events: dict[str, Any]) -> bytes:
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events
    ).encode("utf-8")


def _start() -> dict[str, Any]:
    return {
        "type": "message_start",
        "message": {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 25, "output_tokens": 1, "cache_read_input_tokens": 5},
        },
    }


def _thinking(index: int) -> list[dict[str, Any]]:
    return [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": "assinatura-opaca"},
        },
        {"type": "content_block_stop", "index": index},
    ]


def _end(reason: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": 12},
        },
        {"type": "message_stop"},
    ]


TEXT_SSE = sse(
    _start(),
    *_thinking(0),
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Olá, "}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "mundo"}},
    {"type": "content_block_stop", "index": 1},
    *_end("end_turn"),
)

TOOL_SSE = sse(
    _start(),
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '{"city": '},
    },
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "input_json_delta", "partial_json": '"Recife"}'},
    },
    {"type": "content_block_stop", "index": 0},
    *_end("tool_use"),
)

Handler = Callable[[httpx2.Request], httpx2.Response]


def _ok(body: bytes) -> httpx2.Response:
    return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _err(status: int, kind: str, headers: dict[str, str] | None = None) -> httpx2.Response:
    body = {"type": "error", "error": {"type": kind, "message": kind}}
    return httpx2.Response(status, json=body, headers=headers or {})


class Server:
    def __init__(
        self, responses: list[httpx2.Response] | None = None, always: httpx2.Response | None = None
    ) -> None:
        self.responses = responses or []
        self.always = always
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        assert self.always is not None
        return self.always

    def count(self) -> int:
        return len(self.requests)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def make(server: Server, **kwargs: Any) -> AnthropicLLM:
    async def no_sleep(_: float) -> None:
        return None

    client = anthropic.AsyncAnthropic(
        api_key="sk-teste",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )
    return AnthropicLLM(client, retry=RetryPolicy(sleep=no_sleep), **kwargs)


class TestAnthropicContract(LLMContract):
    @pytest.fixture
    def known_model(self) -> str:
        return MODEL

    @pytest.fixture
    def llm_text(self) -> AnthropicLLM:
        return make(Server(always=_ok(TEXT_SSE)))

    @pytest.fixture
    def llm_tool(self) -> AnthropicLLM:
        return make(Server(always=_ok(TOOL_SSE)))

    @pytest.fixture
    def llm_flaky(self) -> tuple[AnthropicLLM, Callable[[], int]]:
        retry = {"retry-after": "0"}
        server = Server(
            [_err(429, "rate_limit_error", retry), _err(429, "rate_limit_error", retry)],
            always=_ok(TEXT_SSE),
        )
        return make(server), server.count

    @pytest.fixture
    def llm_unauthorized(self) -> tuple[AnthropicLLM, Callable[[], int]]:
        server = Server(always=_err(401, "authentication_error"))
        return make(server), server.count


async def test_request_shape_for_current_model() -> None:
    server = Server(always=_ok(TEXT_SSE))
    await collect(make(server), contract_request(MODEL))
    body = server.body()
    assert body["system"] == "Você é um assistente de teste."
    assert body["thinking"]["type"] == "adaptive"
    assert body["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "high"
    assert body["tools"][0]["eager_input_streaming"] is True
    assert body["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in body
    betas = server.requests[-1].headers["anthropic-beta"]
    assert "thinking-binding-controls-2026-08-01" in betas
    assert "server-side-fallback-2026-07-01" in betas


async def test_old_model_gets_no_beta_features() -> None:
    server = Server(always=_ok(TEXT_SSE))
    await collect(make(server), contract_request("claude-haiku-4-5"))
    body = server.body()
    assert "thinking" not in body
    assert "fallbacks" not in body
    assert "anthropic-beta" not in server.requests[-1].headers


async def test_provider_state_is_replayed_verbatim() -> None:
    server = Server(always=_ok(TEXT_SSE))
    events = await collect(make(server), contract_request(MODEL))
    [state] = [e for e in events if isinstance(e, ProviderState)]
    content = state.data["content"]
    assert content[0]["type"] == "thinking"
    assert content[0]["signature"] == "assinatura-opaca"
    msg = Message(
        role="assistant",
        parts=[
            TextPart(text="Olá, mundo"),
            ProviderStatePart(provider="anthropic", data=state.data),
        ],
    )
    assert _to_wire(msg) == [{"role": "assistant", "content": content}]


def test_tool_messages_without_state_are_translated() -> None:
    call = Message(role="assistant", parts=[ToolCallPart(id="t1", name="x", arguments={"a": 1})])
    assert _to_wire(call)[0]["content"][0] == {
        "type": "tool_use",
        "id": "t1",
        "name": "x",
        "input": {"a": 1},
    }
    foreign = ProviderStatePart(provider="outro", data={"content": ["ignorar"]})
    mixed = Message(role="assistant", parts=[TextPart(text="oi"), foreign])
    assert _to_wire(mixed)[0]["content"] == [{"type": "text", "text": "oi"}]


async def test_overloaded_is_retried() -> None:
    server = Server([_err(529, "overloaded_error")], always=_ok(TEXT_SSE))
    await collect(make(server), contract_request(MODEL))
    assert server.count() == 2


async def test_bad_request_is_not_retried() -> None:
    from dengcode_core.errors import AdapterError, TransientError

    server = Server(always=_err(400, "invalid_request_error"))
    with pytest.raises(AdapterError) as info:
        await collect(make(server), contract_request(MODEL))
    assert not isinstance(info.value, TransientError)
    assert server.count() == 1
