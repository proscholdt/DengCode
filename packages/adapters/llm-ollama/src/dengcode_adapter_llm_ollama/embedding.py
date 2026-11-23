"""EmbeddingPort para o Ollama local (HTTP ``POST /api/embed``)."""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar

import httpx2

from dengcode_adapter_llm_ollama.llm import DEFAULT_HOST, _status_error
from dengcode_core.errors import AdapterError, ConfigError, TransientError
from dengcode_core.llm.retry import RetryPolicy, call_with_retry
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind

DEFAULT_MODEL = "nomic-embed-text"

# dimensões nativas dos modelos de embedding mais comuns da biblioteca do Ollama
KNOWN_DIMENSIONS: dict[str, int] = {
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
    "bge-m3": 1024,
    "bge-large": 1024,
    "snowflake-arctic-embed": 1024,
    "paraphrase-multilingual": 768,
    "granite-embedding": 384,
}


class OllamaEmbedding:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="ollama", port=PortKind.EMBEDDING, version="0.1.0"
    )
    name = "ollama"

    def __init__(
        self,
        client: httpx2.AsyncClient,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int | None = None,
        batch_size: int = 64,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._client = client
        self.model = model
        # dimensão pedida explicitamente vai no payload (modelos com Matryoshka)
        self._requested_dims = dimensions
        self.dimensions = dimensions or _dimensions_of(model)
        self._batch_size = batch_size
        self._retry = retry or RetryPolicy(max_attempts=3)

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> OllamaEmbedding:
        host = str(cfg.get("host") or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST)
        if not host.startswith("http"):
            host = f"http://{host}"
        client = httpx2.AsyncClient(base_url=host, timeout=float(cfg.get("timeout_s", 120)))
        dims = cfg.get("dimensions")
        return cls(
            client,
            model=str(cfg.get("model") or DEFAULT_MODEL),
            dimensions=int(dims) if dims else None,
            batch_size=int(cfg.get("batch_size", 64)),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            out.extend(
                await call_with_retry(lambda batch=batch: self._embed_once(batch), self._retry)
            )
        return out

    def build_payload(self, texts: list[str]) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "input": texts}
        if self._requested_dims is not None:
            payload["dimensions"] = self._requested_dims
        return payload

    async def _embed_once(self, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.post("/api/embed", json=self.build_payload(texts))
        except httpx2.ConnectError as exc:
            raise AdapterError(
                f"não foi possível conectar ao Ollama em {self._client.base_url}; "
                "ele está rodando? (ollama serve)"
            ) from exc
        except (httpx2.TimeoutException, httpx2.RemoteProtocolError, httpx2.ReadError) as exc:
            raise TransientError(f"ollama: {exc}") from exc
        if resp.status_code >= 400:
            raise _status_error(resp.status_code, resp.text, resp.headers, self.model)
        try:
            data: dict[str, Any] = resp.json()
        except json.JSONDecodeError as exc:
            raise AdapterError(f"resposta inválida do Ollama: {exc}") from exc
        if data.get("error"):
            raise AdapterError(f"ollama: {data['error']}")
        vectors = [[float(x) for x in v] for v in data.get("embeddings") or []]
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            raise AdapterError(
                f"o Ollama devolveu vetores fora do esperado para {self.model} "
                f"({self.dimensions} dimensões); ajuste 'dimensions' na stack"
            )
        return vectors


def _dimensions_of(model: str) -> int:
    base = model.split(":", 1)[0].rsplit("/", 1)[-1]
    if base in KNOWN_DIMENSIONS:
        return KNOWN_DIMENSIONS[base]
    raise ConfigError(
        f"dimensão desconhecida para o modelo de embedding {model!r} do Ollama; "
        "informe 'dimensions' na stack"
    )
