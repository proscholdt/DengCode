from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from dengcode_adapter_embedding_local.embedding import DEFAULT_MODEL, LocalEmbedding
from dengcode_core.errors import AdapterError, ConfigError
from dengcode_core.ports.embedding import EmbeddingPort
from dengcode_core.ports.models import AdapterContext


class FakeModel:
    def __init__(self, dims: int = 384, *, fail: Exception | None = None) -> None:
        self.dims = dims
        self.fail = fail
        self.calls: list[tuple[list[str], int]] = []

    def embed(self, documents: list[str], batch_size: int = 256) -> Iterable[Any]:
        self.calls.append((list(documents), batch_size))
        if self.fail is not None:
            raise self.fail
        for i, _ in enumerate(documents):
            yield [float(i)] * self.dims


class Factory:
    def __init__(self, model: FakeModel | None = None, fail: Exception | None = None) -> None:
        self.model = model or FakeModel()
        self.fail = fail
        self.count = 0

    def __call__(self) -> FakeModel:
        self.count += 1
        if self.fail is not None:
            raise self.fail
        return self.model


async def test_lazy_load_and_embed() -> None:
    factory = Factory()
    emb = LocalEmbedding(model_factory=factory, batch_size=8)
    assert emb.dimensions == 384
    assert not emb.loaded
    assert factory.count == 0
    vectors = await emb.embed(["olá", "mundo"])
    assert vectors == [[0.0] * 384, [1.0] * 384]
    await emb.embed(["de novo"])
    assert factory.count == 1  # carrega uma vez só
    assert factory.model.calls[0] == (["olá", "mundo"], 8)


async def test_empty_input_does_not_load() -> None:
    factory = Factory()
    emb = LocalEmbedding(model_factory=factory)
    assert await emb.embed([]) == []
    assert factory.count == 0


async def test_load_failure_becomes_adapter_error() -> None:
    emb = LocalEmbedding(model_factory=Factory(fail=OSError("sem rede")))
    with pytest.raises(AdapterError, match="sem internet"):
        await emb.embed(["x"])


async def test_runtime_failure_becomes_adapter_error() -> None:
    emb = LocalEmbedding(model_factory=Factory(FakeModel(fail=RuntimeError("onnx"))))
    with pytest.raises(AdapterError, match="onnx"):
        await emb.embed(["x"])


async def test_wrong_dimension_is_detected() -> None:
    emb = LocalEmbedding(model_factory=Factory(FakeModel(dims=10)))
    with pytest.raises(AdapterError, match="dimensions"):
        await emb.embed(["x"])


def test_dimensions_override_and_unknown_model() -> None:
    assert LocalEmbedding(model="meu/modelo", dimensions=99).dimensions == 99
    with pytest.raises(ConfigError):
        LocalEmbedding(model="nao/existe-em-lugar-nenhum")


def test_from_config_defaults_to_workspace_cache(tmp_path: Path) -> None:
    ctx = AdapterContext(workspace="ws", cache_dir=tmp_path / "cache")
    emb = LocalEmbedding.from_config({}, ctx)
    assert emb.model == DEFAULT_MODEL
    assert emb._cache_dir == tmp_path / "cache" / "models"
    custom = LocalEmbedding.from_config(
        {"model": "BAAI/bge-small-en-v1.5", "cache_dir": str(tmp_path / "m"), "threads": 2}, ctx
    )
    assert custom.dimensions == 384
    assert custom._cache_dir == tmp_path / "m"
    assert custom._threads == 2
    assert not custom.loaded


def test_satisfies_port() -> None:
    emb = LocalEmbedding(model_factory=Factory())
    assert isinstance(emb, EmbeddingPort)
    assert LocalEmbedding.info.name == "local"


@pytest.mark.integration
async def test_real_model_is_multilingual(tmp_path: Path) -> None:
    """Baixa o modelo padrão (~220 MB). Defina DENGCODE_MODEL_CACHE para reaproveitar."""
    cache = Path(os.environ.get("DENGCODE_MODEL_CACHE") or tmp_path / "models")
    emb = LocalEmbedding(cache_dir=cache)
    try:
        vectors = await emb.embed(
            [
                "a carga de pedidos falhou por timeout",
                "the orders load failed due to a timeout",
                "receita de bolo de cenoura",
            ]
        )
    except AdapterError as exc:
        pytest.skip(f"modelo indisponível (offline?): {exc}")

    def cos(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b, strict=True))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(y * y for y in b) ** 0.5
        return dot / (na * nb)

    assert len(vectors[0]) == 384
    assert cos(vectors[0], vectors[1]) > cos(vectors[0], vectors[2])
