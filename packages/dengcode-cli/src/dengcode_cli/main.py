"""Ponto de entrada da CLI ``dengcode``."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import typer
import yaml
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from dengcode_cli import __version__
from dengcode_cli.ui import AutoApprover, ChatRenderer, InteractiveApprover
from dengcode_core.agent.loop import Approver, deny_all
from dengcode_core.checkpoints import latest_session_store
from dengcode_core.errors import ConfigError, DengCodeError
from dengcode_core.ports.models import PortKind
from dengcode_engine.config.credentials import load_dotenv
from dengcode_engine.config.loader import StackLoader
from dengcode_engine.paths import (
    current_workspace,
    dengcode_home,
    list_workspaces,
    sessions_dir,
    set_current_workspace,
    workspace_dir,
)
from dengcode_engine.project import init_project
from dengcode_engine.registry import AdapterRegistry
from dengcode_engine.runtime import Runtime, RuntimeOptions, build_runtime


def _ensure_utf8() -> None:
    """Console do Windows usa outra codificação por padrão; a CLI fala UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_ensure_utf8()
console = Console()
err_console = Console(stderr=True)

app = typer.Typer(
    name="dengcode",
    help="DengCode — assistente de IA para engenharia de dados.",
    no_args_is_help=True,
    add_completion=False,
)
adapters_app = typer.Typer(help="Adapters instalados.", no_args_is_help=True)
stack_app = typer.Typer(help="Stacks (ligação porta → adapter).", no_args_is_help=True)
sessions_app = typer.Typer(help="Sessões salvas.", no_args_is_help=True)
audit_app = typer.Typer(help="Auditoria de queries (append-only).", no_args_is_help=True)
workspace_app = typer.Typer(
    help="Workspaces (isolam memória, sessões e auditoria).", no_args_is_help=True
)
catalog_app = typer.Typer(help="Catálogo de dados.", no_args_is_help=True)
memory_app = typer.Typer(help="Memória do workspace.", no_args_is_help=True)
app.add_typer(adapters_app, name="adapters")
app.add_typer(stack_app, name="stack")
app.add_typer(sessions_app, name="sessions")
app.add_typer(audit_app, name="audit")
app.add_typer(workspace_app, name="workspace")
app.add_typer(catalog_app, name="catalog")
app.add_typer(memory_app, name="memory")

