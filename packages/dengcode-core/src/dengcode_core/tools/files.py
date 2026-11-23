"""Ferramentas genéricas de arquivo: ler, listar, buscar, editar, escrever."""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import re
import shutil
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from dengcode_core.tools.base import ActionRequest, Preview, Risk, Tool, ToolContext, ToolResult

_IGNORED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".dengcode",
    "target",
    ".pytest_cache",
    ".ruff_cache",
    "dbt_packages",
}
MAX_READ_BYTES = 2_000_000


def unified_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(lines)


def _rel(path: Path, ctx: ToolContext) -> str:
    try:
        return path.relative_to(ctx.workspace_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read_text(path: Path) -> str:
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read(MAX_READ_BYTES)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# --------------------------------------------------------------------- leitura
class FileReadArgs(BaseModel):
    path: str = Field(description="Caminho do arquivo (relativo ao workspace).")
    offset: int = Field(default=0, ge=0, description="Linha inicial (0 = começo).")
    limit: int = Field(default=2000, ge=1, le=10_000, description="Máximo de linhas.")


class FileRead(Tool[FileReadArgs]):
    name: ClassVar[str] = "file_read"
    description: ClassVar[str] = "Lê um arquivo de texto e devolve as linhas numeradas."
    risk: ClassVar[Risk] = Risk.READ
    Args = FileReadArgs
    parallel_safe: ClassVar[bool] = True

    async def assess(self, args: FileReadArgs, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(self.name, self.risk, paths_read=[ctx.resolve(args.path)])

    async def run(self, args: FileReadArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(f"arquivo não encontrado: {args.path}", is_error=True)
        text = await asyncio.to_thread(_read_text, path)
        lines = text.splitlines()
        chunk = lines[args.offset : args.offset + args.limit]
        body = "\n".join(f"{i + 1 + args.offset:>6}\t{line}" for i, line in enumerate(chunk))
        rest = len(lines) - args.offset - len(chunk)
        if rest > 0:
            body += f"\n… mais {rest} linhas (use offset={args.offset + len(chunk)})"
        return ToolResult(body or "(arquivo vazio)")


class FileListArgs(BaseModel):
    path: str = Field(default=".", description="Diretório (relativo ao workspace).")
    recursive: bool = Field(default=False, description="Listar subdiretórios.")
    pattern: str | None = Field(default=None, description="Filtro glob, ex.: '*.sql'.")


class FileList(Tool[FileListArgs]):
    name: ClassVar[str] = "file_list"
    description: ClassVar[str] = "Lista arquivos e diretórios do workspace."
    risk: ClassVar[Risk] = Risk.READ
    Args = FileListArgs
    parallel_safe: ClassVar[bool] = True
    untrusted_output: ClassVar[bool] = False

    async def assess(self, args: FileListArgs, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(self.name, self.risk, paths_read=[ctx.resolve(args.path)])

    async def run(self, args: FileListArgs, ctx: ToolContext) -> ToolResult:
        base = ctx.resolve(args.path)
        if not base.is_dir():
            return ToolResult(f"diretório não encontrado: {args.path}", is_error=True)

        def walk() -> list[str]:
            out: list[str] = []
            it = base.rglob("*") if args.recursive else base.iterdir()
            for p in sorted(it):
                if _IGNORED_DIRS.intersection(p.relative_to(base).parts):
                    continue
                if args.pattern and not fnmatch.fnmatch(p.name, args.pattern):
                    continue
                out.append(_rel(p, ctx) + ("/" if p.is_dir() else ""))
                if len(out) >= 2000:
                    out.append("… (lista truncada em 2000 itens)")
                    break
            return out

        items = await asyncio.to_thread(walk)
        return ToolResult("\n".join(items) or "(vazio)")


class FileSearchArgs(BaseModel):
    pattern: str = Field(description="Expressão regular a procurar.")
    path: str = Field(default=".", description="Diretório base.")
    glob: str | None = Field(default=None, description="Filtro de arquivos, ex.: '*.py'.")
    max_results: int = Field(default=200, ge=1, le=2000)


class FileSearch(Tool[FileSearchArgs]):
    name: ClassVar[str] = "file_search"
    description: ClassVar[str] = (
        "Busca uma expressão regular no conteúdo dos arquivos (arquivo:linha:texto)."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = FileSearchArgs
    parallel_safe: ClassVar[bool] = True

    async def assess(self, args: FileSearchArgs, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(self.name, self.risk, paths_read=[ctx.resolve(args.path)])

    async def run(self, args: FileSearchArgs, ctx: ToolContext) -> ToolResult:
        base = ctx.resolve(args.path)
        try:
            regex = re.compile(args.pattern)
        except re.error as exc:
            return ToolResult(f"regex inválida: {exc}", is_error=True)
        rg = shutil.which("rg")
        if rg:
            hits = await self._ripgrep(rg, args, base, ctx)
            if hits is not None:
                return ToolResult("\n".join(hits) or "(nenhum resultado)")
        hits = await asyncio.to_thread(self._python_search, regex, args, base, ctx)
        return ToolResult("\n".join(hits) or "(nenhum resultado)")

    async def _ripgrep(
        self, rg: str, args: FileSearchArgs, base: Path, ctx: ToolContext
    ) -> list[str] | None:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", "--max-count", "50"]
        if args.glob:
            cmd += ["--glob", args.glob]
        root = ctx.workspace_root.resolve()
        target = _rel(base, ctx) if ctx.inside_workspace(base) else str(base)
        cmd += ["--", args.pattern, target or "."]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, _ = await proc.communicate()
        except OSError:
            return None
        if proc.returncode not in (0, 1):
            return None
        hits: list[str] = []
        for line in out.decode("utf-8", errors="replace").splitlines()[: args.max_results]:
            file_part, sep, rest = line.partition(":")
            hits.append(file_part.replace("\\", "/").removeprefix("./") + sep + rest)
        return hits

    def _python_search(
        self, regex: re.Pattern[str], args: FileSearchArgs, base: Path, ctx: ToolContext
    ) -> list[str]:
        hits: list[str] = []
        files = [base] if base.is_file() else sorted(base.rglob(args.glob or "*"))
        for f in files:
            if not f.is_file() or _IGNORED_DIRS.intersection(f.parts):
                continue
            try:
                text = _read_text(f)
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{_rel(f, ctx)}:{i}:{line.strip()}")
                    if len(hits) >= args.max_results:
                        return hits
        return hits


# --------------------------------------------------------------------- escrita
class FileEditArgs(BaseModel):
    path: str = Field(description="Arquivo a editar.")
    old_text: str = Field(description="Trecho exato a substituir (precisa ser único).")
    new_text: str = Field(description="Novo trecho.")
    replace_all: bool = Field(default=False, description="Substituir todas as ocorrências.")


def _apply_edit(text: str, args: FileEditArgs) -> str:
    count = text.count(args.old_text)
    if count == 0:
        raise ValueError("old_text não encontrado no arquivo")
    if count > 1 and not args.replace_all:
        raise ValueError(f"old_text aparece {count} vezes; inclua mais contexto ou replace_all")
    return text.replace(args.old_text, args.new_text, -1 if args.replace_all else 1)


class FileEdit(Tool[FileEditArgs]):
    name: ClassVar[str] = "file_edit"
    description: ClassVar[str] = (
        "Edita um arquivo substituindo um trecho exato por outro. Mostra diff e pede aprovação."
    )
    risk: ClassVar[Risk] = Risk.WRITE_LOCAL
    Args = FileEditArgs
    untrusted_output: ClassVar[bool] = False

    async def assess(self, args: FileEditArgs, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(self.name, self.risk, paths_written=[ctx.resolve(args.path)])

    async def preview(self, args: FileEditArgs, ctx: ToolContext) -> Preview | None:
        path = ctx.resolve(args.path)
        if not path.is_file():
            return Preview("text", f"arquivo não encontrado: {args.path}")
        before = _read_text(path)
        try:
            after = _apply_edit(before, args)
        except ValueError as exc:
            return Preview("text", f"edição inválida: {exc}")
        return Preview(
            "diff", unified_diff(_rel(path, ctx), before, after), {"path": _rel(path, ctx)}
        )

    async def run(self, args: FileEditArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.is_file():
            return ToolResult(f"arquivo não encontrado: {args.path}", is_error=True)
        before = _read_text(path)
        try:
            after = _apply_edit(before, args)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True)
        if ctx.checkpoints is not None:
            ctx.checkpoints.save(path)
        await asyncio.to_thread(_write_text, path, after)
        diff = unified_diff(_rel(path, ctx), before, after)
        return ToolResult(f"editado: {_rel(path, ctx)}", metadata={"diff": diff})


class FileWriteArgs(BaseModel):
    path: str = Field(description="Arquivo a criar ou sobrescrever.")
    content: str = Field(description="Conteúdo completo do arquivo.")


class FileWrite(Tool[FileWriteArgs]):
    name: ClassVar[str] = "file_write"
    description: ClassVar[str] = (
        "Cria ou sobrescreve um arquivo com o conteúdo dado. Mostra diff e pede aprovação."
    )
    risk: ClassVar[Risk] = Risk.WRITE_LOCAL
    Args = FileWriteArgs
    untrusted_output: ClassVar[bool] = False

    async def assess(self, args: FileWriteArgs, ctx: ToolContext) -> ActionRequest:
        return ActionRequest(self.name, self.risk, paths_written=[ctx.resolve(args.path)])

    async def preview(self, args: FileWriteArgs, ctx: ToolContext) -> Preview | None:
        path = ctx.resolve(args.path)
        before = _read_text(path) if path.is_file() else ""
        return Preview(
            "diff",
            unified_diff(_rel(path, ctx), before, args.content),
            {"path": _rel(path, ctx), "new_file": not path.exists()},
        )

    async def run(self, args: FileWriteArgs, ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args.path)
        before = _read_text(path) if path.is_file() else ""
        if ctx.checkpoints is not None:
            ctx.checkpoints.save(path)
        await asyncio.to_thread(_write_text, path, args.content)
        diff = unified_diff(_rel(path, ctx), before, args.content)
        return ToolResult(f"escrito: {_rel(path, ctx)}", metadata={"diff": diff})
