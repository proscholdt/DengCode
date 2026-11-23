"""OrchestratorPort: pipelines, execuções, logs e disparo."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from dengcode_core.ports.models import Frozen

RunStatus = Literal["queued", "running", "success", "failed", "cancelled", "unknown"]


class OrchestratorCapabilities(Frozen):
    supports_trigger: bool
    supports_code_definitions: bool  # pipeline definido em código (vs JSON/UI)
    supports_backfill: bool


class PipelineInfo(Frozen):
    id: str
    name: str
    schedule: str | None = None
    paused: bool | None = None
    definition_path: str | None = None  # arquivo no repositório, quando houver


class TaskRunInfo(Frozen):
    task_id: str
    status: RunStatus
    try_number: int | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class RunInfo(Frozen):
    run_id: str
    pipeline_id: str
    status: RunStatus
    started_at: datetime | None = None
    ended_at: datetime | None = None
    failed_tasks: list[str] = Field(default_factory=lambda: list[str]())
    tasks: list[TaskRunInfo] = Field(default_factory=lambda: list[TaskRunInfo]())


@runtime_checkable
class OrchestratorPort(Protocol):
    name: str

    def capabilities(self) -> OrchestratorCapabilities: ...

    async def list_pipelines(self) -> list[PipelineInfo]: ...

    async def list_runs(self, pipeline_id: str, *, limit: int = 20) -> list[RunInfo]: ...

    async def get_run(self, run_id: str) -> RunInfo: ...

    async def task_logs(self, run_id: str, task_id: str, *, tail_lines: int = 500) -> str: ...

    async def trigger(self, pipeline_id: str, params: dict[str, Any] | None = None) -> RunInfo: ...
