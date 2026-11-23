"""Montagem segura da expressão ``MATCH`` do FTS5 a partir de texto livre.

O texto do usuário nunca vai cru para o ``MATCH``: operadores (``AND``,
``NEAR``, ``col:``), aspas e parênteses causariam erro de sintaxe ou mudariam
a consulta. Extraímos só palavras (letras e dígitos), descartamos palavras
vazias comuns e citamos cada uma entre aspas, unidas por ``OR`` — o bm25 se
encarrega de ordenar quem casa mais termos. Palavras com 3+ letras viram
busca por prefixo (``"pedido"*`` acha ``pedidos``), útil para o português.
"""

from __future__ import annotations

import re

MAX_TERMS = 32
MIN_PREFIX_LEN = 3

_TOKEN = re.compile(r"[^\W_]+")

STOPWORDS = frozenset(
    {
        # português
        "ao",
        "aos",
        "as",
        "às",
        "com",
        "como",
        "da",
        "das",
        "de",
        "do",
        "dos",
        "em",
        "entre",
        "foi",
        "há",
        "isso",
        "mais",
        "mas",
        "na",
        "nas",
        "no",
        "nos",
        "num",
        "numa",
        "os",
        "ou",
        "para",
        "pela",
        "pelo",
        "por",
        "qual",
        "que",
        "se",
        "sem",
        "ser",
        "são",
        "um",
        "uma",
        "umas",
        "uns",
        # inglês
        "an",
        "and",
        "are",
        "by",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }
)


def terms(text: str) -> list[str]:
    """Palavras relevantes do texto, em minúsculas, sem repetição, em ordem."""
    seen: dict[str, None] = {}
    for token in _TOKEN.findall(text.lower()):
        if len(token) < 2 or token in STOPWORDS:
            continue
        seen.setdefault(token, None)
    return list(seen)[:MAX_TERMS]


def fts_match_expression(text: str) -> str | None:
    """Expressão ``MATCH`` segura, ou ``None`` se não sobrar termo nenhum."""
    words = terms(text)
    if not words:
        return None
    # cada termo só tem letras/dígitos: não há aspas a escapar
    parts = [f'"{w}"*' if len(w) >= MIN_PREFIX_LEN else f'"{w}"' for w in words]
    return " OR ".join(parts)
