"""Leitura do JSON de metadados de uma tabela Iceberg (spec de formato v1/v2/v3).

Só o que o catálogo do DengCode precisa: schema atual (colunas, tipos,
obrigatoriedade, ``doc``), localização, comentário e o total de registros do
snapshot atual (``summary["total-records"]``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from dengcode_core.errors import AdapterError
from dengcode_core.ports.models import ColumnInfo

JsonDict = Mapping[str, Any]


def render_type(kind: Any) -> str:
    """Tipo Iceberg como texto: primitivos como vêm; aninhados em notação ``<...>``.

    ``{"type": "list", "element": "string"}`` → ``list<string>``;
    ``{"type": "map", "key": "string", "value": "long"}`` → ``map<string, long>``;
    ``{"type": "struct", "fields": [...]}`` → ``struct<a: int, b: string>``.
    """
    if isinstance(kind, str):
        return kind
    if isinstance(kind, Mapping):
        name = kind.get("type")
        if name == "list":
            return f"list<{render_type(kind.get('element'))}>"
        if name == "map":
            return f"map<{render_type(kind.get('key'))}, {render_type(kind.get('value'))}>"
        if name == "struct":
            fields = kind.get("fields") or []
            inner = ", ".join(f"{f.get('name')}: {render_type(f.get('type'))}" for f in fields if f)
            return f"struct<{inner}>"
        if isinstance(name, str):
            return name
    raise AdapterError(f"iceberg-rest: tipo de coluna desconhecido nos metadados: {kind!r}")


def current_schema(metadata: JsonDict) -> JsonDict:
    """Schema atual: ``schemas[current-schema-id]`` (v2+) ou ``schema`` (v1)."""
    schemas = metadata.get("schemas") or []
    current_id = metadata.get("current-schema-id")
    for schema in schemas:
        if schema.get("schema-id") == current_id:
            return schema
    legacy = metadata.get("schema")
    if isinstance(legacy, Mapping):
        return legacy
    if schemas:
        return schemas[-1]
    raise AdapterError("iceberg-rest: metadados da tabela sem schema")


def columns_from_schema(schema: JsonDict) -> list[ColumnInfo]:
    out: list[ColumnInfo] = []
    for f in schema.get("fields") or []:
        doc = f.get("doc")
        out.append(
            ColumnInfo(
                name=str(f["name"]),
                data_type=render_type(f.get("type")),
                nullable=not bool(f.get("required", False)),
                description=str(doc) if doc else None,
            )
        )
    return out


def row_count(metadata: JsonDict) -> int | None:
    """Registros do snapshot atual; tabela sem snapshot = vazia (0)."""
    snapshot_id = metadata.get("current-snapshot-id")
    if snapshot_id is None or snapshot_id == -1:
        return 0
    for snapshot in metadata.get("snapshots") or []:
        if snapshot.get("snapshot-id") == snapshot_id:
            total = (snapshot.get("summary") or {}).get("total-records")
            try:
                return None if total is None else int(total)
            except (TypeError, ValueError):
                return None
    return None  # snapshot atual não veio na resposta
