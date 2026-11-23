"""StreamPort (tópicos, amostras, lag) e StreamComputePort (jobs de streaming)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from dengcode_core.ports.models import Frozen


class StreamCapabilities(Frozen):
    supports_consumer_lag: bool
    supports_schema_registry: bool


class TopicInfo(Frozen):
    name: str
    partitions: int
    retention_hours: int | None = None


@runtime_checkable
class StreamPort(Protocol):
    name: str

    def capabilities(self) -> StreamCapabilities: ...

    async def list_topics(self) -> list[TopicInfo]: ...

    async def sample(
        self, topic: str, *, max_messages: int = 20, timeout_s: int = 10
    ) -> list[dict[str, Any]]: ...  # o conteúdo passa pelo mascaramento antes do LLM

    async def consumer_lag(self, topic: str, group: str) -> dict[int, int]: ...

    async def schema(self, topic: str) -> dict[str, Any] | None: ...


class StreamJobInfo(Frozen):
    id: str
    name: str
    status: Literal["running", "failed", "stopped", "unknown"]
    lag_seconds: float | None = None
    last_checkpoint_at: datetime | None = None


@runtime_checkable
class StreamComputePort(Protocol):
    name: str

    async def list_jobs(self) -> list[StreamJobInfo]: ...

    async def job_status(self, job_id: str) -> StreamJobInfo: ...

    async def job_logs(self, job_id: str, *, tail_lines: int = 500) -> str: ...
