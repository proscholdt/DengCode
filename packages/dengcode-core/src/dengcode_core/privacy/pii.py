# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (os stubs do pyarrow.compute são incompletos; os tipos de retorno abaixo são explícitos)
"""PiiMasker: PII nunca vai crua ao LLM (sqlguard.md, "Resultados e PII").

Mascara por três vias:
1. colunas com tag de PII no catálogo (``pii``, ``email``, ``cpf``...);
2. colunas cujo *nome* indica PII (email, cpf, telefone...);
3. valores que casam detectores (e-mail, CPF, CNPJ, telefone, cartão com Luhn),
   em qualquer texto — inclusive saídas de arquivo e logs.
A máscara preserva o formato (``j***@d***.com``, ``***.***.***-12``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

import pyarrow as pa

PII_TAGS = frozenset(
    {
        "pii",
        "email",
        "cpf",
        "cnpj",
        "phone",
        "telefone",
        "card",
        "cartao",
        "documento",
        "sensitive",
        "sensivel",
    }
)
_PII_NAME = re.compile(
    r"(e-?mail|cpf|cnpj|telefone|celular|phone|mobile|whats|cart[aã]o|card_?number|"
    r"\brg\b|passaporte|passport|ssn|documento)",
    re.IGNORECASE,
)

EMAIL = re.compile(
    r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-])[A-Za-z0-9.-]*"
    r"\.([A-Za-z]{2,})\b"
)
CPF = re.compile(r"(?<![\d.])(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})(?![\d.])")
CNPJ = re.compile(r"(?<![\d.])(\d{2})\.?(\d{3})\.?(\d{3})/?(\d{4})-?(\d{2})(?![\d.])")
# telefone exige formatação (+DDI, (DDD) ou separadores): sequências puras de dígitos
# são ids/valores na engenharia de dados e não devem ser mascaradas
PHONE = re.compile(
    r"(?<![\w+])(?:"
    r"\+\d{1,3}[\s.-]?\(?\d{2,3}\)?[\s.-]?\d{4,5}[\s.-]?\d{4}"
    r"|\(\d{2,3}\)\s?\d{4,5}[\s.-]?\d{4}"
    r"|\d{2,3}[\s.-]\d{4,5}[\s.-]\d{4}"
    r")(?!\d)"
)
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


def cpf_valid(text: str) -> bool:
    d = _digits(text)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d[i]) * (size + 1 - i) for i in range(size))
        check = (total * 10) % 11 % 10
        if check != int(d[size]):
            return False
    return True


def cnpj_valid(text: str) -> bool:
    d = _digits(text)
    if len(d) != 14 or d == d[0] * 14:
        return False
    weights = [[5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]]
    for i, w in enumerate(weights):
        total = sum(int(d[j]) * w[j] for j in range(12 + i))
        check = 0 if total % 11 < 2 else 11 - total % 11
        if check != int(d[12 + i]):
            return False
    return True


def luhn_valid(text: str) -> bool:
    d = _digits(text)
    if not 13 <= len(d) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _keep_last(text: str, keep: int) -> str:
    """Troca dígitos por * mantendo os ``keep`` últimos e a pontuação."""
    total = sum(ch.isdigit() for ch in text)
    seen = 0
    out: list[str] = []
    for ch in text:
        if ch.isdigit():
            seen += 1
            out.append(ch if seen > total - keep else "*")
        else:
            out.append(ch)
    return "".join(out)


def _mask_email(m: re.Match[str]) -> str:
    return f"{m.group(1)}***@{m.group(2)}***.{m.group(3)}"


def _mask_if(valid: Callable[[str], bool], keep: int) -> Callable[[re.Match[str]], str]:
    def repl(m: re.Match[str]) -> str:
        return _keep_last(m.group(0), keep) if valid(m.group(0)) else m.group(0)

    return repl


def mask_text(text: str) -> str:
    """Mascara qualquer PII detectável dentro de um texto livre."""
    if not text:
        return text
    out = EMAIL.sub(_mask_email, text)
    out = CNPJ.sub(_mask_if(cnpj_valid, 2), out)
    out = CPF.sub(_mask_if(cpf_valid, 2), out)
    out = CARD.sub(_mask_if(luhn_valid, 4), out)
    return PHONE.sub(lambda m: _keep_last(m.group(0), 4), out)


def mask_value(value: str) -> str:
    """Valor de coluna marcada como PII: máscara por formato ou ``***``."""
    masked = mask_text(value)
    if masked != value:
        return masked
    return "***" if value else value


def is_pii_column(name: str, tags: Iterable[str] = ()) -> bool:
    return bool(PII_TAGS.intersection(t.lower() for t in tags)) or bool(_PII_NAME.search(name))


class PiiMasker:
    """Mascarador configurável; ``enabled=False`` só para testes explícitos."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def text(self, text: str) -> str:
        return mask_text(text) if self.enabled else text

    def table(self, table: pa.Table, pii_columns: Iterable[str] = ()) -> pa.Table:
        """Mascara colunas de texto: as marcadas inteiras; as demais por detector."""
        if not self.enabled:
            return table
        marked = {c.lower() for c in pii_columns}
        arrays: list[pa.Array[Any]] = []
        for field, column in zip(table.schema, table.columns, strict=True):
            is_marked = field.name.lower() in marked or is_pii_column(field.name)
            if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
                fn = mask_value if is_marked else mask_text
                values = [None if v is None else fn(str(v)) for v in column.to_pylist()]
                arrays.append(pa.array(values, type=pa.string()))
            elif is_marked:
                values = [None if v is None else "***" for v in column.to_pylist()]
                arrays.append(pa.array(values, type=pa.string()))
            else:
                arrays.append(column.combine_chunks())
        names = [f.name for f in table.schema]
        return pa.Table.from_arrays(arrays, names=names)
