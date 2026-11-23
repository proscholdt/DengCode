"""FakeStreamCompute: jobs de streaming em memória para testes.

Sem argumentos, traz três jobs de exemplo (um rodando, um parado e um que
falhou com logs de erro). Passe ``jobs`` para montar outro cenário.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar

from dengcode_core.errors import NotFoundError
from dengcode_core.ports.models import AdapterInfo, PortKind
from dengcode_core.ports.stream import StreamJobInfo


@dataclass
class FakeStreamJob:
    """Um job do fake: o ``info`` devolvido pela porta e as linhas de log."""

    info: StreamJobInfo
    logs: list[str] = field(default_factory=lambda: list[str]())


_CHECKPOINT = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)


def default_jobs() -> list[FakeStreamJob]:
    """Cenário padrão: enriquecimento rodando, agregação parada, deduplicação com falha."""
    return [
        FakeStreamJob(
            StreamJobInfo(
                id="job-enriquece-pedidos",
                name="enriquece_pedidos",
                status="running",
                lag_seconds=4.2,
                last_checkpoint_at=_CHECKPOINT,
            ),
            [
                "2026-01-15T03:00:00Z INFO checkpoint 812 concluído em 1.3 s",
                "2026-01-15T03:00:05Z INFO processadas 1250 mensagens de pedidos",
            ],
        ),
        FakeStreamJob(
            StreamJobInfo(id="job-agrega-vendas", name="agrega_vendas_hora", status="stopped"),
            ["2026-01-14T22:00:00Z INFO job parado pelo operador (savepoint gravado)"],
        ),
        FakeStreamJob(
            StreamJobInfo(
                id="job-deduplica-eventos",
                name="deduplica_eventos",
                status="failed",
                lag_seconds=5400.0,
                last_checkpoint_at=datetime(2026, 1, 15, 1, 30, tzinfo=UTC),
            ),
            [
                "2026-01-15T01:30:00Z INFO checkpoint 97 concluído em 2.1 s",
                "2026-01-15T01:31:12Z WARN estado da janela cresceu para 3.8 GB",
                "2026-01-15T01:32:40Z ERROR checkpoint 98 expirou após 600 s",
                "2026-01-15T01:32:41Z ERROR java.lang.OutOfMemoryError: Java heap space",
                "2026-01-15T01:32:41Z ERROR deduplica_eventos falhou; reinícios esgotados (3/3)",
            ],
        ),
    ]


class FakeStreamCompute:
    """Implementa ``StreamComputePort`` em memória (somente leitura)."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="fake_stream_compute", port=PortKind.STREAM_COMPUTE, version="0.1.0"
    )
    name = "fake_stream_compute"

    def __init__(self, jobs: Iterable[FakeStreamJob] | None = None) -> None:
        items = list(default_jobs() if jobs is None else jobs)
        self._jobs: dict[str, FakeStreamJob] = {}
        for job in items:
            if job.info.id in self._jobs:
                raise ValueError(f"id de job repetido no fake: {job.info.id}")
            self._jobs[job.info.id] = job

    def _get(self, job_id: str) -> FakeStreamJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"job de streaming inexistente: {job_id}")
        return job

    async def list_jobs(self) -> list[StreamJobInfo]:
        return [job.info for job in self._jobs.values()]

    async def job_status(self, job_id: str) -> StreamJobInfo:
        return self._get(job_id).info

    async def job_logs(self, job_id: str, *, tail_lines: int = 500) -> str:
        lines = self._get(job_id).logs
        if tail_lines <= 0:
            return ""
        return "\n".join(lines[-tail_lines:])
