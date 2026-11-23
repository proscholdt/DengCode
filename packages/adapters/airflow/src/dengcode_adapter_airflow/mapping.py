"""Conversões puras: JSON da API do Airflow -> DTOs da OrchestratorPort.

Identificadores compostos (a API precisa de mais de uma chave):

- run: ``"<dag_id>/<dag_run_id>"`` (o ``dag_id`` nunca contém ``/``);
- task mapeada (dynamic task mapping): ``"<task_id>[<map_index>]"``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

from dengcode_core.errors import AdapterError
from dengcode_core.ports.orchestrator import PipelineInfo, RunInfo, RunStatus, TaskRunInfo

_STATUS: dict[str, RunStatus] = {
    "success": "success",
    "failed": "failed",
    "upstream_failed": "failed",
    "running": "running",
    "up_for_retry": "running",
    "up_for_reschedule": "running",
    "deferred": "running",
    "restarting": "running",
    "queued": "queued",
    "scheduled": "queued",
    "none": "queued",
    "skipped": "cancelled",
    "removed": "cancelled",
}
"""Estados de DAG run e task instance -> ``RunStatus``.

``up_for_retry``/``up_for_reschedule``/``deferred`` ainda vão rodar (``running``);
``upstream_failed`` conta como falha; ``skipped``/``removed`` não executaram por
decisão do fluxo (``cancelled``)."""

FAILED_TASK_STATES = frozenset({"failed", "upstream_failed"})
_MAPPED = re.compile(r"^(?P<task>.+)\[(?P<index>\d+)\]$")


def map_status(state: object, *, task: bool = False) -> RunStatus:
    """Estado do Airflow -> ``RunStatus`` (estado nulo: task ainda sem agendamento)."""
    if state is None:
        return "queued" if task else "unknown"
    return _STATUS.get(str(state).lower(), "unknown")


def parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ------------------------------------------------------------------ ids
def compose_run_id(dag_id: str, dag_run_id: str) -> str:
    return f"{dag_id}/{dag_run_id}"


def split_run_id(run_id: str) -> tuple[str, str]:
    """``"<dag_id>/<dag_run_id>"`` -> ``(dag_id, dag_run_id)``."""
    dag_id, sep, dag_run_id = run_id.partition("/")
    if not sep or not dag_id or not dag_run_id:
        raise AdapterError(
            f"airflow: run_id {run_id!r} inválido; use o formato '<dag_id>/<dag_run_id>' "
            "(o mesmo devolvido por list_runs)"
        )
    return dag_id, dag_run_id


def task_label(task_id: str, map_index: object) -> str:
    if isinstance(map_index, int) and map_index >= 0:
        return f"{task_id}[{map_index}]"
    return task_id


def split_task_label(label: str) -> tuple[str, int | None]:
    """``"tarefa[3]"`` -> ``("tarefa", 3)``; sem índice -> ``(label, None)``."""
    match = _MAPPED.match(label)
    if match is None:
        return label, None
    return match.group("task"), int(match.group("index"))


# ------------------------------------------------------------------ DTOs
def to_pipeline(dag: Mapping[str, Any]) -> PipelineInfo:
    dag_id = str(dag["dag_id"])
    return PipelineInfo(
        id=dag_id,
        name=str(dag.get("dag_display_name") or dag_id),
        schedule=_schedule(dag),
        paused=_opt_bool(dag.get("is_paused")),
        definition_path=_opt_str(dag.get("relative_fileloc") or dag.get("fileloc")),
    )


def _schedule(dag: Mapping[str, Any]) -> str | None:
    summary = dag.get("timetable_summary")
    if isinstance(summary, str) and summary and summary != "None":
        return summary
    interval = dag.get("schedule_interval")  # Airflow 2
    if isinstance(interval, Mapping):
        kind = interval.get("__type")
        if kind == "CronExpression":
            return _opt_str(interval.get("value"))
        if kind == "TimeDelta":
            delta = timedelta(
                days=float(interval.get("days") or 0),
                seconds=float(interval.get("seconds") or 0),
                microseconds=float(interval.get("microseconds") or 0),
            )
            return f"a cada {delta}"
        payload = {k: v for k, v in interval.items() if k != "__type"}
        return f"{kind}: {json.dumps(payload, default=str)}"
    if isinstance(interval, str) and interval:
        return interval
    description = dag.get("timetable_description")
    return description if isinstance(description, str) and description else None


def to_task(ti: Mapping[str, Any]) -> TaskRunInfo:
    try_number = ti.get("try_number")
    return TaskRunInfo(
        task_id=task_label(str(ti["task_id"]), ti.get("map_index")),
        status=map_status(ti.get("state"), task=True),
        try_number=try_number if isinstance(try_number, int) else None,
        started_at=parse_dt(ti.get("start_date")),
        ended_at=parse_dt(ti.get("end_date")),
    )


def failed_task_labels(task_instances: Iterable[Mapping[str, Any]]) -> list[str]:
    """Tasks em ``failed``/``upstream_failed``, sem repetir, na ordem recebida."""
    labels: list[str] = []
    for ti in task_instances:
        if str(ti.get("state")).lower() in FAILED_TASK_STATES:
            label = task_label(str(ti["task_id"]), ti.get("map_index"))
            if label not in labels:
                labels.append(label)
    return labels


def to_run(
    run: Mapping[str, Any], task_instances: list[Mapping[str, Any]] | None = None
) -> RunInfo:
    dag_id = str(run["dag_id"])
    tis = task_instances or []
    return RunInfo(
        run_id=compose_run_id(dag_id, str(run["dag_run_id"])),
        pipeline_id=dag_id,
        status=map_status(run.get("state")),
        started_at=parse_dt(run.get("start_date")),
        ended_at=parse_dt(run.get("end_date")),
        failed_tasks=failed_task_labels(tis),
        tasks=[to_task(ti) for ti in tis],
    )


# ------------------------------------------------------------------ logs
def render_log_content(content: object) -> str:
    """Texto do log a partir do ``content`` da API.

    No Airflow 3 o ``content`` é uma lista de eventos estruturados
    (``timestamp``, ``level``, ``event``, ``error_detail``...); em versões
    antigas, uma string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        lines: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                lines.extend(_render_event(item))
            else:
                lines.append(str(item))
        return "\n".join(lines)
    return json.dumps(content, ensure_ascii=False, default=str)


