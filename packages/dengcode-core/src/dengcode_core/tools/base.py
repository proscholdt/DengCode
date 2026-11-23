"""Contrato de uma ferramenta: spec + risco + handler + resumidor."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ValidationError

from dengcode_core.llm.messages import ToolSpec

if TYPE_CHECKING:
    from dengcode_core.checkpoints import CheckpointStore


class Risk(StrEnum):
    READ = "read"
    WRITE_LOCAL = "write_local"
    EXECUTE = "execute"
    WRITE_REMOTE = "write_remote"
    SQL = "sql"


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


@dataclass
class Preview:
    """O que a interface mostra ao pedir aprovação."""

    kind: Literal["diff", "sql", "command", "text"]
    text: str
    details: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


@dataclass
class ActionRequest:
    """Descrição neutra da ação, para a política decidir."""

    tool: str
    risk: Risk
    paths_read: list[Path] = field(default_factory=lambda: list[Path]())
    paths_written: list[Path] = field(default_factory=lambda: list[Path]())
    command: str | None = None
    # preenchidos por ferramentas de SQL (fase 3): análise e custo
    details: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    @property
    def grant_key(self) -> str:
        """Chave usada pelo "sempre nesta sessão"."""
        kind = self.details.get("sql_kind")
        return f"{self.tool}:{kind}" if kind else self.tool


@dataclass
class ToolContext:
    workspace_root: Path
    session_id: str
    env: str = "dev"
    checkpoints: CheckpointStore | None = None
    extras: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    def resolve(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        return (path if path.is_absolute() else self.workspace_root / path).resolve()

    def inside_workspace(self, path: Path) -> bool:
        root = self.workspace_root.resolve()
        return path == root or path.is_relative_to(root)


APPROVED_KEY = "approved_by_user"
"""``ToolContext.extras``: o loop marca True quando o usuário aprovou a chamada atual."""

INVALID_JSON_KEY = "__invalid_json__"
"""Chave que os adapters usam quando o provedor devolve argumentos que não são JSON."""


class ToolArgsError(ValueError):
    """Argumentos inválidos vindos do modelo."""


class Tool[A: BaseModel](ABC):
    """Base das ferramentas. ``Args`` é um modelo pydantic que vira JSON Schema."""

    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[Risk]
    Args: type[A]
    parallel_safe: ClassVar[bool] = False
    untrusted_output: ClassVar[bool] = True

    @property
    def spec(self) -> ToolSpec:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, json_schema=schema)

    def parse(self, arguments: dict[str, Any]) -> A:
        if INVALID_JSON_KEY in arguments:
            raise ToolArgsError(f"argumentos não são JSON válido: {arguments[INVALID_JSON_KEY]}")
        try:
            return self.Args.model_validate(arguments)
        except ValidationError as exc:
            raise ToolArgsError(str(exc)) from exc

    async def assess(self, args: A, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(tool=self.name, risk=self.risk)

    async def preview(self, args: A, ctx: ToolContext) -> Preview | None:
        return None

    async def on_blocked(self, args: A, request: ActionRequest, decision: str, reason: str) -> None:
        """Chamado quando a política nega ou o usuário recusa (ex.: para auditar)."""
        return None

    @abstractmethod
    async def run(self, args: A, ctx: ToolContext) -> ToolResult: ...
