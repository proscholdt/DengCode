"""JSON-RPC 2.0 bidirecional sobre um transporte de linhas (JSON delimitado por ``\\n``).

Os dois lados podem enviar requisições e notificações. Cada requisição recebida
roda numa task própria (um ``session/prompt`` longo não bloqueia o
``session/cancel``); notificações são tratadas em ordem, na própria leitura.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002


class Transport(Protocol):
    """Canal de bytes orientado a linhas. ``readline`` devolve ``b""`` no fim."""

    async def readline(self) -> bytes: ...

    async def write(self, data: bytes) -> None: ...


class JsonRpcError(Exception):
    """Erro JSON-RPC (recebido do outro lado ou a ser devolvido a ele)."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err


RequestHandler = Callable[[str, Any], Awaitable[Any]]
NotificationHandler = Callable[[str, Any], Awaitable[None]]


async def _no_request(method: str, params: Any) -> Any:
    raise JsonRpcError(METHOD_NOT_FOUND, f"método desconhecido: {method}")


async def _no_notification(method: str, params: Any) -> None:
    return None


class JsonRpcConnection:
    def __init__(
        self,
        transport: Transport,
        *,
        on_request: RequestHandler = _no_request,
        on_notification: NotificationHandler = _no_notification,
    ) -> None:
        self.transport = transport
        self.on_request = on_request
        self.on_notification = on_notification
        self._next_id = 0
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # ------------------------------------------------------------------ envio
    async def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
        await self.transport.write(line.encode("utf-8") + b"\n")

    async def notify(self, method: str, params: Any = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._send(msg)

    async def request(self, method: str, params: Any = None) -> Any:
        if self._closed:
            raise JsonRpcError(INTERNAL_ERROR, "conexão encerrada")
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(msg)
            return await fut
        finally:
            self._pending.pop(req_id, None)

    # ------------------------------------------------------------------ leitura
    async def serve(self) -> None:
        """Lê mensagens até o fim do transporte."""
        try:
            while True:
                raw = await self.transport.readline()
                if not raw:
                    break
                line = raw.strip()
                if line:
                    await self._dispatch(line)
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(JsonRpcError(INTERNAL_ERROR, "conexão encerrada"))
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _dispatch(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": PARSE_ERROR, "message": f"JSON inválido: {exc}"},
                }
            )
            return
        if not isinstance(msg, dict):
            await self._invalid(None)
            return
        data: dict[str, Any] = msg  # pyright: ignore[reportUnknownVariableType]
        method = data.get("method")
        if isinstance(method, str):
            params = data.get("params")
            if "id" in data:
                task = asyncio.create_task(self._handle_request(data["id"], method, params))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                await self._handle_notification(method, params)
            return
        if "id" in data and ("result" in data or "error" in data):
            self._handle_response(data)
            return
        await self._invalid(data.get("id"))

    async def _invalid(self, req_id: Any) -> None:
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": INVALID_REQUEST, "message": "mensagem JSON-RPC inválida"},
            }
        )

    def _handle_response(self, msg: dict[str, Any]) -> None:
        fut = self._pending.get(msg["id"])
        if fut is None or fut.done():
            log.debug("resposta para id desconhecido: %s", msg.get("id"))
            return
        err = msg.get("error")
        if err is not None:
            e: dict[str, Any] = err if isinstance(err, dict) else {"message": str(err)}  # pyright: ignore[reportUnknownVariableType]
            fut.set_exception(
                JsonRpcError(
                    int(e.get("code", INTERNAL_ERROR)), str(e.get("message", "")), e.get("data")
                )
            )
        else:
            fut.set_result(msg.get("result"))

    async def _handle_notification(self, method: str, params: Any) -> None:
        try:
            await self.on_notification(method, params)
        except Exception:
            log.exception("falha ao tratar a notificação %s", method)

    async def _handle_request(self, req_id: Any, method: str, params: Any) -> None:
        try:
            result = await self.on_request(method, params)
            response: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except JsonRpcError as exc:
            response = {"jsonrpc": "2.0", "id": req_id, "error": exc.to_dict()}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("falha ao tratar %s", method)
            response = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": INTERNAL_ERROR, "message": f"{type(exc).__name__}: {exc}"},
            }
        with contextlib.suppress(OSError, ConnectionError):
            await self._send(response)
