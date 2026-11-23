"""EventBus: todo acontecimento da sessão vira um evento tipado.

As interfaces (CLI, servidor ACP) assinam o barramento; o core não sabe quem
está ouvindo.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class AssistantText(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    delta: str


class ToolCallStarted(BaseModel):
    type: Literal["tool_call_started"] = "tool_call_started"
    call_id: str
    name: str
    arguments: dict[str, Any]
    risk: str


class ToolCallFinished(BaseModel):
    type: Literal["tool_call_finished"] = "tool_call_finished"
    call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = Field(default_factory=lambda: dict[str, Any]())


class ApprovalRequested(BaseModel):
    type: Literal["approval_requested"] = "approval_requested"
    call_id: str
    name: str
    arguments: dict[str, Any]
    risk: str
    reason: str
    preview: str | None = None
    preview_kind: Literal["diff", "sql", "command", "text"] | None = None
    details: dict[str, Any] = Field(default_factory=lambda: dict[str, Any]())


class ApprovalResolved(BaseModel):
    type: Literal["approval_resolved"] = "approval_resolved"
    call_id: str
    approved: bool
    remember: bool = False


class UsageUpdated(BaseModel):
    type: Literal["usage_updated"] = "usage_updated"
    input_tokens: int
    output_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float | None


class ProviderFallback(BaseModel):
    type: Literal["provider_fallback"] = "provider_fallback"
    from_provider: str
    to_provider: str
    reason: str


class ContextCompacted(BaseModel):
    type: Literal["context_compacted"] = "context_compacted"
    messages_before: int
    messages_after: int


class SessionEnded(BaseModel):
    type: Literal["session_ended"] = "session_ended"
    reason: Literal["end", "max_steps", "budget", "cancelled", "error", "max_tokens"]
    detail: str | None = None


Event = Annotated[
    AssistantText
    | ToolCallStarted
    | ToolCallFinished
    | ApprovalRequested
    | ApprovalResolved
    | UsageUpdated
    | ProviderFallback
    | ContextCompacted
    | SessionEnded,
    Field(discriminator="type"),
]

Handler = Callable[[Any], Awaitable[None] | None]


class EventBus:
    """Barramento simples, em processo. Falha de um assinante não derruba a sessão."""

    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> Callable[[], None]:
        self._handlers.append(handler)

        def unsubscribe() -> None:
            if handler in self._handlers:
                self._handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: BaseModel) -> None:
        for handler in list(self._handlers):
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                log.exception("assinante do EventBus falhou em %s", type(event).__name__)
