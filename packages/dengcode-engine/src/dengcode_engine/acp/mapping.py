"""Tradução de conceitos do DengCode para estruturas do ACP (sem I/O de rede).

Tipos de ferramenta (``kind``), títulos, diffs, motivos de parada e a
reconstrução do histórico de uma sessão salva como ``session/update``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from dengcode_core.llm.messages import Message, TextPart, ToolCallPart, ToolResultPart

PROTOCOL_VERSION = 1
EXTENSIONS = ["sqlApproval", "dataPreview", "lineage", "stack", "workspace"]
MAX_TEXT = 50_000
MAX_DIFF_BYTES = 2_000_000

READ_TOOLS = {
    "file_read",
    "file_list",
    "catalog_list_tables",
    "catalog_describe",
    "catalog_column_stats",
    "storage_list",
    "storage_schema",
    "storage_sample",
    "query_explain",
    "transform_list_models",
    "transform_compile",
    "knowledge_read",
    "impact_analysis",
    "data_diff",
    "pipeline_list",
    "pipeline_runs",
    "pipeline_run_detail",
    "pipeline_task_logs",
    "stream_list_topics",
    "stream_sample",
    "stream_consumer_lag",
    "stream_schema",
    "stream_jobs",
    "stream_job_logs",
}
SEARCH_TOOLS = {"file_search", "memory_search"}
EDIT_TOOLS = {"file_edit", "file_write"}
EXECUTE_TOOLS = {
    "shell_run",
    "query_run",
    "data_snapshot",
    "transform_run",
    "transform_test",
    "pipeline_trigger",
}
DATA_PREVIEW_TOOLS = {"query_run", "storage_sample"}
_RISK_KIND = {"read": "read", "write_local": "edit", "execute": "execute", "sql": "execute"}

STOP_REASONS = {
    "end": "end_turn",
    "max_tokens": "max_tokens",
    "max_steps": "max_turn_requests",
    "budget": "max_turn_requests",
    "cancelled": "cancelled",
}

PERMISSION_OPTIONS: list[dict[str, str]] = [
    {"optionId": "allow_once", "name": "Permitir", "kind": "allow_once"},
    {"optionId": "allow_always", "name": "Sempre nesta sessão", "kind": "allow_always"},
    {"optionId": "reject_once", "name": "Recusar", "kind": "reject_once"},
]


def tool_kind(name: str, risk: str | None = None) -> str:
    if name in EDIT_TOOLS:
        return "edit"
    if name in SEARCH_TOOLS:
        return "search"
    if name in READ_TOOLS:
        return "read"
    if name in EXECUTE_TOOLS:
        return "execute"
    return _RISK_KIND.get(risk or "", "other")


def _short(text: str, limit: int = 80) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def tool_title(name: str, args: dict[str, Any]) -> str:
    path = str(args.get("path", ""))
    titles = {
        "file_read": f"Ler {path}",
        "file_list": f"Listar {path or '.'}",
        "file_edit": f"Editar {path}",
        "file_write": f"Escrever {path}",
        "file_search": f"Buscar {_short(str(args.get('pattern', '')), 60)}",
        "shell_run": f"Executar: {_short(str(args.get('command', '')))}",
        "query_run": f"SQL: {_short(str(args.get('sql', '')))}",
    }
    if name in titles:
        return titles[name]
    if not args:
        return name
    rendered = json.dumps(args, ensure_ascii=False, default=str)
    return f"{name} {_short(rendered, 90)}"


def text_content(text: str) -> dict[str, Any]:
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n… [{len(text) - MAX_TEXT} caracteres omitidos]"
    return {"type": "content", "content": {"type": "text", "text": text}}


def _read(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size > MAX_DIFF_BYTES:
        return None
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def edit_diff(name: str, args: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Conteúdo ``diff`` do ACP (arquivo inteiro antes/depois) para file_edit/file_write.

    Calculado antes da escrita (no pedido de aprovação ou no início da execução).
    """
    raw = args.get("path")
    if name not in EDIT_TOOLS or not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    try:
        before = _read(path)
    except OSError:
        return None
    if name == "file_write":
        new = args.get("content")
        if not isinstance(new, str):
            return None
        return {"type": "diff", "path": str(path), "oldText": before, "newText": new}
    old_text, new_text = args.get("old_text"), args.get("new_text")
    if before is None or not isinstance(old_text, str) or not isinstance(new_text, str):
        return None
    if not old_text or old_text not in before:
        return None
    count = -1 if args.get("replace_all") else 1
    after = before.replace(old_text, new_text, count)
    return {"type": "diff", "path": str(path), "oldText": before, "newText": after}


def locations(args: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    raw = args.get("path")
    if not isinstance(raw, str) or not raw:
        return []
    path = Path(raw).expanduser()
    return [{"path": str((path if path.is_absolute() else root / path).resolve())}]


def prompt_text(blocks: Any) -> str:
    """Junta os blocos de conteúdo do prompt num texto (só texto é anunciado)."""
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(block, dict):
            continue
        b: dict[str, Any] = block  # pyright: ignore[reportUnknownVariableType]
        kind = b.get("type")
        if kind == "text":
            parts.append(str(b.get("text", "")))
        elif kind == "resource_link":
            parts.append(f"[{b.get('name') or b.get('uri')}]({b.get('uri')})")
        elif kind == "resource":
            res: dict[str, Any] = b.get("resource") or {}
            if isinstance(res.get("text"), str):
                parts.append(f"conteúdo de {res.get('uri', '?')}:\n```\n{res['text']}\n```")
    return "\n\n".join(p for p in parts if p)


_PLAN_ITEM = re.compile(r"^\s*(\d{1,2})[.)]\s+(.+?)\s*$")


def parse_plan(text: str) -> list[str]:
    """Itens de uma lista numerada (1., 2., ...) no texto; vazio se não houver plano."""
    items: list[str] = []
    expected = 1
    for line in text.splitlines():
        m = _PLAN_ITEM.match(line)
        if m and int(m.group(1)) == expected:
            items.append(m.group(2).strip("*_ "))
            expected += 1
    return items if len(items) >= 2 else []


def plan_update(items: list[str], status: str) -> dict[str, Any]:
    return {
        "sessionUpdate": "plan",
        "entries": [{"content": c, "priority": "medium", "status": status} for c in items],
    }


def replay_updates(messages: list[Message], root: Path) -> list[dict[str, Any]]:
    """Histórico salvo → ``session/update`` (mensagens e tool calls, sem diffs)."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            continue
        for part in msg.parts:
            if isinstance(part, TextPart) and part.text:
                kind = "user_message_chunk" if msg.role == "user" else "agent_message_chunk"
                out.append({"sessionUpdate": kind, "content": {"type": "text", "text": part.text}})
            elif isinstance(part, ToolCallPart):
                out.append(
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": part.id,
                        "title": tool_title(part.name, part.arguments),
                        "kind": tool_kind(part.name),
                        "status": "in_progress",
                        "name": part.name,
                        "rawInput": part.arguments,
                        "locations": locations(part.arguments, root),
                    }
                )
            elif isinstance(part, ToolResultPart):
                out.append(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": part.call_id,
                        "status": "failed" if part.is_error else "completed",
                        "content": [text_content(part.content)],
                    }
                )
    return out
