from __future__ import annotations

import json
from typing import Any

import httpx2
import openai
import pytest

from dengcode_adapter_llm_openai.embedding import OpenAIEmbedding
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    RateLimitError,
)
from dengcode_core.llm.retry import RetryPolicy
from dengcode_core.ports.embedding import EmbeddingPort
from dengcode_core.ports.models import AdapterContext


def _error(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, json={"error": {"message": f"erro {status}"}}, headers=headers or {}
    )


class Server:
    def __init__(self, responses: list[httpx2.Response] | None = None, dims: int = 1536) -> None:
        self.responses = responses or []
        self.dims = dims
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        body = json.loads(request.content)
        # devolve fora de ordem para conferir a ordenação por ``index``
        data = [
            {"object": "embedding", "index": i, "embedding": [float(i)] * self.dims}
            for i, _ in enumerate(body["input"])
        ][::-1]
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": data,
                "model": body["model"],
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    def payloads(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def make(server: Server, **kwargs: Any) -> OpenAIEmbedding:
    async def no_sleep(_: float) -> None:
        return None

    client = openai.AsyncOpenAI(
        api_key="sk-teste",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )
    return OpenAIEmbedding(client, retry=RetryPolicy(max_attempts=3, sleep=no_sleep), **kwargs)


async def test_embed_calls_embeddings_endpoint_in_order() -> None:
    server = Server()
    emb = make(server)
    vectors = await emb.embed(["pedidos atrasados", "receita líquida"])
    assert vectors == [[0.0] * 1536, [1.0] * 1536]
    assert server.requests[0].url.path.endswith("/embeddings")
    payload = server.payloads()[0]
    assert payload["model"] == "text-embedding-3-small"
    assert payload["input"] == ["pedidos atrasados", "receita líquida"]
    assert "dimensions" not in payload


async def test_dimensions_and_batches() -> None:
    server = Server(dims=256)
    emb = make(server, dimensions=256, batch_size=2)
    assert len(await emb.embed(["a", "b", "c"])) == 3
    assert [p["input"] for p in server.payloads()] == [["a", "b"], ["c"]]
    assert all(p["dimensions"] == 256 for p in server.payloads())


async def test_retries_rate_limit() -> None:
    h = {"retry-after": "0"}
    server = Server([_error(429, h), _error(500)])
    assert len(await make(server).embed(["x"])) == 1
    assert len(server.requests) == 3


async def test_rate_limit_exhausted() -> None:
    server = Server([_error(429, {"retry-after": "0"})] * 5)
    with pytest.raises(RateLimitError):
        await make(server).embed(["x"])
    assert len(server.requests) == 3


@pytest.mark.parametrize(
    ("status", "error"), [(401, AuthError), (404, NotFoundError), (400, AdapterError)]
)
async def test_non_transient_errors_are_not_retried(status: int, error: type[Exception]) -> None:
    server = Server([_error(status)])
    with pytest.raises(error):
        await make(server).embed(["x"])
    assert len(server.requests) == 1


async def test_wrong_dimension_is_adapter_error() -> None:
    with pytest.raises(AdapterError, match="dimensions"):
        await make(Server(dims=8)).embed(["x"])


def test_unknown_model_needs_dimensions() -> None:
    client = openai.AsyncOpenAI(api_key="sk-teste")
    with pytest.raises(ConfigError):
        OpenAIEmbedding(client, model="embed-caseiro")
    assert OpenAIEmbedding(client, model="embed-caseiro", dimensions=64).dimensions == 64


def test_from_config() -> None:
    ctx = AdapterContext(workspace="t")
    emb = OpenAIEmbedding.from_config({"api_key": "sk-x", "model": "text-embedding-3-large"}, ctx)
    assert emb.dimensions == 3072
    assert isinstance(emb, EmbeddingPort)
    assert OpenAIEmbedding.info.name == "openai"
    with pytest.raises(ConfigError):
        OpenAIEmbedding.from_config({"variant": "azure", "api_key": "x"}, ctx)
