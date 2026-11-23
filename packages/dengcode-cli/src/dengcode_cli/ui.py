"""Renderização de eventos no terminal (Rich) e aprovação interativa."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from dengcode_core.agent.loop import ApprovalAnswer
from dengcode_core.events import (
    ApprovalRequested,
    AssistantText,
    ContextCompacted,
    ProviderFallback,
    SessionEnded,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)

MAX_PREVIEW_LINES = 80


def short_args(args: dict[str, Any], limit: int = 100) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_preview(console: Console, kind: str | None, text: str | None) -> None:
    if not text:
        return
    lines = text.splitlines()
    if len(lines) > MAX_PREVIEW_LINES:
        omitted = len(lines) - MAX_PREVIEW_LINES
        text = "\n".join(lines[:MAX_PREVIEW_LINES]) + f"\n… [{omitted} linhas omitidas]"
    lexer = {"diff": "diff", "sql": "sql", "command": "bash"}.get(kind or "", "text")
    console.print(Syntax(text, lexer, theme="ansi_dark", word_wrap=True))


class ChatRenderer:
    """Assinante do EventBus que desenha a sessão no terminal."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self._streaming = False
        self.total_cost: float | None = None
        self.total_in = 0
        self.total_out = 0

    def _break(self) -> None:
        if self._streaming:
            self.console.print()
            self._streaming = False

    def __call__(self, event: BaseModel) -> None:
        if isinstance(event, AssistantText):
            self.console.print(event.delta, end="", markup=False, highlight=False)
            self._streaming = True
        elif isinstance(event, ToolCallStarted):
            self._break()
            self.console.print(Text(f"▶ {event.name} {short_args(event.arguments)}", style="cyan"))
        elif isinstance(event, ToolCallFinished):
            self._break()
            mark, style = ("✗", "red") if event.is_error else ("✓", "green")
            first = (event.content.strip().splitlines() or [""])[0][:120]
            self.console.print(Text(f"  {mark} {event.name}: {first}", style=style))
        elif isinstance(event, UsageUpdated):
            self.total_cost = event.total_cost_usd
            self.total_in = event.total_input_tokens
            self.total_out = event.total_output_tokens
        elif isinstance(event, ProviderFallback):
            self._break()
            self.console.print(
                f"[yellow]⚠ {event.from_provider} falhou; usando {event.to_provider}[/yellow]"
            )
        elif isinstance(event, ContextCompacted):
            self._break()
            self.console.print(
                f"[dim]contexto compactado: {event.messages_before} → "
                f"{event.messages_after} mensagens[/dim]"
            )
        elif isinstance(event, SessionEnded):
            self._break()
            if event.reason not in ("end", "cancelled"):
                detail = f": {event.detail}" if event.detail else ""
                self.console.print(f"[yellow]turno encerrado ({event.reason}){detail}[/yellow]")

    def cost_line(self) -> str:
        cost = f"US$ {self.total_cost:.4f}" if self.total_cost is not None else "custo n/d"
        return f"tokens: {self.total_in} entrada / {self.total_out} saída · {cost}"


Ask = Callable[[str], str]


class InteractiveApprover:
    """Pergunta s/n/sempre no terminal. A pergunta roda numa thread (não bloqueia o loop)."""

    def __init__(self, console: Console, ask: Ask | None = None) -> None:
        self.console = console
        self._ask = ask or (lambda prompt: console.input(prompt))

    async def __call__(self, req: ApprovalRequested) -> ApprovalAnswer:
        self.console.print()
        title = f"Aprovação: {req.name} · risco {req.risk}"
        body = Text(req.reason)
        warnings = [str(w) for w in req.details.get("warnings", [])]
        for w in warnings:
            body.append(f"\n⚠ {w}", style="bold yellow")
        self.console.print(Panel(body, title=title, border_style="yellow"))
        render_preview(self.console, req.preview_kind, req.preview)
        while True:
            raw = await asyncio.to_thread(
                self._ask, r"[bold]Aprovar?[/bold] \[s]im / \[n]ão / sempre nesta sessão \[t]: "
            )
            answer = raw.strip().lower()
            if answer in ("s", "sim", "y", "yes"):
                return ApprovalAnswer(approved=True)
            if answer in ("t", "sempre", "a", "always"):
                return ApprovalAnswer(approved=True, remember=True)
            if answer in ("n", "não", "nao", "no", ""):
                return ApprovalAnswer(approved=False)
            self.console.print("responda s, n ou t")


class AutoApprover:
    """``--yes`` no modo headless: aprova o que a política deixou como ASK (nunca DENY)."""

    async def __call__(self, req: ApprovalRequested) -> ApprovalAnswer:
        return ApprovalAnswer(approved=True)
