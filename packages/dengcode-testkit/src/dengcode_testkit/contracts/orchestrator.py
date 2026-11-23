"""Contrato da OrchestratorPort.

O adapter herda ``OrchestratorContract`` numa classe ``Test...`` e fornece:

- ``orchestrator``: o adapter apontando para respostas gravadas ou para um
  orquestrador local/container;
- ``failing_pipeline_id``: um pipeline com pelo menos um run que falhou (com
  ao menos uma task em falha e com log).

O contrato só lê: nunca dispara runs (``trigger`` é escrita remota).
"""

from __future__ import annotations

from typing import get_args

import pytest

from dengcode_core.errors import NotFoundError
from dengcode_core.ports.orchestrator import (
    OrchestratorCapabilities,
    OrchestratorPort,
    RunInfo,
    RunStatus,
)

ALLOWED_STATUSES = frozenset(get_args(RunStatus))
UNKNOWN_PIPELINE = "pipeline_que_nao_existe_xyz"


async def latest_failed_run(orchestrator: OrchestratorPort, pipeline_id: str) -> RunInfo:
    """Run com falha mais recente de ``pipeline_id`` (falha o teste se não houver)."""
    runs = await orchestrator.list_runs(pipeline_id)
    failed = [r for r in runs if r.status == "failed"]
    assert failed, f"{pipeline_id} não tem run com falha entre os mais recentes"
    return failed[0]


class OrchestratorContract:
    """Fixtures: ``orchestrator``; ``failing_pipeline_id``."""

    def test_implements_port(self, orchestrator: OrchestratorPort) -> None:
        assert isinstance(orchestrator, OrchestratorPort)
        assert orchestrator.name
        assert isinstance(orchestrator.capabilities(), OrchestratorCapabilities)

    async def test_lists_failing_pipeline(
        self, orchestrator: OrchestratorPort, failing_pipeline_id: str
    ) -> None:
        pipelines = await orchestrator.list_pipelines()
        ids = [p.id for p in pipelines]
        assert failing_pipeline_id in ids
        assert len(ids) == len(set(ids))
        assert all(p.name for p in pipelines)

    async def test_runs_have_allowed_statuses(
        self, orchestrator: OrchestratorPort, failing_pipeline_id: str
    ) -> None:
        runs = await orchestrator.list_runs(failing_pipeline_id, limit=10)
        assert runs
        assert len(runs) <= 10
        for run in runs:
            assert run.status in ALLOWED_STATUSES
            assert run.pipeline_id == failing_pipeline_id
            assert run.run_id
            assert all(t.status in ALLOWED_STATUSES for t in run.tasks)

    async def test_limit_is_respected(
        self, orchestrator: OrchestratorPort, failing_pipeline_id: str
    ) -> None:
        assert len(await orchestrator.list_runs(failing_pipeline_id, limit=1)) == 1

    async def test_latest_failed_run_has_failed_tasks(
        self, orchestrator: OrchestratorPort, failing_pipeline_id: str
    ) -> None:
        summary = await latest_failed_run(orchestrator, failing_pipeline_id)
        assert summary.failed_tasks
        detail = await orchestrator.get_run(summary.run_id)
        assert detail.run_id == summary.run_id
        assert detail.status == "failed"
        assert detail.failed_tasks
        task_ids = {t.task_id for t in detail.tasks}
        assert set(detail.failed_tasks) <= task_ids

    async def test_failed_task_logs_are_text(
        self, orchestrator: OrchestratorPort, failing_pipeline_id: str
    ) -> None:
        run = await orchestrator.get_run(
            (await latest_failed_run(orchestrator, failing_pipeline_id)).run_id
        )
        for task_id in run.failed_tasks:
            logs = await orchestrator.task_logs(run.run_id, task_id)
            assert isinstance(logs, str)
            assert logs.strip(), f"log vazio para {task_id}"
        first = run.failed_tasks[0]
        short = await orchestrator.task_logs(run.run_id, first, tail_lines=3)
        assert 0 < len(short.splitlines()) <= 3

    async def test_unknown_pipeline_is_not_found(self, orchestrator: OrchestratorPort) -> None:
        with pytest.raises(NotFoundError):
            await orchestrator.list_runs(UNKNOWN_PIPELINE)
