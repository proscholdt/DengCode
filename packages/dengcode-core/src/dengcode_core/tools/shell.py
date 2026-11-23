"""Ferramenta de shell: roda um comando no diretório do workspace.

A classificação (allowlist, destrutivos) fica na política; aqui só executa.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from typing import ClassVar

from pydantic import BaseModel, Field

from dengcode_core.tools.base import ActionRequest, Preview, Risk, Tool, ToolContext, ToolResult

MAX_OUTPUT_CHARS = 60_000


class ShellRunArgs(BaseModel):
    command: str = Field(description="Comando a executar no shell do sistema.")
    timeout_s: int = Field(default=120, ge=1, le=3600, description="Tempo máximo em segundos.")


class ShellRun(Tool[ShellRunArgs]):
    name: ClassVar[str] = "shell_run"
    description: ClassVar[str] = (
        "Executa um comando de shell na raiz do workspace e devolve a saída e o código de saída. "
        "Comandos fora da lista permitida pedem aprovação; destrutivos são bloqueados."
    )
    risk: ClassVar[Risk] = Risk.EXECUTE
    Args = ShellRunArgs

    async def assess(self, args: ShellRunArgs, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(self.name, self.risk, command=args.command)

    async def preview(self, args: ShellRunArgs, ctx: ToolContext) -> Preview | None:
        return Preview("command", args.command, {"cwd": str(ctx.workspace_root)})

    async def run(self, args: ShellRunArgs, ctx: ToolContext) -> ToolResult:
        # no Windows, grupo próprio para o kill alcançar o processo do comando
        flags: int = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = await asyncio.create_subprocess_shell(
            args.command,
            cwd=ctx.workspace_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=flags,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=args.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return ToolResult(f"tempo esgotado após {args.timeout_s}s", is_error=True)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise
        text = out.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[: MAX_OUTPUT_CHARS // 2] + "\n…\n" + text[-MAX_OUTPUT_CHARS // 2 :]
        code = proc.returncode
        return ToolResult(
            f"código de saída: {code}\n{text}".rstrip(),
            is_error=code != 0,
            metadata={"exit_code": code},
        )
