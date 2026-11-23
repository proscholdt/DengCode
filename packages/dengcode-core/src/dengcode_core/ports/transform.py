"""TransformPort (opcional): modelos de transformação versionados no repositório.

Exemplo de ferramenta: projetos de modelos SQL com dependências, compilação,
execução e testes. O core só conhece esta interface.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from dengcode_core.ports.models import Frozen, LineageEdge


class ModelInfo(Frozen):
    name: str
    unique_id: str
    path: str | None = None  # arquivo do modelo, relativo à raiz do projeto de transformação
    relation: str | None = None  # "schema.tabela" materializada (minúsculas)
    materialized: str | None = None
    depends_on: list[str] = Field(default_factory=lambda: list[str]())  # nomes de modelos/fontes
    columns: list[str] = Field(default_factory=lambda: list[str]())  # colunas documentadas
    description: str | None = None


class NodeResult(Frozen):
    name: str
    status: Literal["success", "error", "fail", "warn", "skipped", "pass", "unknown"]
    message: str | None = None
    elapsed_s: float | None = None
    failures: int | None = None


class TransformRunResult(Frozen):
    success: bool
    command: str  # "run" | "test" | "compile" | ...
    selector: str
    target: str | None = None
    results: list[NodeResult] = Field(default_factory=lambda: list[NodeResult]())
    logs: str = ""  # cauda dos logs, já truncada pelo adapter


@runtime_checkable
class TransformPort(Protocol):
    name: str

    async def list_models(self, selector: str | None = None) -> list[ModelInfo]: ...

    async def compile(self, model: str) -> str: ...

    async def run(self, selector: str, *, target: str) -> TransformRunResult: ...

    async def test(self, selector: str, *, target: str) -> TransformRunResult: ...

    async def manifest_lineage(self) -> list[LineageEdge]: ...
