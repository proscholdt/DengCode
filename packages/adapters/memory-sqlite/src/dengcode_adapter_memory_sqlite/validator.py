"""Validador "só metadados" (regra 2 de docs/especificacoes/memoria.md).

A memória guarda pistas sobre o ambiente (schemas, problemas conhecidos,
decisões), nunca valores de linha nem PII. Antes de gravar, o registro passa
por dois filtros:

1. **PII**: título, corpo e entidades não podem conter nada que o
   ``mask_text`` do core mascararia (e-mail, CPF, CNPJ, telefone, cartão).
2. **Linhas de dados**: o corpo não pode parecer um trecho de resultado —
   três ou mais linhas com quatro ou mais campos separados pelo mesmo
   separador (vírgula, ponto e vírgula, tab ou barra vertical), ou uma tabela
   markdown com três ou mais linhas de dados.
"""

from __future__ import annotations

import re

from dengcode_core.errors import PermissionDeniedError
from dengcode_core.ports.memory import MemoryRecord
from dengcode_core.privacy.pii import mask_text

SEPARATORS = (",", ";", "\t", "|")
MIN_FIELDS = 4
MIN_ROWS = 3

_MD_DIVIDER = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")

_PREFIX = "a memória guarda só metadados"


def check_metadata_only(rec: MemoryRecord) -> None:
    """Levanta ``PermissionDeniedError`` se o registro carregar PII ou dados."""
    fields = {"o título": rec.title, "o corpo": rec.body}
    for i, entity in enumerate(rec.entities):
        fields[f"a entidade #{i + 1}"] = entity
    for label, text in fields.items():
        if mask_text(text) != text:
            raise PermissionDeniedError(
                f"{_PREFIX}: {label} parece conter PII (e-mail, CPF, CNPJ, telefone ou "
                "cartão); descreva o padrão sem copiar valores"
            )
    if looks_like_data_rows(rec.body) or looks_like_data_rows(rec.title):
        raise PermissionDeniedError(
            f"{_PREFIX}: o texto parece conter linhas de dados; resuma o achado "
            "(colunas, contagens, padrão observado) sem copiar valores"
        )


def looks_like_data_rows(text: str) -> bool:
    """Heurística: o texto parece um trecho de CSV/TSV/tabela com valores."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < MIN_ROWS:
        return False
    if markdown_table_rows(lines) >= MIN_ROWS:
        return True
    for sep in SEPARATORS:
        rows = sum(1 for line in lines if _field_count(line, sep) >= MIN_FIELDS)
        if rows >= MIN_ROWS:
            return True
    return False


def markdown_table_rows(lines: list[str]) -> int:
    """Maior número de linhas de dados de uma tabela markdown (após o ``|---|``)."""
    best = 0
    current: int | None = None
    for line in lines:
        if _MD_DIVIDER.match(line) and "-" in line:
            current = 0
            continue
        if current is not None and "|" in line:
            current += 1
            best = max(best, current)
        else:
            current = None
    return best


def _field_count(line: str, sep: str) -> int:
    if sep not in line:
        return 0
    if _MD_DIVIDER.match(line):
        return 0
    if sep == "|":
        line = line.strip("|")
    return len(line.split(sep))
