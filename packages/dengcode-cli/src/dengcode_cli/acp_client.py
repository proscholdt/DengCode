"""CLI como cliente ACP (``dengcode chat --acp``).

Sobe ``dengcode acp`` como subprocesso, fala JSON-RPC pelos pipes e reconstrói
os eventos do core a partir das mensagens ACP — o mesmo ``ChatRenderer`` e o
mesmo aprovador interativo do modo local desenham a sessão (paridade).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dengcode_cli import __version__
from dengcode_core.agent.loop import ApprovalAnswer
from dengcode_core.events import (
    ApprovalRequested,
    AssistantText,
    SessionEnded,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from dengcode_engine.acp.jsonrpc import JsonRpcConnection, JsonRpcError
from dengcode_engine.acp.stdio import StreamTransport

Render = Callable[[BaseModel], Any]
Ask = Callable[[ApprovalRequested], Awaitable[ApprovalAnswer]]
Notice = Callable[[str], Any]

STREAM_LIMIT = 64 * 1024 * 1024
_REASONS = {
    "end_turn": "end",
    "max_tokens": "max_tokens",
    "max_turn_requests": "max_steps",
    "cancelled": "cancelled",
    "refusal": "error",
}


def server_command(
    *,
    stack: str,
    env: str,
    project: Path,
    mode: str | None = None,
    llm: str | None = None,
    workspace: str | None = None,
) -> list[str]:
    """``dengcode acp`` pelo mesmo interpretador (não depende do PATH)."""
    cmd = [sys.executable, "-m", "dengcode_cli.main", "acp"]
    cmd += ["--stack", stack, "--env", env, "--project", str(project)]
    for flag, value in (("--mode", mode), ("--llm", llm), ("--workspace", workspace)):
        if value:
            cmd += [flag, value]
    return cmd


def _text(content: Any) -> str:
    parts: list[str] = []
    for item in content if isinstance(content, list) else []:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(item, dict) and item.get("type") == "content":  # pyright: ignore[reportUnknownMemberType]
            block: dict[str, Any] = item.get("content") or {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
    return "\n".join(parts)


class AcpChatClient:
    def __init__(self, render: Render, ask: Ask, notice: Notice | None = None) -> None:
        self.render = render
        self.ask = ask
        self.notice = notice or (lambda _msg: None)
        self.conn: JsonRpcConnection | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.session_id: str | None = None
        self.meta: dict[str, Any] = {}
        self._serving: asyncio.Task[None] | None = None
        self._calls: dict[str, dict[str, Any]] = {}
        self._replaying = False

    # ------------------------------------------------------------------ ciclo de vida
    async def start(self, cmd: Sequence[str], *, cwd: Path) -> None:
        # Ctrl+C no terminal é do cliente (cancela o turno), não do servidor
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
            **kwargs,
        )
        assert self.process.stdout is not None
        assert self.process.stdin is not None
        self.conn = JsonRpcConnection(
            StreamTransport(self.process.stdout, self.process.stdin),
            on_request=self._on_request,
            on_notification=self._on_notification,
        )
        self._serving = asyncio.create_task(self.conn.serve())
        await self.conn.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    "session": {"notices": {}},
                },
                "clientInfo": {
                    "name": "dengcode-cli",
                    "title": "DengCode CLI",
                    "version": __version__,
                },
            },
        )

    async def open(self, cwd: Path, resume: str | None = None) -> dict[str, Any]:
        conn = self._require()
        params: dict[str, Any] = {"cwd": str(cwd), "mcpServers": []}
        if resume:
            # o histórico volta como session/update; o modo local também não o redesenha
            self._replaying = True
            try:
                result = await conn.request("session/load", {**params, "sessionId": resume})
            finally:
                self._replaying = False
            self.session_id = resume
        else:
            result = await conn.request("session/new", params)
            self.session_id = str(result["sessionId"])
        self.meta = dict((result or {}).get("_meta", {}).get("dengcode", {}))
        return self.meta

    async def close(self) -> None:
        if self.process is not None and self.process.stdin is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self.process.stdin.close()
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._serving is not None:
            self._serving.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._serving

    def _require(self) -> JsonRpcConnection:
        if self.conn is None:
            raise RuntimeError("cliente ACP não iniciado")
        return self.conn

    # ------------------------------------------------------------------ turno
    async def prompt(self, text: str) -> str:
        """Roda um turno e devolve o motivo de parada (no vocabulário do core)."""
        conn = self._require()
        try:
            result = await conn.request(
                "session/prompt",
                {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]},
            )
        except JsonRpcError as exc:
            self.render(SessionEnded(reason="error", detail=exc.message))
            return "error"
        meta: dict[str, Any] = (result.get("_meta") or {}).get("dengcode") or {}
        reason = str(meta.get("reason") or _REASONS.get(result.get("stopReason"), "end"))
        if reason not in ("end", "max_steps", "budget", "cancelled", "error", "max_tokens"):
            reason = "end"
        self.render(SessionEnded(reason=reason, detail=meta.get("detail")))  # type: ignore[arg-type]
        return reason

    async def cancel(self) -> None:
        await self._require().notify("session/cancel", {"sessionId": self.session_id})

    # ------------------------------------------------------------------ agente → cliente
    async def _on_notification(self, method: str, params: Any) -> None:
        if method != "session/update" or self._replaying or not isinstance(params, dict):
            return  # extensões _dengcode/* são para IDEs; o terminal já mostra o essencial
        update: dict[str, Any] = params.get("update") or {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content: dict[str, Any] = update.get("content") or {}
            if content.get("type") == "text":
                self.render(AssistantText(delta=str(content.get("text", ""))))
        elif kind in ("tool_call", "tool_call_update"):
            self._on_tool(update)
        elif kind == "usage_update":
            d: dict[str, Any] = (update.get("_meta") or {}).get("dengcode") or {}
            self.render(
                UsageUpdated(
                    input_tokens=int(d.get("inputTokens", 0)),
                    output_tokens=int(d.get("outputTokens", 0)),
                    total_input_tokens=int(d.get("totalInputTokens", 0)),
                    total_output_tokens=int(d.get("totalOutputTokens", 0)),
                    total_cost_usd=d.get("costUsd"),
                )
            )
        elif kind == "notice":
            self.notice(str(update.get("title", "")))

    def _on_tool(self, update: dict[str, Any]) -> None:
        call_id = str(update.get("toolCallId"))
        known = self._calls.setdefault(call_id, {})
        for key in ("name", "title", "rawInput"):
            if update.get(key) is not None:
                known[key] = update[key]
        name = str(known.get("name") or known.get("title") or "?")
        args: dict[str, Any] = known.get("rawInput") or {}
        status = update.get("status")
        if status == "in_progress":
            self.render(ToolCallStarted(call_id=call_id, name=name, arguments=args, risk=""))
        elif status in ("completed", "failed"):
            self.render(
                ToolCallFinished(
                    call_id=call_id,
                    name=name,
                    content=_text(update.get("content")),
                    is_error=status == "failed",
                )
            )

    async def _on_request(self, method: str, params: Any) -> Any:
        if method != "session/request_permission" or not isinstance(params, dict):
            raise JsonRpcError(-32601, f"método desconhecido: {method}")
        p: dict[str, Any] = params  # pyright: ignore[reportUnknownVariableType]
        tool: dict[str, Any] = p.get("toolCall") or {}
        d: dict[str, Any] = (p.get("_meta") or {}).get("dengcode") or {}
        req = ApprovalRequested(
            call_id=str(tool.get("toolCallId")),
            name=str(d.get("name") or tool.get("title") or "?"),
            arguments=d.get("arguments") or tool.get("rawInput") or {},
            risk=str(d.get("risk") or tool.get("kind") or "?"),
            reason=str(d.get("reason") or tool.get("title") or ""),
            preview=d.get("preview"),
            preview_kind=d.get("previewKind"),
            details=d.get("details") or {},
        )
        answer = await self.ask(req)
        option = (
            ("allow_always" if answer.remember else "allow_once")
            if answer.approved
            else "reject_once"
        )
        return {"outcome": {"outcome": "selected", "optionId": option}}
