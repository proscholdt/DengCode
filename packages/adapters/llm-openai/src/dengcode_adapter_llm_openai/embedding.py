"""EmbeddingPort para a API de embeddings da OpenAI (SDK oficial ``openai``).

Atende OpenAI, Azure OpenAI (``variant: azure``; ``model`` é o nome do
deployment) e servidores compatíveis (``base_url``). O retry é nosso
(``max_retries=0`` no SDK).
"""

from __future__ import annotations

from typing import Any, ClassVar

import openai

from dengcode_adapter_llm_openai.llm import map_error
from dengcode_core.errors import AdapterError, AuthError, ConfigError
from dengcode_core.llm.retry import RetryPolicy, call_with_retry
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind

DEFAULT_MODEL = "text-embedding-3-small"

KNOWN_DIMENSIONS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class OpenAIEmbedding:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="openai", port=PortKind.EMBEDDING, version="0.1.0"
    )
    name = "openai"

    def __init__(
        self,
        client: openai.AsyncOpenAI,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int | None = None,
        batch_size: int = 256,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._client = client
        self.model = model
        # dimensão pedida explicitamente vai na chamada (modelos text-embedding-3)
        self._requested_dims = dimensions
        self.dimensions = dimensions or _dimensions_of(model)
        self._batch_size = batch_size
        self._retry = retry or RetryPolicy()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> OpenAIEmbedding:
        timeout = float(cfg.get("timeout_s", 120))
        try:
            if cfg.get("variant") == "azure":
                endpoint = cfg.get("endpoint") or cfg.get("azure_endpoint")
                if not endpoint:
                    raise ConfigError("Azure OpenAI exige 'endpoint' na stack")
                client: openai.AsyncOpenAI = openai.AsyncAzureOpenAI(
                    azure_endpoint=endpoint,
                    api_version=cfg.get("api_version", "2024-10-21"),
                    api_key=cfg.get("api_key"),
                    timeout=timeout,
                    max_retries=0,
                )
            else:
                client = openai.AsyncOpenAI(
                    api_key=cfg.get("api_key"),
                    base_url=cfg.get("base_url"),
                    timeout=timeout,
                    max_retries=0,
                )
        except openai.OpenAIError as exc:
            raise AuthError(f"credenciais do provedor indisponíveis: {exc}") from exc
        dims = cfg.get("dimensions")
        return cls(
            client,
            model=str(cfg.get("model") or DEFAULT_MODEL),
            dimensions=int(dims) if dims else None,
            batch_size=int(cfg.get("batch_size", 256)),
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            out.extend(
                await call_with_retry(lambda batch=batch: self._embed_once(batch), self._retry)
            )
        return out

    async def _embed_once(self, texts: list[str]) -> list[list[float]]:
        params: dict[str, Any] = {"model": self.model, "input": texts, "encoding_format": "float"}
        if self._requested_dims is not None:
            params["dimensions"] = self._requested_dims
        try:
            resp = await self._client.embeddings.create(**params)
        except openai.OpenAIError as exc:
            raise map_error(exc) from exc
        vectors = [[float(x) for x in item.embedding] for item in sorted(resp.data, key=_index)]
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            raise AdapterError(
                f"o provedor devolveu vetores fora do esperado para {self.model} "
                f"({self.dimensions} dimensões); ajuste 'dimensions' na stack"
            )
        return vectors


def _index(item: Any) -> int:
    return int(item.index)


def _dimensions_of(model: str) -> int:
    if model in KNOWN_DIMENSIONS:
        return KNOWN_DIMENSIONS[model]
    raise ConfigError(
        f"dimensão desconhecida para o modelo de embedding {model!r}; informe 'dimensions' na stack"
    )
