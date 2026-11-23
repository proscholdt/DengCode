"""CatalogPort: tabelas, colunas, estatísticas e lineage."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from dengcode_core.ports.models import (
    CatalogCapabilities,
    ColumnStats,
    LineageEdge,
    TableInfo,
    TableRef,
)


@runtime_checkable
class CatalogPort(Protocol):
    name: str

    def capabilities(self) -> CatalogCapabilities: ...

    async def list_schemas(self, catalog: str | None = None) -> list[str]: ...

    async def list_tables(self, schema: str, *, catalog: str | None = None) -> list[TableRef]: ...

    async def describe(self, ref: TableRef) -> TableInfo: ...

    async def column_stats(
        self, ref: TableRef, columns: list[str] | None = None
    ) -> list[ColumnStats]: ...

    async def lineage(
        self, ref: TableRef, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]: ...
