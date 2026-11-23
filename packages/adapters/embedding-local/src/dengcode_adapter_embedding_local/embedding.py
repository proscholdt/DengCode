"""EmbeddingPort local com ``fastembed`` (ONNX Runtime, sem torch).

Ver ``docs/adr/0007-embeddings-locais-fastembed.md``. O modelo só é carregado
(e, na primeira vez, baixado) no primeiro ``embed``, numa thread — construir o
adapter é barato e não toca a rede.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, ClassVar, Protocol

from dengcode_core.errors import AdapterError, ConfigError
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind

log = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# dimensões conhecidas sem importar o fastembed (importá-lo custa ~2 s)
KNOWN_DIMENSIONS: dict[str, int] = {
    DEFAULT_MODEL: 384,
    "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "BAAI/bge-small-en-v1.5": 384,
    "intfloat/multilingual-e5-large": 1024,
    "nomic-ai/nomic-embed-text-v1.5": 768,
}


class TextModel(Protocol):
    """O pedaço do ``fastembed.TextEmbedding`` que usamos (permite injetar fakes)."""

    def embed(self, documents: list[str], batch_size: int = ...) -> Iterable[Any]: ...


ModelFactory = Callable[[], TextModel]


class LocalEmbedding:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="local", port=PortKind.EMBEDDING, version="0.1.0"
    )
    name = "local"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        dimensions: int | None = None,
        cache_dir: Path | None = None,
        threads: int | None = None,
        batch_size: int = 32,
        model_factory: ModelFactory | None = None,
    ) -> None:
        self.model = model
        self.dimensions = dimensions or _dimensions_of(model)
        self._cache_dir = cache_dir
        self._threads = threads
        self._batch_size = batch_size
        self._factory = model_factory or self._fastembed_factory
        self._model: TextModel | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> LocalEmbedding:
        raw_cache = cfg.get("cache_dir")
        cache: Path | None
        if raw_cache:
            cache = Path(str(raw_cache)).expanduser()
        elif ctx.cache_dir is not None:
            cache = ctx.cache_dir / "models"
        else:
            cache = None
        dims = cfg.get("dimensions")
        threads = cfg.get("threads")
        return cls(
            model=str(cfg.get("model") or DEFAULT_MODEL),
            dimensions=int(dims) if dims else None,
            cache_dir=cache,
            threads=int(threads) if threads else None,
            batch_size=int(cfg.get("batch_size", 32)),
        )

    @property
    def loaded(self) -> bool:
        return self._model is not None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, list(texts))

    # ------------------------------------------------------------ interno
    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        try:
            vectors = [
                [float(x) for x in v] for v in model.embed(texts, batch_size=self._batch_size)
            ]
        except Exception as exc:  # fronteira do adapter: nada cru atravessa a porta
            raise AdapterError(f"embedding local ({self.model}) falhou: {exc}") from exc
        if len(vectors) != len(texts) or any(len(v) != self.dimensions for v in vectors):
            raise AdapterError(
                f"o modelo {self.model} devolveu vetores fora do esperado "
                f"({self.dimensions} dimensões); ajuste 'dimensions' na stack"
            )
        return vectors

    def _load(self) -> TextModel:
        with self._lock:
            if self._model is None:
                log.info("carregando o modelo de embeddings %s", self.model)
                try:
                    self._model = self._factory()
                except (ConfigError, AdapterError):
                    raise
                except Exception as exc:
                    raise AdapterError(
                        f"não foi possível carregar o modelo de embeddings {self.model}: {exc}. "
                        "Na primeira execução ele é baixado (~220 MB no padrão): "
                        "a máquina está sem internet?"
                    ) from exc
            return self._model

    def _fastembed_factory(self) -> TextModel:
        from fastembed import TextEmbedding

        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            return TextEmbedding(
                model_name=self.model,
                cache_dir=str(self._cache_dir) if self._cache_dir is not None else None,
                threads=self._threads,
            )
        except ValueError as exc:
            raise ConfigError(f"modelo de embeddings não suportado pelo fastembed: {exc}") from exc


def _dimensions_of(model: str) -> int:
    if model in KNOWN_DIMENSIONS:
        return KNOWN_DIMENSIONS[model]
    from fastembed import TextEmbedding

    for item in TextEmbedding.list_supported_models():
        if str(item.get("model")) == model:
            return int(item["dim"])
    raise ConfigError(
        f"modelo de embeddings desconhecido: {model!r}; informe 'dimensions' na stack"
    )
