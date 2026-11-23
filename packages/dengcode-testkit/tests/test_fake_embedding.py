from __future__ import annotations

import math

import pytest

from dengcode_core.errors import TransientError
from dengcode_core.ports.embedding import EmbeddingPort
from dengcode_testkit.fake_embedding import FakeEmbedding, fake_tokens


def cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_deterministic_and_normalized() -> None:
    fake = FakeEmbedding(dimensions=32)
    first, second = await fake.embed(["pedidos atrasados", "pedidos atrasados"])
    assert first == second
    assert len(first) == 32
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0)
    assert FakeEmbedding(dimensions=32).vector("pedidos atrasados") == first
    assert fake.calls == [["pedidos atrasados", "pedidos atrasados"]]


def test_similar_texts_are_closer() -> None:
    fake = FakeEmbedding()
    base = fake.vector("a carga de pedidos falhou por timeout")
    similar = fake.vector("falhou a carga dos pedidos")
    other = fake.vector("receita líquida do trimestre")
    assert cos(base, similar) > cos(base, other)
    assert cos(base, similar) > 0.5


def test_accents_case_and_plural_are_normalized() -> None:
    assert fake_tokens("Devoluções PEDIDOS") == ["devolucoe", "pedido"]
    fake = FakeEmbedding()
    assert fake.vector("pedido") == fake.vector("Pedidos")


def test_empty_text_is_zero_vector() -> None:
    assert FakeEmbedding(dimensions=8).vector("  ") == [0.0] * 8


async def test_fail_with_and_port() -> None:
    fake = FakeEmbedding(fail_with=TransientError("fora do ar"))
    assert isinstance(fake, EmbeddingPort)
    with pytest.raises(TransientError):
        await fake.embed(["x"])
    with pytest.raises(ValueError, match="dimensions"):
        FakeEmbedding(dimensions=1)
