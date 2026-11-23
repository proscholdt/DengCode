"""Registros de memória derivados do catálogo (só metadados, nunca valores)."""

from __future__ import annotations

from dengcode_core.ports.memory import MemoryKind, MemoryRecord, Scope
from dengcode_core.ports.models import TableInfo
from dengcode_core.privacy.pii import is_pii_column


def table_record_id(qualified: str) -> str:
    return f"table:{qualified.lower()}"


def table_record(info: TableInfo, scope: Scope, source: str) -> MemoryRecord:
    cols = ", ".join(
        f"{c.name} ({c.data_type})" + (" [PII]" if is_pii_column(c.name, c.tags) else "")
        for c in info.columns
    )
    rows = info.row_count if info.row_count is not None else "?"
    body = f"colunas: {cols}\nlinhas: {rows}\nformato: {info.table_format or '-'}"
    if info.description:
        body += f"\ndescrição: {info.description}"
    qualified = info.ref.qualified().lower()
    return MemoryRecord(
        id=table_record_id(qualified),
        scope=scope,
        kind=MemoryKind.CATALOG_TABLE,
        title=f"tabela {qualified}",
        body=body,
        entities=[qualified, *[f"{qualified}.{c.name.lower()}" for c in info.columns]],
        source=f"catalog:{source}",
        confidence="verified",
        schema_hash=info.schema_hash,
    )
