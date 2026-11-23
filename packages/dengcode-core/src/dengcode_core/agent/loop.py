"""Loop do agente (ARQUITETURA.md §3).

1. monta o contexto; 2. chama o LLM em streaming; 3. cada tool call passa pela
política (libera, pergunta ou bloqueia); 4. executa (leituras independentes em
paralelo) e devolve resultados resumidos; 5. repete até o modelo encerrar, o
usuário cancelar ou o orçamento acabar.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from dengcode_core.context import ContextManager
from dengcode_core.errors import ConfigError, DengCodeError
from dengcode_core.events import (
    ApprovalRequested,
    ApprovalResolved,
    AssistantText,
    ContextCompacted,
    EventBus,
    SessionEnded,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from dengcode_core.llm.events import Stop, TextDelta, ToolCallEnd, ToolCallStart, Usage
from dengcode_core.llm.messages import (
    ChatRequest,
    Message,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from dengcode_core.llm.port import LLMPort
from dengcode_core.policy.permission import Decision, PermissionPolicy
from dengcode_core.session import Session, SessionStore
from dengcode_core.tools.base import Tool, ToolArgsError, ToolContext, ToolResult
from dengcode_core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

EndReason = Literal["end", "max_steps", "budget", "cancelled", "error", "max_tokens"]


@dataclass(frozen=True)
class ApprovalAnswer:
    approved: bool
    remember: bool = False


Approver = Callable[[ApprovalRequested], Awaitable[ApprovalAnswer]]


async def deny_all(_: ApprovalRequested) -> ApprovalAnswer:
    """Aprovador padrão do modo headless: recusa tudo que pede aprovação."""
    return ApprovalAnswer(approved=False)


@dataclass(frozen=True)
class ModelRoles:
    main: str
    fast: str


@dataclass(frozen=True)
class Budget:
    max_steps: int = 40
    max_tokens: int | None = None
    max_usd: float | None = None


@dataclass
class TurnResult:
    reason: EndReason
    text: str
    steps: int
    detail: str | None = None


@dataclass
class _Call:
    part: ToolCallPart
    tool: Tool[BaseModel] | None = None
    args: BaseModel | None = None
    error: str | None = None
    parallel: bool = False


@dataclass
class _StepOutput:
    text: str = ""
    calls: list[ToolCallPart] = field(default_factory=lambda: list[ToolCallPart]())
    stop: str = "end"


class Agent:
    def __init__(
        self,
        *,
        llm: LLMPort,
        models: ModelRoles,
        tools: ToolRegistry,
        policy: PermissionPolicy,
        tool_ctx: ToolContext,
        session: Session,
        system_prompt: str,
        approver: Approver = deny_all,
        bus: EventBus | None = None,
        budget: Budget | None = None,
        context: ContextManager | None = None,
        store: SessionStore | None = None,
    ) -> None:
        self.llm = llm
        self.models = models
        self.tools = tools
        self.policy = policy
        self.ctx = tool_ctx
        self.session = session
        self.system_prompt = system_prompt
        self.approver = approver
        self.bus = bus or EventBus()
        self.budget = budget or Budget()
        self.context = context or ContextManager()
        self.store = store

    # ------------------------------------------------------------------ público
    @property
    def total_cost_usd(self) -> float | None:
        return self.session.meta.cost_usd

    async def run(self, user_text: str) -> TurnResult:
        """Executa um turno completo a partir de uma mensagem do usuário."""
        profile = self.llm.profile(self.models.main)
        if self.tools and not profile.supports_tools:
            raise ConfigError(
                f"o modelo {profile.model} ({profile.provider}) não suporta ferramentas; "
                "escolha outro modelo na stack"
            )
        self.session.append(Message.text("user", user_text))
        steps = 0
        final_text: list[str] = []
        try:
            while True:
                if steps >= self.budget.max_steps:
                    return await self._end("max_steps", final_text, steps)
                if self._over_budget():
                    return await self._end("budget", final_text, steps)
                await self._maybe_compact()
                out = await self._call_llm()
                steps += 1
                if out.text:
                    final_text.append(out.text)
                parts: list[Part] = []
                if out.text:
                    parts.append(TextPart(text=out.text))
                parts.extend(out.calls)
                if parts:
                    self.session.append(Message(role="assistant", parts=parts))
                if not out.calls:
                    reason: EndReason = "max_tokens" if out.stop == "max_tokens" else "end"
                    return await self._end(reason, final_text, steps)
                results = await self._execute(out.calls)
                self.session.append(Message(role="tool", parts=list(results)))
                self._persist()
        except asyncio.CancelledError:
            self._close_dangling_calls("cancelado pelo usuário")
            await self.bus.publish(SessionEnded(reason="cancelled"))
            self._persist()
            raise
        except DengCodeError as exc:
            self._close_dangling_calls(f"erro: {exc}")
            return await self._end("error", final_text, steps, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ LLM
    def _request(self) -> ChatRequest:
        return ChatRequest(
            model=self.models.main,
            messages=[Message.text("system", self.system_prompt), *self.session.messages],
            tools=self.tools.specs(),
            cache_breakpoints=[0],
        )

    async def _maybe_compact(self) -> None:
        profile = self.llm.profile(self.models.main)
        if not self.context.needs_compaction(self._request(), profile):
            return
        before = len(self.session.messages)
        compacted = await self.context.compact(self.session.messages, self.llm, self.models.fast)
        if compacted is None:
            return
        self.session.messages = compacted
        await self.bus.publish(
            ContextCompacted(messages_before=before, messages_after=len(compacted))
        )

    async def _call_llm(self) -> _StepOutput:
        out = _StepOutput()
        names: dict[str, str] = {}
        text: list[str] = []
        async for ev in self.llm.stream(self._request()):
            if isinstance(ev, TextDelta):
                text.append(ev.text)
                await self.bus.publish(AssistantText(delta=ev.text))
            elif isinstance(ev, ToolCallStart):
                names[ev.id] = ev.name
            elif isinstance(ev, ToolCallEnd):
                out.calls.append(
                    ToolCallPart(id=ev.id, name=names.get(ev.id, "?"), arguments=ev.arguments)
                )
            elif isinstance(ev, Usage):
                await self._account(ev)
            elif isinstance(ev, Stop):
                out.stop = ev.reason
        out.text = "".join(text)
        return out

    async def _account(self, usage: Usage) -> None:
        meta = self.session.meta
        meta.input_tokens += usage.input_tokens
        meta.output_tokens += usage.output_tokens
        cost = self.llm.profile(self.models.main).cost_usd(usage.input_tokens, usage.output_tokens)
        if cost is not None:
            meta.cost_usd = (meta.cost_usd or 0.0) + cost
        await self.bus.publish(
            UsageUpdated(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_input_tokens=meta.input_tokens,
                total_output_tokens=meta.output_tokens,
                total_cost_usd=meta.cost_usd,
            )
        )

    def _over_budget(self) -> bool:
        meta = self.session.meta
        tokens = meta.input_tokens + meta.output_tokens
        if self.budget.max_tokens is not None and tokens >= self.budget.max_tokens:
            return True
        return (
            self.budget.max_usd is not None
            and meta.cost_usd is not None
            and meta.cost_usd >= self.budget.max_usd
        )

    # ------------------------------------------------------------------ ferramentas
    def _prepare(self, part: ToolCallPart) -> _Call:
        tool = self.tools.get(part.name)
        if tool is None:
            return _Call(part, error=f"ferramenta desconhecida: {part.name}")
        try:
            args = tool.parse(part.arguments)
        except ToolArgsError as exc:
            return _Call(part, tool, error=f"argumentos inválidos para {part.name}: {exc}")
        return _Call(part, tool, args, parallel=tool.parallel_safe)

    async def _execute(self, parts: list[ToolCallPart]) -> list[ToolResultPart]:
        calls = [self._prepare(p) for p in parts]
        results: dict[str, ToolResultPart] = {}
        batch: list[_Call] = []

        async def flush() -> None:
            done = await asyncio.gather(*(self._run_one(c) for c in batch))
            for c, r in zip(batch, done, strict=True):
                results[c.part.id] = r
            batch.clear()

        for call in calls:
            if call.parallel and call.error is None:
                batch.append(call)
                continue
            await flush()
            results[call.part.id] = await self._run_one(call)
        await flush()
        return [results[p.id] for p in parts]

    async def _run_one(self, call: _Call) -> ToolResultPart:
        part = call.part
        if call.error is not None or call.tool is None or call.args is None:
            return await self._finish(part, ToolResult(call.error or "erro", is_error=True), False)
        tool, args = call.tool, call.args
        request = await tool.assess(args, self.ctx)
        paths = [*request.paths_read, *request.paths_written]
        inside = all(self.ctx.inside_workspace(p) for p in paths)
        verdict = self.policy.decide(request, inside=inside)

        if verdict.decision is Decision.DENY:
            msg = f"bloqueado pela política: {verdict.reason}"
            return await self._finish(part, ToolResult(msg, is_error=True), False)

        if verdict.decision is Decision.ASK:
            preview = await tool.preview(args, self.ctx)
            ask = ApprovalRequested(
                call_id=part.id,
                name=part.name,
                arguments=part.arguments,
                risk=request.risk.value,
                reason=verdict.reason,
                preview=preview.text if preview else None,
                preview_kind=preview.kind if preview else None,
                details={
                    **(preview.details if preview else {}),
                    **request.details,
                    "warnings": list(verdict.warnings),
                },
            )
            await self.bus.publish(ask)
            answer = await self.approver(ask)
            await self.bus.publish(
                ApprovalResolved(
                    call_id=part.id, approved=answer.approved, remember=answer.remember
                )
            )
            if not answer.approved:
                msg = "o usuário recusou esta ação; não tente contorná-la, pergunte como seguir"
                return await self._finish(part, ToolResult(msg, is_error=True), False)
            if answer.remember:
                self.policy.grant(request)

        await self.bus.publish(
            ToolCallStarted(
                call_id=part.id, name=part.name, arguments=part.arguments, risk=request.risk.value
            )
        )
        try:
            result = await tool.run(args, self.ctx)
        except DengCodeError as exc:
            result = ToolResult(f"{type(exc).__name__}: {exc}", is_error=True)
        except (OSError, ValueError) as exc:
            result = ToolResult(f"falha em {part.name}: {type(exc).__name__}: {exc}", is_error=True)
        return await self._finish(part, result, tool.untrusted_output)

    async def _finish(
        self, part: ToolCallPart, result: ToolResult, untrusted: bool
    ) -> ToolResultPart:
        content = self.context.tool_output(result.content, untrusted=untrusted)
        await self.bus.publish(
            ToolCallFinished(
                call_id=part.id,
                name=part.name,
                content=result.content,
                is_error=result.is_error,
                metadata=_jsonable(result.metadata),
            )
        )
        return ToolResultPart(call_id=part.id, content=content, is_error=result.is_error)

    # ------------------------------------------------------------------ fim
    def _close_dangling_calls(self, reason: str) -> None:
        """Garante que toda tool call do histórico tenha resultado (exigência dos provedores)."""
        if not self.session.messages:
            return
        last = self.session.messages[-1]
        if last.role == "assistant" and last.tool_calls():
            results: list[Part] = [
                ToolResultPart(call_id=c.id, content=reason, is_error=True)
                for c in last.tool_calls()
            ]
            self.session.append(Message(role="tool", parts=results))

    async def _end(
        self, reason: EndReason, text: list[str], steps: int, detail: str | None = None
    ) -> TurnResult:
        await self.bus.publish(SessionEnded(reason=reason, detail=detail))
        self._persist()
        return TurnResult(reason=reason, text="\n".join(text), steps=steps, detail=detail)

    def _persist(self) -> None:
        if self.store is not None:
            try:
                self.store.save(self.session)
            except OSError:
                log.exception("não foi possível salvar a sessão %s", self.session.id)


def _jsonable(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if isinstance(v, str | int | float | bool | None)}
