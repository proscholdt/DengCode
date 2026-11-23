"""QueryEnginePort: executar SQL, explain/dry-run, custo, dialeto.

O loop NUNCA chama esta porta direto: toda execução passa pelo SqlGuard
(ferramenta ``query_run``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dengcode_core.ports.models import CostEstimate, QueryEngineCapabilities, QueryResult


@runtime_checkable
class QueryEnginePort(Protocol):
    name: str

    def capabilities(self) -> QueryEngineCapabilities: ...

    async def execute(
        self, sql: str, *, max_rows: int = 10_000, timeout_s: int = 300
    ) -> QueryResult: ...

    async def explain(self, sql: str) -> str: ...

    async def dry_run(self, sql: str) -> CostEstimate: ...

    async def cancel(self, query_id: str) -> None: ...
