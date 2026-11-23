from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from dengcode_adapter_llm_ollama.embedding import OllamaEmbedding
from dengcode_core.errors import AdapterError, ConfigError, NotFoundError, TransientError
from dengcode_core.llm.retry import RetryPolicy
from dengcode_core.ports.embedding import EmbeddingPort
from dengcode_core.ports.models import AdapterContext


class Server:
    def __init__(self, responses: list[httpx2.Response] | None = None, dims: int = 768) -> None:
        self.responses = responses or []
        self.dims = dims
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        body = json.loads(request.content)
        vectors = [[float(i)] * self.dims for i, _ in enumerate(body["input"])]
        return httpx2.Response(200, json={"model": body["model"], "embeddings": vectors})

    def payloads(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


def make(server: Server, **kwargs: Any) -> OllamaEmbedding:
    async def no_sleep(_: float) -> None:
        return None

    client = httpx2.AsyncClient(
        base_url="http://ollama.test", transport=httpx2.MockTransport(server)
    )
    return OllamaEmbedding(client, retry=RetryPolicy(max_attempts=3, sleep=no_sleep), **kwargs)


async def test_embed_posts_to_api_embed() -> None:
    server = Server()
    emb = make(server)
    vectors = await emb.embed(["pedidos atrasados", "receita líquida"])
    assert vectors == [[0.0] * 768, [1.0] * 768]
    assert server.requests[0].url.path == "/api/embed"
    assert server.payloads() == [
        {"model": "nomic-embed-text", "input": ["pedidos atrasados", "receita líquida"]}
    ]


async def test_batches_and_dimensions_in_payload() -> None:
    server = Server(dims=256)
    emb = make(server, model="nomic-embed-text:latest", dimensions=256, batch_size=2)
    vectors = await emb.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert emb.dimensions == 256
    assert [p["input"] for p in server.payloads()] == [["a", "b"], ["c"]]
    assert all(p["dimensions"] == 256 for p in server.payloads())


async def test_retries_transient_errors() -> None:
    server = Server([httpx2.Response(503, text="ocupado"), httpx2.Response(429, text="calma")])
    emb = make(server)
    assert len(await emb.embed(["x"])) == 1
    assert len(server.requests) == 3


async def test_gives_up_after_max_attempts() -> None:
    server = Server([httpx2.Response(500, text="erro")] * 5)
    with pytest.raises(TransientError):
        await make(server).embed(["x"])
    assert len(server.requests) == 3


async def test_missing_model_is_not_found_without_retry() -> None:
    server = Server([httpx2.Response(404, json={"error": "model not found"})])
    with pytest.raises(NotFoundError, match="ollama pull"):
        await make(server).embed(["x"])
    assert len(server.requests) == 1


async def test_wrong_dimension_is_adapter_error() -> None:
    with pytest.raises(AdapterError, match="dimensions"):
        await make(Server(dims=10)).embed(["x"])


async def test_connection_refused_is_actionable() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("recusado", request=request)

    client = httpx2.AsyncClient(
        base_url="http://ollama.test", transport=httpx2.MockTransport(refuse)
    )
    with pytest.raises(AdapterError, match="ollama serve"):
        await OllamaEmbedding(client).embed(["x"])


def test_unknown_model_needs_dimensions() -> None:
    client = httpx2.AsyncClient(base_url="http://ollama.test")
    with pytest.raises(ConfigError):
        OllamaEmbedding(client, model="modelo-caseiro")
    assert OllamaEmbedding(client, model="modelo-caseiro", dimensions=512).dimensions == 512


def test_from_config() -> None:
    emb = OllamaEmbedding.from_config(
        {"host": "localhost:11434", "model": "mxbai-embed-large"}, AdapterContext(workspace="t")
    )
    assert emb.dimensions == 1024
    assert isinstance(emb, EmbeddingPort)
    assert OllamaEmbedding.info.name == "ollama"
    assert str(emb._client.base_url).startswith("http://localhost:11434")
