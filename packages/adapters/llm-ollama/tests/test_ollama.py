from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from dengcode_adapter_llm_ollama.llm import OllamaLLM, to_wire

from dengcode_core.errors import ConfigError
from dengcode_core.llm.messages import Message, ToolCallPart, ToolResultPart
from dengcode_core.llm.retry import RetryPolicy
from dengcode_testkit.contracts.llm import LLMContract, collect, contract_request

MODEL = "qwen2.5:7b"


def ndjson(*lines: dict[str, Any]) -> bytes:
    return "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in lines).encode("utf-8")


DONE = {"done": True, "done_reason": "stop", "prompt_eval_count": 30, "eval_count": 7}

TEXT = ndjson(
    {"message": {"role": "assistant", "content": "Olá, "}, "done": False},
    {"message": {"role": "assistant", "content": "mundo"}, "done": False},
    {"message": {"role": "assistant", "content": ""}, **DONE},
)

TOOL = ndjson(
    {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Recife"}}}],
        },
        "done": False,
    },
    {"message": {"role": "assistant", "content": ""}, **DONE},
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


def make(server: Server) -> OllamaLLM:
    async def no_sleep(_: float) -> None:
        return None

    client = httpx2.AsyncClient(
        base_url="http://ollama.test", transport=httpx2.MockTransport(server)
    )
    return OllamaLLM(client, retry=RetryPolicy(sleep=no_sleep))


class TestOllamaContract(LLMContract):
    @pytest.fixture
    def known_model(self) -> str:
        return MODEL

    @pytest.fixture
    def llm_text(self) -> OllamaLLM:
        return make(Server(always=httpx2.Response(200, content=TEXT)))

    @pytest.fixture
    def llm_tool(self) -> OllamaLLM:
        return make(Server(always=httpx2.Response(200, content=TOOL)))

    @pytest.fixture
    def llm_flaky(self) -> tuple[OllamaLLM, Callable[[], int]]:
        busy = httpx2.Response(429, content=b"ocupado", headers={"retry-after": "0"})
        server = Server([busy, busy], always=httpx2.Response(200, content=TEXT))
        return make(server), server.count

    @pytest.fixture
    def llm_unauthorized(self) -> tuple[OllamaLLM, Callable[[], int]]:
        # Ollama local não tem auth; um proxy na frente pode devolver 401
        server = Server(always=httpx2.Response(401, content=b"unauthorized"))
        return make(server), server.count


async def test_payload_sets_context_and_tools() -> None:
    server = Server(always=httpx2.Response(200, content=TEXT))
    await collect(make(server), contract_request(MODEL))
    body = json.loads(server.requests[0].content)
    assert body["options"]["num_ctx"] == 32768
    assert body["tools"][0]["function"]["name"] == "get_weather"
    assert body["messages"][0]["role"] == "system"


async def test_model_without_tools_gives_clear_error() -> None:
    resp = httpx2.Response(400, content=b'{"error":"qwen does not support tools"}')
    with pytest.raises(ConfigError, match="não suporta ferramentas"):
        await collect(make(Server(always=resp)), contract_request(MODEL))


def test_tool_result_carries_tool_name() -> None:
    msgs = [
        Message(
            role="assistant",
            parts=[ToolCallPart(id="c1", name="file_read", arguments={"path": "a"})],
        ),
        Message(role="tool", parts=[ToolResultPart(call_id="c1", content="conteúdo")]),
    ]
    wire = to_wire(msgs)
    assert wire[1] == {"role": "tool", "content": "conteúdo", "tool_name": "file_read"}
