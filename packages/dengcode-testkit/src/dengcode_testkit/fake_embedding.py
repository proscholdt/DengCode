"""FakeEmbedding: vetores determinísticos sem modelo nenhum.

Saco de palavras com *hashing*: cada palavra (minúscula, sem acento, com um
radical bem simples — tira o ``s`` final) soma ±1 numa posição do vetor
escolhida por hash estável (``blake2b``, não o ``hash()`` aleatório do
Python). O vetor sai normalizado; textos que compartilham palavras ficam
próximos, textos sem palavra em comum ficam quase ortogonais. Texto sem
palavra nenhuma vira o vetor nulo.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import ClassVar

from dengcode_core.errors import DengCodeError
from dengcode_core.ports.models import AdapterInfo, PortKind

_TOKEN = re.compile(r"[^\W_]+")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fake_tokens(text: str) -> list[str]:
    """Palavras normalizadas que entram no vetor."""
    out: list[str] = []
    for token in _TOKEN.findall(_strip_accents(text.lower())):
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        out.append(token)
    return out


class FakeEmbedding:
    """EmbeddingPort falso; ``calls`` guarda cada lote recebido."""

    info: ClassVar[AdapterInfo] = AdapterInfo(name="fake", port=PortKind.EMBEDDING, version="0.1.0")
    name = "fake"

    def __init__(self, dimensions: int = 256, *, fail_with: DengCodeError | None = None) -> None:
        if dimensions < 2:
            raise ValueError("dimensions precisa ser >= 2")
        self.dimensions = dimensions
        self.fail_with = fail_with
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail_with is not None:
            raise self.fail_with
        return [self.vector(text) for text in texts]

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self.dimensions
        for token in fake_tokens(text):
            digest = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest())
            sign = 1.0 if (digest >> 40) & 1 else -1.0
            values[digest % self.dimensions] += sign
        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:
            return values
        return [v / norm for v in values]
