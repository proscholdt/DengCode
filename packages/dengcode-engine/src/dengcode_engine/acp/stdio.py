"""Transportes do ACP: stdio do processo e pipes de um subprocesso.

Por que uma thread lendo o stdin: no Windows, o loop padrão (Proactor) aceita
``connect_read_pipe(sys.stdin)`` mas nunca entrega os dados quando o stdin é o
pipe herdado do processo pai (a leitura fica parada para sempre). Uma thread
com leitura bloqueante funciona igual em Windows, Linux e macOS.

O stdout é o protocolo: ``protect_stdout`` duplica o descritor real para uso
exclusivo do ACP e aponta o descritor 1 (e ``sys.stdout``) para o stderr — um
``print`` perdido ou um subprocesso falante não corrompe o fluxo JSON-RPC.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import threading
from typing import BinaryIO


class StdioTransport:
    """Linhas lidas por uma thread; escritas serializadas fora do loop."""

    def __init__(self, reader: BinaryIO, writer: BinaryIO) -> None:
        self._reader = reader
        self._writer = writer
        self._queue: asyncio.Queue[bytes] | None = None
        self._lock = asyncio.Lock()
        self._thread: threading.Thread | None = None

    def _start(self) -> asyncio.Queue[bytes]:
        if self._queue is not None:
            return self._queue
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._queue = queue

        def pump() -> None:
            try:
                for line in iter(self._reader.readline, b""):
                    loop.call_soon_threadsafe(queue.put_nowait, line)
            except (OSError, ValueError):
                pass
            finally:
                with contextlib.suppress(RuntimeError):  # loop já fechado
                    loop.call_soon_threadsafe(queue.put_nowait, b"")

        self._thread = threading.Thread(target=pump, name="acp-stdin", daemon=True)
        self._thread.start()
        return queue

    async def readline(self) -> bytes:
        return await self._start().get()

    def _write_sync(self, data: bytes) -> None:
        self._writer.write(data)
        self._writer.flush()

    async def write(self, data: bytes) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write_sync, data)


def protect_stdout() -> BinaryIO:
    """Devolve um canal binário exclusivo para o protocolo e desvia o stdout para o stderr."""
    try:
        sys.stdout.flush()
        out_fd = sys.stdout.fileno()
        err_fd = sys.stderr.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        # stdout sem descritor (ex.: capturado em testes): usa o buffer como está
        return sys.stdout.buffer
    proto_fd = os.dup(out_fd)
    os.dup2(err_fd, out_fd)
    sys.stdout = sys.stderr
    return os.fdopen(proto_fd, "wb", buffering=0)


def process_stdio() -> StdioTransport:
    return StdioTransport(sys.stdin.buffer, protect_stdout())


class StreamTransport:
    """Pipes de um subprocesso asyncio (lado cliente: CLI e testes)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()

    async def readline(self) -> bytes:
        try:
            return await self._reader.readline()
        except (ConnectionError, ValueError):
            return b""

    async def write(self, data: bytes) -> None:
        async with self._lock:
            self._writer.write(data)
            await self._writer.drain()