StackOpt = typer.Option("local", "--stack", "-s", help="Stack a usar.")
EnvOpt = typer.Option("dev", "--env", "-e", help="Ambiente da stack (dev, prod...).")
ModeOpt = typer.Option(None, "--mode", "-m", help="Modo de permissão: ask, auto, read_only.")
LlmOpt = typer.Option(None, "--llm", help="Troca o LLM: provedor ou provedor:modelo.")
WsOpt = typer.Option(
    None, "--workspace", "-w", help="Workspace (padrão: o ativo; isola memória e sessões)."
)
DirOpt = typer.Option(Path("."), "--project", "-p", help="Diretório do projeto.")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"dengcode {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Mostra a versão e sai.",
        callback=_version_callback,
        is_eager=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Logs de depuração."),
) -> None:
    """DengCode — assistente de IA para engenharia de dados."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # o sqlglot avisa quando cai para "Command"; o SqlGuard já trata esses casos
    logging.getLogger("sqlglot").setLevel(logging.DEBUG if verbose else logging.ERROR)


def _fail(exc: Exception) -> typer.Exit:
    err_console.print(f"[red]erro:[/red] {exc}")
    return typer.Exit(code=1)


def _options(
    project: Path,
    stack: str,
    env: str,
    mode: str | None,
    llm: str | None,
    workspace: str | None,
    resume: str | None = None,
) -> RuntimeOptions:
    project = project.resolve()
    load_dotenv(project)
    return RuntimeOptions(
        project_dir=project,
        stack=stack,
        env=env,
        mode=mode,
        llm=llm,
        workspace=workspace or current_workspace(),
        session_id=resume,
    )


# ----------------------------------------------------------------------- adapters
@adapters_app.command("list")
def adapters_list() -> None:
    """Lista os adapters descobertos por entry points."""
    registry = AdapterRegistry.discover()
    table = Table(title="Adapters instalados")
    for col in ("porta", "nome", "versão", "pacote"):
        table.add_column(col)
    for item in registry.all():
        if item.info is None:
            table.add_row("?", item.entry_point, "-", f"[red]erro: {item.error}[/red]")
            continue
        table.add_row(
            item.info.port.value, item.info.name, item.info.version, item.distribution or "-"
        )
    console.print(table)


# ----------------------------------------------------------------------- stacks
@stack_app.command("list")
def stack_list(project: Path = DirOpt) -> None:
    """Lista as stacks disponíveis (projeto > usuário > embutidas)."""
    loader = StackLoader(project.resolve())
    table = Table(title="Stacks")
    for col in ("nome", "origem", "descrição"):
        table.add_column(col)
    for src in loader.list():
        try:
            desc = loader.load(src.name).description or ""
        except DengCodeError as exc:
            desc = f"[red]inválida: {exc}[/red]"
        table.add_row(src.name, src.origin, desc)
    console.print(table)


@stack_app.command("show")
def stack_show(name: str, project: Path = DirOpt) -> None:
    """Mostra a stack já resolvida (com extends aplicado)."""
    try:
        stack = StackLoader(project.resolve()).load(name)
    except DengCodeError as exc:
        raise _fail(exc) from exc
    data = stack.model_dump(mode="json", exclude_none=True)
    console.print(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


@stack_app.command("validate")
def stack_validate(name: str, project: Path = DirOpt) -> None:
    """Valida schema, adapters instalados e valores de exemplo pendentes."""
    try:
        stack = StackLoader(project.resolve()).load(name)
    except DengCodeError as exc:
        raise _fail(exc) from exc
    registry = AdapterRegistry.discover()
    refs: list[tuple[PortKind, str]] = []
    if stack.llm:
        refs.append((PortKind.LLM, stack.llm.provider))
        if stack.llm.fallback:
            refs.append((PortKind.LLM, stack.llm.fallback.provider))
    refs += [(PortKind.QUERY_ENGINE, c.adapter) for c in stack.compute]
    single = (
        (PortKind.STORAGE, stack.storage),
        (PortKind.CATALOG, stack.catalog),
        (PortKind.ORCHESTRATOR, stack.orchestrator),
        (PortKind.TRANSFORM, stack.transform),
        (PortKind.STREAM, stack.streaming),
        (PortKind.SECRETS, stack.secrets),
    )
    refs += [(port, ref.adapter) for port, ref in single if ref is not None]
    problems = [
        f"adapter não instalado: {port.value} → {adapter}"
        for port, adapter in refs
        if not registry.has(port, adapter)
    ]
    for field in stack.placeholders():
        console.print(f"[yellow]⚠ valor de exemplo a preencher: {field}[/yellow]")
    for p in problems:
        console.print(f"[red]✗ {p}[/red]")
    if problems:
        raise typer.Exit(code=1)
    console.print(f"[green]✓ stack {name} válida[/green]")


@stack_app.command("doctor")
def stack_doctor(
    name: str, env: str = EnvOpt, workspace: str | None = WsOpt, project: Path = DirOpt
) -> None:
    """Conecta em cada porta da stack e executa uma operação inofensiva."""
    from dengcode_engine.doctor import run_doctor

    project = project.resolve()
    load_dotenv(project)
    try:
        stack = StackLoader(project).load(name)
    except DengCodeError as exc:
        raise _fail(exc) from exc
    checks = asyncio.run(
        run_doctor(
            stack,
            AdapterRegistry.discover(),
            project_dir=project,
            workspace=workspace or current_workspace(),
            env=env,
            home=dengcode_home(),
        )
    )
    table = Table(title=f"stack doctor · {name} · {env}")
    for col in ("", "porta", "adapter", "detalhe", "ms"):
        table.add_column(col)
    for c in checks:
        mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
        table.add_row(mark, c.port, c.adapter, c.detail, str(c.elapsed_ms))
    console.print(table)
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)


# ----------------------------------------------------------------------- auditoria
def _parse_since(text: str) -> datetime:
    units = {"m": 60, "h": 3600, "d": 86400}
    if len(text) < 2 or text[-1] not in units or not text[:-1].isdigit():
        raise typer.BadParameter("use o formato 30m, 12h ou 7d")
    return datetime.now(UTC) - timedelta(seconds=int(text[:-1]) * units[text[-1]])


@audit_app.command("list")
def audit_list(
    since: str = typer.Option("7d", "--since", help="Janela: 30m, 12h, 7d."),
    limit: int = typer.Option(50, "--limit", help="Máximo de registros."),
    workspace: str | None = WsOpt,
) -> None:
    """Lista as queries auditadas do workspace."""
    workspace = workspace or current_workspace()
    from dengcode_core.audit import AuditLog

    path = workspace_dir(workspace) / "audit.db"
    if not path.exists():
        console.print("nenhuma auditoria ainda")
        return
    table = Table(title=f"Auditoria · {workspace} · últimos {since}")
    for col in ("quando", "motor", "tipo", "decisão", "linhas", "ms", "sql"):
        table.add_column(col, overflow="fold")
    for r in AuditLog(path).list(since=_parse_since(since), limit=limit):
        sql = r.sql if len(r.sql) <= 90 else r.sql[:89] + "…"
        decision = r.decision + (f" ({r.error[:40]})" if r.error else "")
        rows = str(r.rows) if r.rows is not None else "-"
        ms = str(r.elapsed_ms) if r.elapsed_ms is not None else "-"
        table.add_row(
            r.ts.strftime("%Y-%m-%d %H:%M"), r.engine or "-", r.kind or "-", decision, rows, ms, sql
        )
    console.print(table)


# ----------------------------------------------------------------------- sessões
@sessions_app.command("list")
def sessions_list(workspace: str | None = WsOpt) -> None:
    """Lista as sessões salvas do workspace."""
    workspace = workspace or current_workspace()
    from dengcode_core.session import SessionStore

    table = Table(title=f"Sessões · {workspace}")
    for col in ("id", "atualizada", "stack", "título"):
        table.add_column(col)
    for meta in SessionStore(sessions_dir(workspace)).list():
        table.add_row(
            meta.id, meta.updated_at.strftime("%Y-%m-%d %H:%M"), meta.stack or "-", meta.title or ""
        )
    console.print(table)


@app.command("undo")
def undo(project: Path = DirOpt) -> None:
    """Desfaz a última escrita de arquivo feita pelo agente."""
    store = latest_session_store(project.resolve())
    restored = store.undo() if store else None
    if restored is None:
        console.print("nada para desfazer")
        raise typer.Exit(code=1)
    action = "restaurado" if restored.existed else "removido (era novo)"
    console.print(f"[green]✓ {restored.path} {action}[/green]")


# ----------------------------------------------------------------------- chat / run
async def _chat_loop(rt: Runtime, renderer: ChatRenderer) -> None:
    me = asyncio.current_task()
    console.print(
        f"[bold]DengCode[/bold] · stack [cyan]{rt.stack.name}[/cyan] · "
        f"modelo [cyan]{rt.agent.models.main}[/cyan] · modo [cyan]{rt.agent.policy.mode}[/cyan]\n"
        "[dim]/ajuda para comandos · Ctrl+C cancela o turno[/dim]"
    )
    while True:
        try:
            line = await asyncio.to_thread(console.input, "\n[bold green]você ›[/bold green] ")
        except (EOFError, KeyboardInterrupt):
            break
        except asyncio.CancelledError:
            if me is not None:
                me.uncancel()
            console.print("\n[dim](use /sair para sair)[/dim]")
            continue
        text = line.strip()
        if not text:
            continue
        if text.startswith("/"):
            if not _slash(text, rt, renderer):
                break
            continue
        turn = asyncio.create_task(rt.agent.run(text))
        try:
            await turn
        except asyncio.CancelledError:
            turn.cancel()
            if me is not None:
                me.uncancel()
            console.print("\n[yellow]turno cancelado[/yellow]")
        except DengCodeError as exc:
            err_console.print(f"[red]erro:[/red] {exc}")
    console.print(f"[dim]sessão {rt.agent.session.id} salva · {renderer.cost_line()}[/dim]")


def _slash(cmd: str, rt: Runtime, renderer: ChatRenderer) -> bool:
    """Executa um comando /x. Devolve False para sair."""
    name = cmd.split()[0].lower()
    if name in ("/sair", "/exit", "/quit"):
        return False
    if name == "/undo":
        store = rt.agent.ctx.checkpoints
        restored = store.undo() if store else None
        console.print(
            f"[green]✓ desfeito: {restored.path}[/green]" if restored else "nada para desfazer"
        )
    elif name == "/stack":
        console.print(
            f"stack {rt.stack.name} · ambiente {rt.options.env} · "
            f"LLM {rt.agent.llm.name} ({rt.agent.models.main} / {rt.agent.models.fast})"
        )
    elif name == "/cost":
        console.print(renderer.cost_line())
    else:
        console.print(
            "/undo desfaz a última escrita · /stack mostra a stack · "
            "/cost mostra tokens e custo · /sair encerra"
        )
    return True


async def _acp_chat(opts: RuntimeOptions, renderer: ChatRenderer) -> None:
    """``chat --acp``: a CLI é cliente ACP de um ``dengcode acp`` em subprocesso."""
    from dengcode_cli.acp_client import AcpChatClient, server_command
    from dengcode_core.checkpoints import CheckpointStore
    from dengcode_engine.acp.jsonrpc import JsonRpcError

    client = AcpChatClient(
        renderer,
        InteractiveApprover(console),
        notice=lambda msg: console.print(f"[yellow]⚠ {msg}[/yellow]"),
    )
    cmd = server_command(
        stack=opts.stack,
        env=opts.env,
        project=opts.project_dir,
        mode=opts.mode,
        llm=opts.llm,
        workspace=opts.workspace,
    )
    try:
        await client.start(cmd, cwd=opts.project_dir)
        meta = await client.open(opts.project_dir, opts.session_id)
    except (JsonRpcError, OSError) as exc:
        await client.close()
        raise _fail(exc) from exc
    me = asyncio.current_task()
    console.print(
        f"[bold]DengCode[/bold] (ACP) · stack [cyan]{meta.get('stack')}[/cyan] · "
        f"modelo [cyan]{meta.get('model')}[/cyan] · modo [cyan]{meta.get('mode')}[/cyan]\n"
        "[dim]/ajuda para comandos · Ctrl+C cancela o turno[/dim]"
    )
    try:
        while True:
            try:
                line = await asyncio.to_thread(console.input, "\n[bold green]você ›[/bold green] ")
            except (EOFError, KeyboardInterrupt):
                break
            except asyncio.CancelledError:
                if me is not None:
                    me.uncancel()
                console.print("\n[dim](use /sair para sair)[/dim]")
                continue
            text = line.strip()
            if not text:
                continue
            if text.startswith("/"):
                name = text.split()[0].lower()
                if name in ("/sair", "/exit", "/quit"):
                    break
                if name == "/undo":
                    store = CheckpointStore.for_session(opts.project_dir, client.session_id or "")
                    restored = store.undo()
                    console.print(
                        f"[green]✓ desfeito: {restored.path}[/green]"
                        if restored
                        else "nada para desfazer"
                    )
                elif name == "/stack":
                    console.print(
                        f"stack {meta.get('stack')} · ambiente {meta.get('env')} · "
                        f"LLM {meta.get('llm')} ({meta.get('model')}) · via ACP"
                    )
                elif name == "/cost":
                    console.print(renderer.cost_line())
                else:
                    console.print(
                        "/undo desfaz a última escrita · /stack mostra a stack · "
                        "/cost mostra tokens e custo · /sair encerra"
                    )
                continue
            turn = asyncio.create_task(client.prompt(text))
            try:
                await asyncio.shield(turn)
            except asyncio.CancelledError:
                if me is not None:
                    me.uncancel()
                await client.cancel()
                await turn
                console.print("\n[yellow]turno cancelado[/yellow]")
    finally:
        await client.close()
    console.print(f"[dim]sessão {client.session_id} salva · {renderer.cost_line()}[/dim]")


@app.command("chat")
def chat(
    stack: str = StackOpt,
    env: str = EnvOpt,
    mode: str | None = ModeOpt,
    llm: str | None = LlmOpt,
    workspace: str | None = WsOpt,
    project: Path = DirOpt,
    resume: str | None = typer.Option(None, "--resume", help="Retoma uma sessão pelo id."),
    acp: bool = typer.Option(
        False, "--acp", help="Roda o motor como subprocesso ACP (o mesmo caminho das IDEs)."
    ),
) -> None:
    """Conversa interativa com o agente."""
    renderer = ChatRenderer(console)
    opts = _options(project, stack, env, mode, llm, workspace, resume)
    if acp:
        asyncio.run(_acp_chat(opts, renderer))
        return

    async def main() -> None:
        try:
            rt = await build_runtime(opts, approver=InteractiveApprover(console))
        except DengCodeError as exc:
            raise _fail(exc) from exc
        rt.bus.subscribe(renderer)
        await _chat_loop(rt, renderer)

    asyncio.run(main())


@app.command("acp")
def acp_server(
    stack: str = typer.Option(
        "local", "--stack", "-s", envvar="DENGCODE_STACK", help="Stack padrão das sessões."
    ),
    env: str = typer.Option(
        "dev", "--env", "-e", envvar="DENGCODE_ENV", help="Ambiente padrão das sessões."
    ),
    mode: str | None = typer.Option(
        None,
        "--mode",
        "-m",
        envvar="DENGCODE_MODE",
        help="Modo de permissão: ask, auto, read_only.",
    ),
    llm: str | None = typer.Option(
        None, "--llm", envvar="DENGCODE_LLM", help="Troca o LLM: provedor ou provedor:modelo."
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", "-w", envvar="DENGCODE_WORKSPACE", help="Workspace das sessões."
    ),
    project: Path = DirOpt,
) -> None:
    """Servidor ACP em stdio (Zed, JetBrains, VS Code). O stdout é só do protocolo."""
    from dengcode_engine.acp.server import AcpDefaults, AcpServer, serve_stdio

    project = project.resolve()
    load_dotenv(project)
    defaults = AcpDefaults(stack=stack, env=env, workspace=workspace, mode=mode, llm=llm)
    server = AcpServer(defaults=defaults, project_dir=project)
    asyncio.run(serve_stdio(server))


@app.command("run")
def run(
    task: str = typer.Argument(..., help="Tarefa em linguagem natural."),
    as_json: bool = typer.Option(False, "--json", help="Eventos em JSON Lines (para CI)."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Aprova o que pedir aprovação (bloqueios continuam)."
    ),
    stack: str = StackOpt,
    env: str = EnvOpt,
    mode: str | None = ModeOpt,
    llm: str | None = LlmOpt,
    workspace: str | None = WsOpt,
    project: Path = DirOpt,
) -> None:
    """Executa uma tarefa sem interação (headless)."""
    approver: Approver = AutoApprover() if yes else deny_all
    opts = _options(project, stack, env, mode, llm, workspace)

    def emit(event: BaseModel) -> None:
        sys.stdout.write(event.model_dump_json() + "\n")
        sys.stdout.flush()

    async def main() -> tuple[str, int, str, float | None]:
        try:
            rt = await build_runtime(opts, approver=approver)
        except DengCodeError as exc:
            if as_json:
                print(json.dumps({"type": "error", "message": str(exc)}, ensure_ascii=False))
                raise typer.Exit(code=1) from exc
            raise _fail(exc) from exc
        rt.bus.subscribe(emit if as_json else ChatRenderer(console))
        result = await rt.agent.run(task)
        return result.reason, result.steps, rt.agent.session.id, rt.agent.total_cost_usd

    reason, steps, session_id, cost = asyncio.run(main())
    if as_json:
        final = {
            "type": "result",
            "reason": reason,
            "steps": steps,
            "session_id": session_id,
            "cost_usd": cost,
        }
        print(json.dumps(final, ensure_ascii=False))
    if reason == "error":
        raise typer.Exit(code=2)


# ----------------------------------------------------------------------- workspaces
@workspace_app.command("list")
def workspace_list() -> None:
    """Lista os workspaces (o ativo aparece marcado)."""
    active = current_workspace()
    names = sorted({*list_workspaces(), active})
    for name in names:
        console.print(f"{'*' if name == active else ' '} {name}")


@workspace_app.command("new")
def workspace_new(name: str, use: bool = typer.Option(True, help="Já ativar o novo.")) -> None:
    """Cria um workspace (memória, sessões e auditoria isoladas)."""
    try:
        path = workspace_dir(name)
    except DengCodeError as exc:
        raise _fail(exc) from exc
    path.mkdir(parents=True, exist_ok=True)
    if use:
        set_current_workspace(name)
    console.print(f"[green]✓ workspace {name} criado[/green]" + (" e ativo" if use else ""))


@workspace_app.command("use")
def workspace_use(name: str) -> None:
    """Ativa um workspace."""
    try:
        workspace_dir(name).mkdir(parents=True, exist_ok=True)
        set_current_workspace(name)
    except DengCodeError as exc:
        raise _fail(exc) from exc
    console.print(f"[green]✓ workspace ativo: {name}[/green]")


# ----------------------------------------------------------------------- projeto
@app.command("init")
def init(project: Path = DirOpt) -> None:
    """Cria .dengcode/PROJETO.md com convenções, camadas e tabelas protegidas."""
    path, created = init_project(project.resolve())
    if created:
        console.print(f"[green]✓ criado {path}[/green] — edite e versione junto com o código")
    else:
        console.print(f"já existe: {path}")


# ----------------------------------------------------------------------- catálogo e memória
@catalog_app.command("sync")
def catalog_sync(
    stack: str = StackOpt, env: str = EnvOpt, workspace: str | None = WsOpt, project: Path = DirOpt
) -> None:
    """Grava os metadados do catálogo (tabelas, colunas, estatísticas) na memória."""
    from dengcode_engine.memory import sync_catalog

    opts = _options(project, stack, env, None, None, workspace)

    async def main() -> str:
        rt = await build_runtime(opts)
        catalog, memory, scope = rt.data.catalog, rt.data.memory, rt.data.scope
        if catalog is None or memory is None or scope is None:
            raise _fail(ConfigError("a stack precisa de catálogo e memória para sincronizar"))
        report = await sync_catalog(catalog, memory, scope, source=catalog.name)
        return (
            f"{report.tables} tabela(s), {report.changed} com schema alterado, "
            f"{report.stats} com estatísticas, {report.errors} erro(s)"
        )

    try:
        summary = asyncio.run(main())
    except DengCodeError as exc:
        raise _fail(exc) from exc
    console.print(f"[green]✓ catálogo sincronizado:[/green] {summary}")


async def _memory(opts: RuntimeOptions) -> tuple[Any, Any]:
    rt = await build_runtime(opts)
    if rt.data.memory is None or rt.data.scope is None:
        raise _fail(ConfigError("a stack não tem memória configurada"))
    return rt.data.memory, rt.data.scope


@memory_app.command("list")
def memory_list(
    proposed: bool = typer.Option(False, "--proposed", help="Só as propostas pendentes."),
    stack: str = StackOpt,
    workspace: str | None = WsOpt,
    project: Path = DirOpt,
) -> None:
    """Lista os registros de memória do workspace."""
    opts = _options(project, stack, "dev", None, None, workspace)

    async def main() -> list[Any]:
        memory, scope = await _memory(opts)
        recs = await memory.list(scope, limit=200)
        return [r for r in recs if not proposed or r.confidence == "proposed"]

    table = Table(title=f"Memória · {opts.workspace}")
    for col in ("id", "tipo", "confiança", "título", "atualizado"):
        table.add_column(col, overflow="fold")
    for r in asyncio.run(main()):
        table.add_row(r.id, r.kind.value, r.confidence, r.title, r.updated_at.strftime("%Y-%m-%d"))
    console.print(table)


@memory_app.command("confirm")
def memory_confirm(
    record_id: str, stack: str = StackOpt, workspace: str | None = WsOpt, project: Path = DirOpt
) -> None:
    """Confirma um registro proposto pelo agente (vira verified)."""
    opts = _options(project, stack, "dev", None, None, workspace)

    async def main() -> bool:
        memory, scope = await _memory(opts)
        rec = await memory.get(scope, record_id)
        if rec is None:
            return False
        await memory.upsert(rec.model_copy(update={"confidence": "verified"}))
        return True

    if not asyncio.run(main()):
        raise _fail(ConfigError(f"registro não encontrado: {record_id}"))
    console.print(f"[green]✓ {record_id} confirmado[/green]")


@memory_app.command("forget")
def memory_forget(
    record_id: str, stack: str = StackOpt, workspace: str | None = WsOpt, project: Path = DirOpt
) -> None:
    """Apaga um registro da memória do workspace."""
    opts = _options(project, stack, "dev", None, None, workspace)

    async def main() -> None:
        memory, scope = await _memory(opts)
        await memory.delete(scope, record_id)

    asyncio.run(main())
    console.print(f"[green]✓ {record_id} esquecido[/green]")


if __name__ == "__main__":  # pragma: no cover
    app()