_EVENT_KEYS = frozenset(
    {"timestamp", "level", "event", "logger", "filename", "lineno", "error_detail", "sources"}
)
# contexto repetido em todo evento (já é conhecido de quem pediu o log)
_CONTEXT_KEYS = frozenset({"dag_id", "run_id", "task_id", "map_index", "try_number", "ti_id"})


def _render_event(item: Mapping[str, Any]) -> list[str]:
    """Um evento estruturado -> linhas no estilo do log texto do Airflow 2."""
    parts: list[str] = []
    if item.get("timestamp"):
        parts.append(f"[{item['timestamp']}]")
    if item.get("filename"):
        where = f"{item['filename']}:{item['lineno']}" if item.get("lineno") else item["filename"]
        parts.append(f"{{{where}}}")
    if item.get("level"):
        parts.append(f"{str(item['level']).upper()} -")
    event = item.get("event")
    parts.append("" if event is None else str(event))
    extras = {k: v for k, v in item.items() if k not in _EVENT_KEYS | _CONTEXT_KEYS}
    if extras:
        parts.append(" ".join(f"{k}={v}" for k, v in extras.items()))
    lines = [" ".join(p for p in parts if p)]
    sources = item.get("sources")
    if isinstance(sources, list) and sources:
        lines.append("fontes: " + ", ".join(str(s) for s in sources))
    errors = item.get("error_detail")
    if isinstance(errors, list):
        for err in errors:
            if isinstance(err, Mapping):
                lines.extend(_render_error(err))
    return lines


def _render_error(err: Mapping[str, Any]) -> list[str]:
    lines = ["Traceback (most recent call last):"]
    frames = err.get("frames")
    if isinstance(frames, list):
        for frame in frames:
            if isinstance(frame, Mapping):
                lines.append(
                    f'  File "{frame.get("filename")}", line {frame.get("lineno")}, '
                    f"in {frame.get('name')}"
                )
    lines.append(f"{err.get('exc_type', 'Exception')}: {err.get('exc_value', '')}")
    return lines


def tail(text: str, lines: int) -> str:
    """Últimas ``lines`` linhas (``lines <= 0`` devolve tudo)."""
    if lines <= 0:
        return text
    return "\n".join(text.splitlines()[-lines:])


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opt_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
