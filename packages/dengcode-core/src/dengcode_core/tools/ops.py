"""Ferramentas de orquestração, streaming e memória (fase 5).

Logs, amostras e mensagens são conteúdo não confiável e passam pelo mascarador
do loop antes de chegar ao LLM.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from dengcode_core.ports.memory import MemoryKind, MemoryRecord, MemoryStorePort, Scope
from dengcode_core.ports.orchestrator import OrchestratorPort, RunInfo
from dengcode_core.ports.stream import StreamComputePort, StreamPort
from dengcode_core.tools.base import APPROVED_KEY, Preview, Risk, Tool, ToolContext, ToolResult


def _when(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "?"


def _run_line(r: RunInfo) -> str:
    failed = f" · tarefas com falha: {', '.join(r.failed_tasks)}" if r.failed_tasks else ""
    return (
        f"- {r.run_id}: {r.status} · início {_when(r.started_at)} · fim {_when(r.ended_at)}{failed}"
    )


# ----------------------------------------------------------------- orquestração
class Empty(BaseModel):
    pass


class PipelineList(Tool[Empty]):
    name: ClassVar[str] = "pipeline_list"
    description: ClassVar[str] = "Lista as pipelines do orquestrador (agenda, pausada, arquivo)."
    risk: ClassVar[Risk] = Risk.READ
    Args = Empty
    parallel_safe: ClassVar[bool] = True

    def __init__(self, orch: OrchestratorPort) -> None:
        self.orch = orch

    async def run(self, args: Empty, ctx: ToolContext) -> ToolResult:
        items = await self.orch.list_pipelines()
        lines = [
            f"- {p.id}: agenda {p.schedule or '-'} · "
            f"{'pausada' if p.paused else 'ativa'} · arquivo {p.definition_path or '-'}"
            for p in items
        ]
        return ToolResult("\n".join(lines) or "(nenhuma pipeline)")


class RunsArgs(BaseModel):
    pipeline_id: str = Field(description="Id da pipeline.")
    limit: int = Field(default=10, ge=1, le=100)


class PipelineRuns(Tool[RunsArgs]):
    name: ClassVar[str] = "pipeline_runs"
    description: ClassVar[str] = "Execuções recentes de uma pipeline (status, horários, falhas)."
    risk: ClassVar[Risk] = Risk.READ
    Args = RunsArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, orch: OrchestratorPort) -> None:
        self.orch = orch

    async def run(self, args: RunsArgs, ctx: ToolContext) -> ToolResult:
        runs = await self.orch.list_runs(args.pipeline_id, limit=args.limit)
        return ToolResult("\n".join(_run_line(r) for r in runs) or "(nenhuma execução)")


class RunArgs(BaseModel):
    run_id: str = Field(description="Id da execução (como aparece em pipeline_runs).")


class PipelineRunDetail(Tool[RunArgs]):
    name: ClassVar[str] = "pipeline_run_detail"
    description: ClassVar[str] = "Detalhe de uma execução: status de cada tarefa e tentativas."
    risk: ClassVar[Risk] = Risk.READ
    Args = RunArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, orch: OrchestratorPort) -> None:
        self.orch = orch

    async def run(self, args: RunArgs, ctx: ToolContext) -> ToolResult:
        r = await self.orch.get_run(args.run_id)
        lines = [_run_line(r).removeprefix("- "), "tarefas:"]
        lines += [
            f"- {t.task_id}: {t.status}" + (f" (tentativa {t.try_number})" if t.try_number else "")
            for t in r.tasks
        ]
        return ToolResult("\n".join(lines))


class LogsArgs(BaseModel):
    run_id: str = Field(description="Id da execução.")
    task_id: str = Field(description="Tarefa.")
    tail_lines: int = Field(default=200, ge=10, le=2000, description="Últimas N linhas.")


class PipelineTaskLogs(Tool[LogsArgs]):
    name: ClassVar[str] = "pipeline_task_logs"
    description: ClassVar[str] = "Últimas linhas do log de uma tarefa (para diagnosticar falhas)."
    risk: ClassVar[Risk] = Risk.READ
    Args = LogsArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, orch: OrchestratorPort) -> None:
        self.orch = orch

    async def run(self, args: LogsArgs, ctx: ToolContext) -> ToolResult:
        text = await self.orch.task_logs(args.run_id, args.task_id, tail_lines=args.tail_lines)
        return ToolResult(text or "(log vazio)")


class TriggerArgs(BaseModel):
    pipeline_id: str = Field(description="Pipeline a disparar.")
    params: dict[str, Any] | None = Field(default=None, description="Parâmetros da execução.")


class PipelineTrigger(Tool[TriggerArgs]):
    name: ClassVar[str] = "pipeline_trigger"
    description: ClassVar[str] = (
        "Dispara uma execução da pipeline (escrita remota; pede aprovação)."
    )
    risk: ClassVar[Risk] = Risk.WRITE_REMOTE
    Args = TriggerArgs

    def __init__(self, orch: OrchestratorPort) -> None:
        self.orch = orch

    async def preview(self, args: TriggerArgs, ctx: ToolContext) -> Preview | None:
        params = json.dumps(args.params or {}, ensure_ascii=False)
        return Preview("command", f"disparar {args.pipeline_id} com {params}")

    async def run(self, args: TriggerArgs, ctx: ToolContext) -> ToolResult:
        r = await self.orch.trigger(args.pipeline_id, args.params)
        return ToolResult(f"execução criada: {r.run_id} ({r.status})")


# ----------------------------------------------------------------- streaming
class StreamTopics(Tool[Empty]):
    name: ClassVar[str] = "stream_list_topics"
    description: ClassVar[str] = "Lista tópicos de streaming (partições, retenção)."
    risk: ClassVar[Risk] = Risk.READ
    Args = Empty
    parallel_safe: ClassVar[bool] = True

    def __init__(self, stream: StreamPort) -> None:
        self.stream = stream

    async def run(self, args: Empty, ctx: ToolContext) -> ToolResult:
        topics = await self.stream.list_topics()
        lines = [
            f"- {t.name}: {t.partitions} partição(ões) · retenção "
            f"{t.retention_hours if t.retention_hours is not None else '?'} h"
            for t in topics
        ]
        return ToolResult("\n".join(lines) or "(nenhum tópico)")


class SampleArgs(BaseModel):
    topic: str = Field(description="Tópico.")
    max_messages: int = Field(default=10, ge=1, le=100)


class StreamSample(Tool[SampleArgs]):
    name: ClassVar[str] = "stream_sample"
    description: ClassVar[str] = (
        "Lê as mensagens mais recentes de um tópico, só leitura (não confirma offsets; PII "
        "mascarada)."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = SampleArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, stream: StreamPort) -> None:
        self.stream = stream

    async def run(self, args: SampleArgs, ctx: ToolContext) -> ToolResult:
        msgs = await self.stream.sample(args.topic, max_messages=args.max_messages)
        lines = [json.dumps(m, ensure_ascii=False, default=str) for m in msgs]
        return ToolResult("\n".join(lines) or "(sem mensagens)")


class LagArgs(BaseModel):
    topic: str = Field(description="Tópico.")
    group: str = Field(description="Grupo de consumidores.")


class StreamLag(Tool[LagArgs]):
    name: ClassVar[str] = "stream_consumer_lag"
    description: ClassVar[str] = "Atraso (lag) de um grupo de consumidores por partição."
    risk: ClassVar[Risk] = Risk.READ
    Args = LagArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, stream: StreamPort) -> None:
        self.stream = stream

    async def run(self, args: LagArgs, ctx: ToolContext) -> ToolResult:
        lag = await self.stream.consumer_lag(args.topic, args.group)
        total = sum(lag.values())
        lines = [f"lag total: {total}"] + [f"- partição {p}: {n}" for p, n in sorted(lag.items())]
        return ToolResult("\n".join(lines))


class TopicArgs(BaseModel):
    topic: str = Field(description="Tópico.")


class StreamSchema(Tool[TopicArgs]):
    name: ClassVar[str] = "stream_schema"
    description: ClassVar[str] = (
        "Schema registrado das mensagens de um tópico (se houver registry)."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = TopicArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, stream: StreamPort) -> None:
        self.stream = stream

    async def run(self, args: TopicArgs, ctx: ToolContext) -> ToolResult:
        schema = await self.stream.schema(args.topic)
        if schema is None:
            return ToolResult("sem schema registrado para este tópico")
        return ToolResult(json.dumps(schema, ensure_ascii=False, indent=2))


class StreamJobs(Tool[Empty]):
    name: ClassVar[str] = "stream_jobs"
    description: ClassVar[str] = "Jobs de processamento de streaming: status, atraso, checkpoint."
    risk: ClassVar[Risk] = Risk.READ
    Args = Empty
    parallel_safe: ClassVar[bool] = True

    def __init__(self, compute: StreamComputePort) -> None:
        self.compute = compute

    async def run(self, args: Empty, ctx: ToolContext) -> ToolResult:
        jobs = await self.compute.list_jobs()
        lines = [
            f"- {j.id} ({j.name}): {j.status} · atraso "
            f"{j.lag_seconds if j.lag_seconds is not None else '?'} s · checkpoint "
            f"{_when(j.last_checkpoint_at)}"
            for j in jobs
        ]
        return ToolResult("\n".join(lines) or "(nenhum job)")


class JobArgs(BaseModel):
    job_id: str = Field(description="Id do job.")
    tail_lines: int = Field(default=200, ge=10, le=2000)


class StreamJobLogs(Tool[JobArgs]):
    name: ClassVar[str] = "stream_job_logs"
    description: ClassVar[str] = "Últimas linhas do log de um job de streaming."
    risk: ClassVar[Risk] = Risk.READ
    Args = JobArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, compute: StreamComputePort) -> None:
        self.compute = compute

    async def run(self, args: JobArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult(await self.compute.job_logs(args.job_id, tail_lines=args.tail_lines))


# ----------------------------------------------------------------- memória
class MemorySearchArgs(BaseModel):
    query: str = Field(description="O que procurar (palavras do problema, tabela, pipeline).")
    kinds: list[MemoryKind] | None = Field(default=None, description="Filtrar por tipos.")
    entities: list[str] | None = Field(default=None, description="Tabelas/colunas relacionadas.")
    limit: int = Field(default=8, ge=1, le=30)


class MemorySearch(Tool[MemorySearchArgs]):
    name: ClassVar[str] = "memory_search"
    description: ClassVar[str] = (
        "Busca na memória do workspace: incidentes passados, problemas conhecidos, glossário, "
        "decisões e metadados de catálogo. Memória é pista, não verdade: confira ao vivo."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = MemorySearchArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, memory: MemoryStorePort, scope: Scope) -> None:
        self.memory = memory
        self.scope = scope

    async def run(self, args: MemorySearchArgs, ctx: ToolContext) -> ToolResult:
        hits = await self.memory.search(
            self.scope, args.query, kinds=args.kinds, entities=args.entities, limit=args.limit
        )
        lines: list[str] = []
        for h in hits:
            r = h.record
            body = r.body if len(r.body) <= 600 else r.body[:599] + "…"
            lines.append(
                f"## [{r.kind.value} · {r.confidence}] {r.title} (id {r.id}, "
                f"{r.updated_at:%Y-%m-%d})\nentidades: {', '.join(r.entities) or '-'}\n"
                f"{body}"
            )
        return ToolResult("\n\n".join(lines) or "(nada na memória)")


class MemoryProposeArgs(BaseModel):
    kind: MemoryKind = Field(description="incident, known_issue, glossary ou decision.")
    title: str = Field(description="Título curto.")
    body: str = Field(
        description="Markdown curto. Para incidentes: sintoma, causa, solução. "
        "Só metadados: nunca valores de linhas ou dados pessoais."
    )
    entities: list[str] = Field(
        default_factory=lambda: list[str](), description="Tabelas, colunas e pipelines envolvidas."
    )


class MemoryPropose(Tool[MemoryProposeArgs]):
    name: ClassVar[str] = "memory_propose"
    description: ClassVar[str] = (
        "Propõe um registro para a memória do workspace (incidente, problema conhecido, termo "
        "de glossário, decisão). O usuário confirma; sem confirmação fica como 'proposed'."
    )
    risk: ClassVar[Risk] = Risk.WRITE_LOCAL
    Args = MemoryProposeArgs
    untrusted_output: ClassVar[bool] = False

    def __init__(self, memory: MemoryStorePort, scope: Scope) -> None:
        self.memory = memory
        self.scope = scope

    async def preview(self, args: MemoryProposeArgs, ctx: ToolContext) -> Preview | None:
        text = (
            f"[{args.kind.value}] {args.title}\nentidades: {', '.join(args.entities) or '-'}"
            f"\n\n{args.body}"
        )
        return Preview("text", text)

    async def run(self, args: MemoryProposeArgs, ctx: ToolContext) -> ToolResult:
        if args.kind in (MemoryKind.CATALOG_TABLE, MemoryKind.COLUMN_STATS):
            return ToolResult(
                "metadados de catálogo são gravados pela sincronização, não aqui", is_error=True
            )
        confirmed = bool(ctx.extras.get(APPROVED_KEY))
        rec = MemoryRecord(
            id=f"{args.kind.value}-{uuid.uuid4().hex[:8]}",
            scope=self.scope,
            kind=args.kind,
            title=args.title,
            body=args.body,
            entities=[e.lower() for e in args.entities],
            source="agent",
            confidence="verified" if confirmed else "proposed",
        )
        await self.memory.upsert(rec)
        state = "confirmado pelo usuário" if confirmed else "proposto (aguarda confirmação)"
        return ToolResult(f"memória gravada: {rec.id} · {state}")


def ops_tools(
    *,
    orchestrator: OrchestratorPort | None,
    stream: StreamPort | None,
    stream_compute: StreamComputePort | None,
    memory: MemoryStorePort | None,
    scope: Scope,
) -> list[Tool[Any]]:
    tools: list[Tool[Any]] = []
    if orchestrator is not None:
        tools += [
            PipelineList(orchestrator),
            PipelineRuns(orchestrator),
            PipelineRunDetail(orchestrator),
            PipelineTaskLogs(orchestrator),
        ]
        if orchestrator.capabilities().supports_trigger:
            tools.append(PipelineTrigger(orchestrator))
    if stream is not None:
        tools += [StreamTopics(stream), StreamSample(stream), StreamLag(stream)]
        if stream.capabilities().supports_schema_registry:
            tools.append(StreamSchema(stream))
    if stream_compute is not None:
        tools += [StreamJobs(stream_compute), StreamJobLogs(stream_compute)]
    if memory is not None:
        tools += [MemorySearch(memory, scope), MemoryPropose(memory, scope)]
    return tools
