"""MemoryStorePort (docs/especificacoes/memoria.md).

Escopo obrigatório: toda leitura/escrita recebe ``Scope`` com workspace; não
existe consulta global entre workspaces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

from dengcode_core.errors import ScopeError
from dengcode_core.ports.models import LineageEdge


class Scope(BaseModel):
    workspace: str
    project: str | None = None
    env: str | None = None

    @field_validator("workspace")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value or not value.strip():
            raise ScopeError("escopo de memória sem workspace")
        return value.strip()


class MemoryKind(StrEnum):
    CATALOG_TABLE = "catalog_table"
    COLUMN_STATS = "column_stats"
    GLOSSARY = "glossary"
    KNOWN_ISSUE = "known_issue"
    INCIDENT = "incident"
    DECISION = "decision"


Confidence = Literal["verified", "proposed", "stale"]


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryRecord(BaseModel):
    id: str
    scope: Scope
    kind: MemoryKind
    title: str
    body: str  # markdown curto; NUNCA valores de linha ou PII
    entities: list[str] = Field(default_factory=lambda: list[str]())
    source: str = "agent"
    confidence: Confidence = "proposed"
    schema_hash: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MemoryHit(BaseModel):
    record: MemoryRecord
    score: float
    matched_by: list[str] = Field(default_factory=lambda: list[str]())  # "fts", "vetor", "grafo"


@runtime_checkable
class MemoryStorePort(Protocol):
    name: str

    async def upsert(self, rec: MemoryRecord) -> None: ...

    async def get(self, scope: Scope, id: str) -> MemoryRecord | None: ...

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        kinds: list[MemoryKind] | None = None,
        entities: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryHit]: ...

    async def list(
        self, scope: Scope, *, kinds: list[MemoryKind] | None = None, limit: int = 50
    ) -> list[MemoryRecord]: ...

    async def delete(self, scope: Scope, id: str) -> None: ...

    async def add_edges(self, scope: Scope, edges: list[LineageEdge]) -> None: ...

    async def neighbors(
        self, scope: Scope, entity: str, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]: ...
