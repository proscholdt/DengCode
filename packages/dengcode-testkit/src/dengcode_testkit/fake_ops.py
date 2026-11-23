"""Fakes em memória de MemoryStorePort e OrchestratorPort para testes do motor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from dengcode_core.errors import NotFoundError, PermissionDeniedError
from dengcode_core.ports.memory import MemoryHit, MemoryKind, MemoryRecord, Scope
from dengcode_core.ports.models import LineageEdge
from dengcode_core.ports.orchestrator import (
    OrchestratorCapabilities,
    PipelineInfo,
    RunInfo,
    TaskRunInfo,
)
from dengcode_core.privacy.pii import mask_text


class FakeMemoryStore:
    """Busca por palavras (sem vetores), isolada por workspace, com o validador de PII."""

    name = "fake_memory"

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], MemoryRecord] = {}
        self.edges: dict[str, list[LineageEdge]] = {}

    async def upsert(self, rec: MemoryRecord) -> None:
        if mask_text(rec.body) != rec.body or mask_text(rec.title) != rec.title:
            raise PermissionDeniedError("a memória guarda só metadados (PII detectada)")
        self.records[(rec.scope.workspace, rec.id)] = rec

    async def get(self, scope: Scope, id: str) -> MemoryRecord | None:
        return self.records.get((scope.workspace, id))

    async def search(
        self,
        scope: Scope,
        query: str,
        *,
        kinds: list[MemoryKind] | None = None,
        entities: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryHit]:
        words = {w for w in query.lower().split() if len(w) > 2}
        hits: list[MemoryHit] = []
        for (ws, _), rec in self.records.items():
            if ws != scope.workspace or (kinds and rec.kind not in kinds):
                continue
            text = f"{rec.title} {rec.body} {' '.join(rec.entities)}".lower()
            score = sum(1 for w in words if w in text)
            if entities and set(entities) & set(rec.entities):
                score += 2
            if score:
                hits.append(MemoryHit(record=rec, score=float(score), matched_by=["fts"]))
        return sorted(hits, key=lambda h: -h.score)[:limit]

    async def list(
        self, scope: Scope, *, kinds: list[MemoryKind] | None = None, limit: int = 50
    ) -> list[MemoryRecord]:
        recs = [
            r
            for (ws, _), r in self.records.items()
            if ws == scope.workspace and (not kinds or r.kind in kinds)
        ]
        return sorted(recs, key=lambda r: r.updated_at, reverse=True)[:limit]

    async def delete(self, scope: Scope, id: str) -> None:
        self.records.pop((scope.workspace, id), None)

    async def add_edges(self, scope: Scope, edges: list[LineageEdge]) -> None:
        self.edges.setdefault(scope.workspace, []).extend(edges)

    async def neighbors(
        self, scope: Scope, entity: str, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        key = "source" if direction == "down" else "target"
        return [e for e in self.edges.get(scope.workspace, []) if getattr(e, key) == entity]


class FakeOrchestrator:
    """Duas pipelines: ``carga_pedidos`` (falhou ontem) e ``relatorio_diario`` (ok)."""

    name = "fake_orchestrator"

    def __init__(self) -> None:
        yesterday = datetime.now(UTC) - timedelta(days=1)
        self.triggered: list[tuple[str, dict[str, Any] | None]] = []
        self.runs: dict[str, RunInfo] = {
            "carga_pedidos/r1": RunInfo(
                run_id="carga_pedidos/r1",
                pipeline_id="carga_pedidos",
                status="failed",
                started_at=yesterday,
                ended_at=yesterday + timedelta(minutes=3),
                failed_tasks=["validar_pedidos"],
                tasks=[
                    TaskRunInfo(task_id="extrair_pedidos", status="success"),
                    TaskRunInfo(task_id="validar_pedidos", status="failed", try_number=2),
                ],
            ),
            "relatorio_diario/r1": RunInfo(
                run_id="relatorio_diario/r1",
                pipeline_id="relatorio_diario",
                status="success",
                started_at=yesterday,
            ),
        }
        self.logs = {
            ("carga_pedidos/r1", "validar_pedidos"): (
                "INFO lendo 312 pedidos\n"
                "ERROR QualidadeError: coluna id_cliente com 12 nulos (contato: ana@x.com)\n"
                "INFO tarefa marcada como FAILED"
            ),
        }

    def capabilities(self) -> OrchestratorCapabilities:
        return OrchestratorCapabilities(
            supports_trigger=True, supports_code_definitions=True, supports_backfill=False
        )

    async def list_pipelines(self) -> list[PipelineInfo]:
        return [
            PipelineInfo(
                id="carga_pedidos",
                name="carga_pedidos",
                schedule="@daily",
                paused=False,
                definition_path="dags/carga_pedidos.py",
            ),
            PipelineInfo(id="relatorio_diario", name="relatorio_diario", schedule="@daily"),
        ]

    async def list_runs(self, pipeline_id: str, *, limit: int = 20) -> list[RunInfo]:
        if pipeline_id not in {"carga_pedidos", "relatorio_diario"}:
            raise NotFoundError(f"pipeline não encontrada: {pipeline_id}")
        return [r for r in self.runs.values() if r.pipeline_id == pipeline_id][:limit]

    async def get_run(self, run_id: str) -> RunInfo:
        if run_id not in self.runs:
            raise NotFoundError(f"execução não encontrada: {run_id}")
        return self.runs[run_id]

    async def task_logs(self, run_id: str, task_id: str, *, tail_lines: int = 500) -> str:
        text = self.logs.get((run_id, task_id))
        if text is None:
            raise NotFoundError(f"sem logs para {run_id}/{task_id}")
        return "\n".join(text.splitlines()[-tail_lines:])

    async def trigger(self, pipeline_id: str, params: dict[str, Any] | None = None) -> RunInfo:
        self.triggered.append((pipeline_id, params))
        return RunInfo(run_id=f"{pipeline_id}/manual", pipeline_id=pipeline_id, status="queued")
