from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest
from dengcode_adapter_llm_openai.llm import OpenAILLM, to_wire

from dengcode_core.llm.events import ToolCallEnd
from dengcode_core.llm.messages import Message, ToolCallPart, ToolResultPart
from dengcode_core.llm.retry import RetryPolicy
from dengcode_core.tools.base import INVALID_JSON_KEY
from dengcode_testkit.contracts.llm import LLMContract, collect, contract_request

MODEL = "gpt-4o"


def chunk(
    delta: dict[str, Any],
    finish: str | None = None,
    usage: dict[str, int] | None = None,
    choices: bool = True,
) -> dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if choices else [],
        "usage": usage,
    }


def sse(*chunks: dict[str, Any]) -> bytes:
    body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode("utf-8")


USAGE = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}

TEXT_SSE = sse(
    chunk({"role": "assistant", "content": "Olá, "}),
    chunk({"content": "mundo"}),
    chunk({}, finish="stop"),
    chunk({}, usage=USAGE, choices=False),
)

TOOL_SSE = sse(
    chunk(
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_a",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": '},
                }
            ],
        }
    ),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Recife"}'}}]}),
    chunk({}, finish="tool_calls"),
    chunk({}, usage=USAGE, choices=False),
)


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


def _ok(body: bytes) -> httpx2.Response:
    return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})


def _err(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"message": f"erro {status}"}}, headers=headers or {}
    )


def make(server: Server) -> OpenAILLM:
    async def no_sleep(_: float) -> None:
        return None

    client = openai.AsyncOpenAI(
        api_key="sk-teste",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )
    return OpenAILLM(client, retry=RetryPolicy(sleep=no_sleep))


class TestOpenAIContract(LLMContract):
    @pytest.fixture
    def known_model(self) -> str:
        return MODEL

    @pytest.fixture
    def llm_text(self) -> OpenAILLM:
        return make(Server(always=_ok(TEXT_SSE)))

    @pytest.fixture
    def llm_tool(self) -> OpenAILLM:
        return make(Server(always=_ok(TOOL_SSE)))

    @pytest.fixture
    def llm_flaky(self) -> tuple[OpenAILLM, Callable[[], int]]:
        h = {"retry-after": "0"}
        server = Server([_err(429, h), _err(429, h)], always=_ok(TEXT_SSE))
        return make(server), server.count

    @pytest.fixture
    def llm_unauthorized(self) -> tuple[OpenAILLM, Callable[[], int]]:
        server = Server(always=_err(401))
        return make(server), server.count


def test_wire_translation_of_tool_round_trip() -> None:
    call = Message(
        role="assistant",
        parts=[ToolCallPart(id="call_a", name="get_weather", arguments={"city": "Recife"})],
    )
    result = Message(role="tool", parts=[ToolResultPart(call_id="call_a", content="31°C")])
    [a] = to_wire(call)
    assert a["tool_calls"][0]["function"]["arguments"] == '{"city": "Recife"}'
    assert a["content"] is None
    assert to_wire(result) == [{"role": "tool", "tool_call_id": "call_a", "content": "31°C"}]


async def test_invalid_tool_json_is_flagged() -> None:
    body = sse(
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "c",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{city"},
                    }
                ]
            }
        ),
        chunk({}, finish="tool_calls"),
    )
    events = await collect(make(Server(always=_ok(body))), contract_request(MODEL))
    [end] = [e for e in events if isinstance(e, ToolCallEnd)]
    assert INVALID_JSON_KEY in end.arguments


async def test_server_error_is_retried() -> None:
    server = Server([_err(503)], always=_ok(TEXT_SSE))
    await collect(make(server), contract_request(MODEL))
    assert server.count() == 2
