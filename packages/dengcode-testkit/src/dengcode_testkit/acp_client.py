"""Cliente ACP de teste: dirige um servidor ACP por streams em memória ou subprocesso.

Registra tudo que chega do agente (notificações e requisições, na ordem) e
responde ``session/request_permission`` com respostas roteirizadas. Só usa a
biblioteca padrão: o testkit não depende do motor.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STREAM_LIMIT = 64 * 1024 * 1024


class AcpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


class _MemoryEnd:
    """Uma ponta de um par de pipes em memória (linhas de bytes)."""

    def __init__(self, inbound: asyncio.Queue[bytes], outbound: asyncio.Queue[bytes]) -> None:
        self._in = inbound
        self._out = outbound

    async def readline(self) -> bytes:
        return await self._in.get()

    async def write(self, data: bytes) -> None:
        await self._out.put(data)

    def close(self) -> None:
        """Sinaliza fim de arquivo para o outro lado."""
        self._out.put_nowait(b"")


def memory_transport_pair() -> tuple[_MemoryEnd, _MemoryEnd]:
    """(lado do servidor, lado do cliente)."""
    a: asyncio.Queue[bytes] = asyncio.Queue()
    b: asyncio.Queue[bytes] = asyncio.Queue()
    return _MemoryEnd(a, b), _MemoryEnd(b, a)


class _ProcessEnd:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        assert proc.stdin is not None
        self._reader = proc.stdout
        self._writer = proc.stdin

    async def readline(self) -> bytes:
        try:
            return await self._reader.readline()
        except (ConnectionError, ValueError):
            return b""

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    def close(self) -> None:
        with contextlib.suppress(OSError, RuntimeError):
            self._writer.close()


PermissionHook = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class Received:
    """Mensagem vinda do agente: ``kind`` é "notification" ou "request"."""

    kind: str
    method: str
    params: dict[str, Any]


@dataclass
class AcpTestClient:
    transport: Any
    permissions: list[str] = field(default_factory=lambda: list[str]())
    default_permission: str = "reject_once"
    on_permission: PermissionHook | None = None
    received: list[Received] = field(default_factory=lambda: list[Received]())
    process: asyncio.subprocess.Process | None = None
    _next_id: int = 0
    _pending: dict[int, asyncio.Future[Any]] = field(
        default_factory=lambda: dict[int, asyncio.Future[Any]]()
    )
    _reader: asyncio.Task[None] | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)
    _tasks: set[asyncio.Task[None]] = field(default_factory=lambda: set[asyncio.Task[None]]())

    # ------------------------------------------------------------------ ciclo de vida
    @classmethod
    async def spawn(
        cls,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> AcpTestClient:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,
        )
        client = cls(_ProcessEnd(proc), process=proc, **kwargs)
        client.start()
        return client

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if close is not None:
            close()
        if self.process is not None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=15)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader

    # ------------------------------------------------------------------ envio
    async def _send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        await self.transport.write(line.encode("utf-8"))

    async def request(self, method: str, params: Any = None) -> Any:
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: Any = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def send_raw(self, line: str) -> None:
        await self.transport.write(line.encode("utf-8") + b"\n")

    # ------------------------------------------------------------------ atalhos ACP
    async def initialize(self, **caps: Any) -> dict[str, Any]:
        return await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                    **caps,
                },
                "clientInfo": {"name": "dengcode-testkit", "version": "0"},
            },
        )

    async def new_session(self, cwd: Path) -> str:
        result = await self.request("session/new", {"cwd": str(cwd), "mcpServers": []})
        return str(result["sessionId"])

    async def prompt(self, session_id: str, text: str) -> dict[str, Any]:
        return await self.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
        )

    async def cancel(self, session_id: str) -> None:
        await self.notify("session/cancel", {"sessionId": session_id})

    # ------------------------------------------------------------------ consultas
    def notifications(self, method: str | None = None) -> list[dict[str, Any]]:
        return [
            r.params
            for r in self.received
            if r.kind == "notification" and (method is None or r.method == method)
        ]

    def updates(self, kind: str | None = None) -> list[dict[str, Any]]:
        """``update`` de cada ``session/update`` (filtrado por ``sessionUpdate``)."""
        out: list[dict[str, Any]] = []
        for p in self.notifications("session/update"):
            upd: dict[str, Any] = p.get("update") or {}
            if kind is None or upd.get("sessionUpdate") == kind:
                out.append(upd)
        return out

    def agent_text(self) -> str:
        return "".join(u["content"].get("text", "") for u in self.updates("agent_message_chunk"))

    def permission_requests(self) -> list[dict[str, Any]]:
        return [r.params for r in self.received if r.method == "session/request_permission"]

    def order(self) -> list[str]:
        """Sequência de métodos recebidos (para conferir a ordem dos eventos)."""
        return [r.method for r in self.received]

    async def wait_for(
        self, predicate: Callable[[AcpTestClient], bool], seconds: float = 10.0
    ) -> None:
        async def loop() -> None:
            while not predicate(self):
                self._changed.clear()
                await self._changed.wait()

        async with asyncio.timeout(seconds):
            await loop()

    # ------------------------------------------------------------------ leitura
    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self.transport.readline()
                if not raw:
                    break
                if raw.strip():
                    await self._dispatch(json.loads(raw))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(AcpError(-32603, "conexão encerrada"))
            self._changed.set()

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method is None:
            fut = self._pending.get(msg.get("id", -1))
            if fut is not None and not fut.done():
                err = msg.get("error")
                if err is not None:
                    fut.set_exception(
                        AcpError(err.get("code", 0), err.get("message", ""), err.get("data"))
                    )
                else:
                    fut.set_result(msg.get("result"))
            return
        params: dict[str, Any] = msg.get("params") or {}
        is_request = "id" in msg
        self.received.append(Received("request" if is_request else "notification", method, params))
        self._changed.set()
        if is_request:
            task = asyncio.create_task(self._answer(msg["id"], method, params))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _answer(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        if method != "session/request_permission":
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"método desconhecido: {method}"},
                }
            )
            return
        if self.on_permission is not None:
            await self.on_permission(params)
        choice = self.permissions.pop(0) if self.permissions else self.default_permission
        outcome: dict[str, Any] = (
            {"outcome": "cancelled"}
            if choice == "cancelled"
            else {"outcome": "selected", "optionId": choice}
        )
        with contextlib.suppress(OSError, ConnectionError, RuntimeError):
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": {"outcome": outcome}})
