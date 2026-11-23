"""DTOs compartilhados pelas portas (pydantic, imutáveis)."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from dengcode_core.ports.secrets import SecretsPort


class PortKind(StrEnum):
    """Conceitos que um adapter pode implementar."""

    LLM = "llm"
    EMBEDDING = "embedding"
    STORAGE = "storage"
    QUERY_ENGINE = "query_engine"
    CATALOG = "catalog"
    ORCHESTRATOR = "orchestrator"
    TRANSFORM = "transform"
    STREAM = "stream"
    STREAM_COMPUTE = "stream_compute"
    SECRETS = "secrets"
    MEMORY_STORE = "memory_store"


class Frozen(BaseModel):
    """Base de todos os DTOs: imutável e sem campos extras."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AdapterInfo(Frozen):
    """Metadados que todo adapter declara como ``info`` de classe."""

    name: str
    port: PortKind
    version: str
    knowledge_pack: str | None = None


@dataclass(frozen=True)
class AdapterContext:
    """O que o motor entrega a cada adapter ao construí-lo."""

    workspace: str
    env: str = "dev"
    secrets: SecretsPort | None = None
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("dengcode.adapter"))
    cache_dir: Path | None = None
    project_dir: Path | None = None
    # adapters já construídos nesta stack, por porta (ex.: o catálogo pode
    # reaproveitar a conexão do motor de consulta do mesmo pacote)
    peers: Mapping[str, Any] = field(default_factory=lambda: dict[str, Any]())


class StorageCapabilities(Frozen):
    supports_write: bool
    supports_list_recursive: bool
    table_formats: frozenset[str]


class ObjectInfo(Frozen):
    uri: str
    size_bytes: int | None
    modified_at: datetime | None
    is_dir: bool


# --------------------------------------------------------------------- consulta
class QueryEngineCapabilities(Frozen):
    sql_dialect: str
    supports_dry_run: bool
    supports_explain: bool
    supports_cost_estimate: bool
    supports_transactions: bool
    supports_cancel: bool


class CostEstimate(Frozen):
    bytes_scanned: int | None = None
    credits: float | None = None
    usd: float | None = None
    confidence: Literal["exact", "estimate", "unknown"] = "unknown"


class ColumnInfo(Frozen):
    name: str
    data_type: str
    nullable: bool | None = None
    description: str | None = None
    tags: frozenset[str] = frozenset()


class QueryResult(Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    query_id: str | None
    columns: list[ColumnInfo]
    preview: pa.Table
    total_rows: int | None
    result_path: Path | None = None
    elapsed_ms: int
    cost: CostEstimate | None = None


# --------------------------------------------------------------------- catálogo
class CatalogCapabilities(Frozen):
    supports_lineage: bool
    supports_column_stats: bool
    supports_tags: bool


class TableRef(Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    catalog: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    name: str

    @classmethod
    def parse(cls, text: str) -> TableRef:
        parts = [p.strip().strip('"`[]') for p in text.split(".") if p.strip()]
        if not parts or len(parts) > 3:
            raise ValueError(f"nome de tabela inválido: {text!r}")
        padded: list[str | None] = [None] * (3 - len(parts)) + list(parts)
        return cls(catalog=padded[0], schema=padded[1], name=str(padded[2]))

    def qualified(self) -> str:
        return ".".join(p for p in (self.catalog, self.schema_, self.name) if p)


class TableInfo(Frozen):
    ref: TableRef
    columns: list[ColumnInfo]
    table_format: str | None = None
    location: str | None = None
    row_count: int | None = None
    description: str | None = None
    schema_hash: str


def schema_hash(columns: list[ColumnInfo]) -> str:
    """Hash estável de nomes + tipos (ordem importa)."""
    text = "|".join(f"{c.name.lower()}:{c.data_type.lower()}" for c in columns)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ColumnStats(Frozen):
    column: str
    null_fraction: float | None = None
    distinct_count: int | None = None
    min: str | None = None
    max: str | None = None


class LineageEdge(Frozen):
    source: str
    target: str
    via: str | None = None
