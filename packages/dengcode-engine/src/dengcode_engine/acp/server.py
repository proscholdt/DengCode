"""AcpServer: o motor do DengCode falando ACP (v1) sobre um transporte JSON-RPC.

Métodos do agente: ``initialize``, ``session/new``, ``session/load``,
``session/prompt``, ``session/cancel`` (notificação) e as extensões
``_dengcode/lineage``, ``_dengcode/stack.list|stack.use`` e
``_dengcode/workspace.list|workspace.use``. Do agente para o cliente:
``session/update``, ``session/request_permission`` e as notificações
``_dengcode/sqlApproval`` e ``_dengcode/dataPreview``.

As ferramentas do DengCode agem direto no disco: o servidor não usa os
métodos ``fs/*`` nem ``terminal/*`` do cliente (ver ADR 0009).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dengcode_core.agent.loop import ApprovalAnswer, Approver
from dengcode_core.errors import DengCodeError, NotFoundError
from dengcode_core.events import (
    ApprovalRequested,
    AssistantText,
    ContextCompacted,
    EventBus,
    ProviderFallback,
    ToolCallFinished,
    ToolCallStarted,
    UsageUpdated,
)
from dengcode_core.session import SessionStore
from dengcode_core.tools.transform import LineageService
from dengcode_engine import __version__
from dengcode_engine.acp import mapping
from dengcode_engine.acp.jsonrpc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    RESOURCE_NOT_FOUND,
    JsonRpcConnection,
    JsonRpcError,
    Transport,
)
from dengcode_engine.config.credentials import load_dotenv
from dengcode_engine.config.loader import StackLoader
from dengcode_engine.paths import (
    current_workspace,
    dengcode_home,
    list_workspaces,
    sessions_dir,
    validate_workspace_name,
    workspace_dir,
)
from dengcode_engine.registry import AdapterRegistry
from dengcode_engine.runtime import Runtime, RuntimeOptions, build_runtime

log = logging.getLogger(__name__)

RuntimeFactory = Callable[[RuntimeOptions, Approver, EventBus], Awaitable[Runtime]]


@dataclass
class AcpDefaults:
    """Escolhas para sessões novas (mudam com ``_dengcode/stack.use``/``workspace.use``)."""

    stack: str = "local"
    env: str = "dev"
    workspace: str | None = None  # None = o workspace ativo (``dengcode workspace use``)
    mode: str | None = None
    llm: str | None = None
    max_steps: int = 40
    max_usd: float | None = None


def _params(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise JsonRpcError(INVALID_PARAMS, "params precisa ser um objeto")
    return raw  # pyright: ignore[reportUnknownVariableType]


def _str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise JsonRpcError(INVALID_PARAMS, f"parâmetro obrigatório: {key}")
    return value


def _cwd(params: dict[str, Any]) -> Path:
    return Path(_str(params, "cwd")).expanduser().resolve()


def _json_safe(data: Any) -> Any:
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


@dataclass
class _CallView:
    """O que já foi anunciado ao cliente sobre uma tool call."""

    name: str
    title: str
    kind: str
    arguments: dict[str, Any]
    locations: list[dict[str, Any]]
    diff: dict[str, Any] | None = None
    extra: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())

    def content(self) -> list[dict[str, Any]]:
        return [*([self.diff] if self.diff else []), *self.extra]


class AcpSession:
    """Uma sessão ACP = um ``Runtime`` do DengCode + tradução do EventBus."""

    def __init__(self, server: AcpServer, conn: JsonRpcConnection) -> None:
        self.server = server
        self.conn = conn
        self.runtime: Runtime | None = None
        self.turn: asyncio.Task[Any] | None = None
        self.calls: dict[str, _CallView] = {}
        self.open_calls: set[str] = set()
        self._turn_text: list[str] = []
        self._plan: list[str] | None = None

    @property
    def id(self) -> str:
        assert self.runtime is not None
        return self.runtime.agent.session.id

    @property
    def root(self) -> Path:
        assert self.runtime is not None
        return self.runtime.agent.ctx.workspace_root

    # ------------------------------------------------------------------ envio
    async def update(self, payload: dict[str, Any]) -> None:
        await self.conn.notify("session/update", {"sessionId": self.id, "update": payload})

    def _view(self, name: str, args: dict[str, Any], risk: str | None) -> _CallView:
        return _CallView(
            name=name,
            title=mapping.tool_title(name, args),
            kind=mapping.tool_kind(name, risk),
            arguments=args,
            locations=mapping.locations(args, self.root),
            diff=mapping.edit_diff(name, args, self.root),
        )

    def _tool_call(self, call_id: str, view: _CallView, status: str) -> dict[str, Any]:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": view.title,
            "kind": view.kind,
            "status": status,
            "name": view.name,
            "content": view.content(),
            "locations": view.locations,
            "rawInput": _json_safe(view.arguments),
        }

    # ------------------------------------------------------------------ eventos
    async def on_event(self, event: BaseModel) -> None:
        if isinstance(event, AssistantText):
            self._turn_text.append(event.delta)
            chunk = {"type": "text", "text": event.delta}
            await self.update({"sessionUpdate": "agent_message_chunk", "content": chunk})
        elif isinstance(event, ApprovalRequested):
            await self._maybe_plan()
            await self._on_approval(event)
        elif isinstance(event, ToolCallStarted):
            await self._maybe_plan()
            await self._on_started(event)
        elif isinstance(event, ToolCallFinished):
            await self._maybe_plan()
            await self._on_finished(event)
        elif isinstance(event, UsageUpdated):
            await self._on_usage(event)
        elif isinstance(event, ProviderFallback):
            await self._notice(
                "warning", f"{event.from_provider} falhou; usando {event.to_provider}"
            )
        elif isinstance(event, ContextCompacted):
            await self._notice(
                "info",
                f"contexto compactado: {event.messages_before} → {event.messages_after} mensagens",
            )

    async def _notice(self, severity: str, title: str) -> None:
        if self.server.client_notices:
            await self.update({"sessionUpdate": "notice", "severity": severity, "title": title})

    async def _maybe_plan(self) -> None:
        """Plano heurístico: se o primeiro texto do turno tem uma lista numerada."""
        if self._plan is not None:
            return
        self._plan = mapping.parse_plan("".join(self._turn_text))
        if self._plan:
            await self.update(mapping.plan_update(self._plan, "pending"))

    async def _on_approval(self, ev: ApprovalRequested) -> None:
        view = self._view(ev.name, ev.arguments, ev.risk)
        if view.diff is None and ev.preview:
            lang = {"sql": "sql", "command": "sh", "diff": "diff"}.get(ev.preview_kind or "", "")
            view.extra.append(mapping.text_content(f"```{lang}\n{ev.preview}\n```"))
        self.calls[ev.call_id] = view
        self.open_calls.add(ev.call_id)
        await self.update(self._tool_call(ev.call_id, view, "pending"))
        if ev.preview_kind == "sql":
            d = ev.details
            await self.conn.notify(
                "_dengcode/sqlApproval",
                _json_safe(
                    {
                        "sessionId": self.id,
                        "toolCallId": ev.call_id,
                        "sql": ev.preview,
                        "kind": d.get("sql_kind"),
                        "engine": d.get("engine"),
                        "statements": d.get("statements", 1),
                        "tablesRead": list(d.get("tables_read") or []),
                        "tablesWritten": list(d.get("tables_written") or []),
                        "cost": d.get("cost"),
                        "warnings": [*(d.get("warnings") or []), ev.reason],
                        "plan": d.get("plan"),
                    }
                ),
            )

    async def approve(self, ev: ApprovalRequested) -> ApprovalAnswer:
        """Aprovador do loop: ``session/request_permission`` ao cliente."""
        view = self.calls.get(ev.call_id) or self._view(ev.name, ev.arguments, ev.risk)
        params = {
            "sessionId": self.id,
            "toolCall": {
                "toolCallId": ev.call_id,
                "title": view.title,
                "kind": view.kind,
                "status": "pending",
                "content": view.content(),
                "locations": view.locations,
                "rawInput": _json_safe(ev.arguments),
            },
            "options": mapping.PERMISSION_OPTIONS,
            # para clientes do DengCode (CLI --acp) desenharem o mesmo card do modo local
            "_meta": {
                "dengcode": _json_safe(
                    {
                        "name": ev.name,
                        "risk": ev.risk,
                        "reason": ev.reason,
                        "preview": ev.preview,
                        "previewKind": ev.preview_kind,
                        "details": ev.details,
                        "arguments": ev.arguments,
                    }
                )
            },
        }
        try:
            result = await self.conn.request("session/request_permission", params)
        except JsonRpcError as exc:
            log.warning("pedido de permissão falhou (%s): recusando", exc.message)
            return ApprovalAnswer(approved=False)
        outcome: dict[str, Any] = (result or {}).get("outcome") or {}
        if outcome.get("outcome") != "selected":
            return ApprovalAnswer(approved=False)
        option = outcome.get("optionId")
        if option == "allow_always":
            return ApprovalAnswer(approved=True, remember=True)
        return ApprovalAnswer(approved=option == "allow_once")

    async def _on_started(self, ev: ToolCallStarted) -> None:
        if ev.call_id in self.calls:
            await self.update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": ev.call_id,
                    "status": "in_progress",
                }
            )
            return
        view = self._view(ev.name, ev.arguments, ev.risk)
        self.calls[ev.call_id] = view
        self.open_calls.add(ev.call_id)
        await self.update(self._tool_call(ev.call_id, view, "in_progress"))

    async def _on_finished(self, ev: ToolCallFinished) -> None:
        status = "failed" if ev.is_error else "completed"
        view = self.calls.get(ev.call_id)
        text = [mapping.text_content(ev.content)] if ev.content else []
        if view is None:
            # bloqueada pela política ou argumentos inválidos: nunca chegou a começar
            view = self._view(ev.name, {}, None)
            view.extra = text
            self.calls[ev.call_id] = view
            await self.update(self._tool_call(ev.call_id, view, status))
        else:
            # edição recusada ou com erro: o diff não foi aplicado, não mostrar
            content = [*([view.diff] if view.diff and not ev.is_error else []), *text]
            payload: dict[str, Any] = {
                "sessionUpdate": "tool_call_update",
                "toolCallId": ev.call_id,
                "status": status,
                "content": content,
            }
            if ev.metadata:
                payload["rawOutput"] = _json_safe(ev.metadata)
            await self.update(payload)
        self.open_calls.discard(ev.call_id)
        meta = ev.metadata
        if (
            ev.name in mapping.DATA_PREVIEW_TOOLS
            and not ev.is_error
            and isinstance(meta.get("columns"), list)
            and isinstance(meta.get("preview"), list)
        ):
            total = meta.get("total_rows")
            await self.conn.notify(
                "_dengcode/dataPreview",
                _json_safe(
                    {
                        "sessionId": self.id,
                        "toolCallId": ev.call_id,
                        "columns": meta["columns"],
                        "rows": meta["preview"][:20],
                        "totalRows": total if total is not None else meta.get("rows"),
                        "resultPath": meta.get("result_path"),
                    }
                ),
            )

    async def _on_usage(self, ev: UsageUpdated) -> None:
        assert self.runtime is not None
        agent = self.runtime.agent
        size = agent.llm.profile(agent.models.main).context_window
        payload: dict[str, Any] = {
            "sessionUpdate": "usage_update",
            "used": ev.input_tokens + ev.output_tokens,
            "size": size,
            "_meta": {
                "dengcode": {
                    "inputTokens": ev.input_tokens,
                    "outputTokens": ev.output_tokens,
                    "totalInputTokens": ev.total_input_tokens,
                    "totalOutputTokens": ev.total_output_tokens,
                    "costUsd": ev.total_cost_usd,
                }
            },
        }
        if ev.total_cost_usd is not None:
            payload["cost"] = {"amount": ev.total_cost_usd, "currency": "USD"}
        await self.update(payload)

    # ------------------------------------------------------------------ turno
    async def prompt(self, text: str) -> dict[str, Any]:
        assert self.runtime is not None
        if self.turn is not None and not self.turn.done():
            raise JsonRpcError(INVALID_REQUEST, "já há um turno em andamento nesta sessão")
        self._turn_text = []
        self._plan = None
        turn = asyncio.create_task(self.runtime.agent.run(text))
        self.turn = turn
        try:
            await asyncio.wait({turn})
        except asyncio.CancelledError:
            turn.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await turn
            raise
        if turn.cancelled():
            await self._close_open_calls()
            return {"stopReason": "cancelled", "_meta": {"dengcode": {"reason": "cancelled"}}}
        exc = turn.exception()
        if isinstance(exc, DengCodeError):
            raise JsonRpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}", {"reason": "error"})
        if exc is not None:
            raise exc
        result = turn.result()
        if result.reason == "error":
            detail = result.detail or "erro no turno"
            raise JsonRpcError(INTERNAL_ERROR, detail, {"reason": "error"})
        await self._maybe_plan()
        if self._plan and result.reason == "end":
            await self.update(mapping.plan_update(self._plan, "completed"))
        return {
            "stopReason": mapping.STOP_REASONS.get(result.reason, "end_turn"),
            "_meta": {
                "dengcode": {
                    "reason": result.reason,
                    "detail": result.detail,
                    "steps": result.steps,
                }
            },
        }

    async def _close_open_calls(self) -> None:
        for call_id in sorted(self.open_calls):
            await self.update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": call_id,
                    "status": "failed",
                    "content": [mapping.text_content("cancelado pelo usuário")],
                }
            )
        self.open_calls.clear()

    def cancel(self) -> None:
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()


class AcpServer:
    def __init__(
        self,
        *,
        defaults: AcpDefaults | None = None,
        registry: AdapterRegistry | None = None,
        home: Path | None = None,
        project_dir: Path | None = None,
        factory: RuntimeFactory | None = None,
    ) -> None:
        self.defaults = defaults or AcpDefaults()
        self._registry = registry
        self.home = home or dengcode_home()
        self.project_dir = (project_dir or Path.cwd()).resolve()
        self._factory = factory
        self.sessions: dict[str, AcpSession] = {}
        self.client_capabilities: dict[str, Any] = {}
        self.conn: JsonRpcConnection | None = None

    # ------------------------------------------------------------------ infraestrutura
    @property
    def registry(self) -> AdapterRegistry:
        if self._registry is None:
            self._registry = AdapterRegistry.discover()
        return self._registry

    @property
    def client_notices(self) -> bool:
        session = self.client_capabilities.get("session")
        return isinstance(session, dict) and session.get("notices") is not None  # pyright: ignore[reportUnknownMemberType]

    @property
    def workspace(self) -> str:
        return self.defaults.workspace or current_workspace(self.home)

    async def _build(self, opts: RuntimeOptions, approver: Approver, bus: EventBus) -> Runtime:
        if self._factory is not None:
            return await self._factory(opts, approver, bus)
        return await build_runtime(
            opts, registry=self.registry, approver=approver, bus=bus, home=self.home
        )

    async def serve(self, transport: Transport) -> None:
        conn = JsonRpcConnection(
            transport, on_request=self.handle_request, on_notification=self.handle_notification
        )
        self.conn = conn
        try:
            await conn.serve()
        finally:
            for session in self.sessions.values():
                session.cancel()

    def _conn(self) -> JsonRpcConnection:
        if self.conn is None:
            raise JsonRpcError(INTERNAL_ERROR, "servidor sem conexão")
        return self.conn

    def _session(self, params: dict[str, Any]) -> AcpSession:
        sid = _str(params, "sessionId")
        session = self.sessions.get(sid)
        if session is None:
            raise JsonRpcError(RESOURCE_NOT_FOUND, f"sessão desconhecida: {sid}")
        return session

    # ------------------------------------------------------------------ despacho
    async def handle_request(self, method: str, raw: Any) -> Any:
        params = _params(raw)
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
            "initialize": self.initialize,
            "session/new": self.new_session,
            "session/load": self.load_session,
            "session/prompt": self.prompt,
            "_dengcode/lineage": self.lineage,
            "_dengcode/stack.list": self.stack_list,
            "_dengcode/stack.use": self.stack_use,
            "_dengcode/workspace.list": self.workspace_list,
            "_dengcode/workspace.use": self.workspace_use,
        }
        handler = handlers.get(method)
        if handler is None:
            raise JsonRpcError(METHOD_NOT_FOUND, f"método desconhecido: {method}")
        try:
            return await handler(params)
        except NotFoundError as exc:
            raise JsonRpcError(RESOURCE_NOT_FOUND, str(exc)) from exc
        except DengCodeError as exc:
            raise JsonRpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}") from exc

    async def handle_notification(self, method: str, raw: Any) -> None:
        if method == "session/cancel":
            sid = _params(raw).get("sessionId")
            session = self.sessions.get(sid) if isinstance(sid, str) else None
            if session is not None:
                session.cancel()
        # demais notificações (inclusive ``_``-prefixadas desconhecidas) são ignoradas

    # ------------------------------------------------------------------ métodos ACP
    async def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        caps = params.get("clientCapabilities")
        self.client_capabilities = caps if isinstance(caps, dict) else {}  # pyright: ignore[reportUnknownMemberType]
        return {
            "protocolVersion": mapping.PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
                "mcpCapabilities": {"http": False, "sse": False},
                "_meta": {"dengcode": {"version": 1, "extensions": list(mapping.EXTENSIONS)}},
            },
            "agentInfo": {"name": "dengcode", "title": "DengCode", "version": __version__},
            "authMethods": [],
        }

    def _options(
        self, cwd: Path, *, stack: str, env: str, session_id: str | None = None
    ) -> RuntimeOptions:
        d = self.defaults
        return RuntimeOptions(
            project_dir=cwd,
            stack=stack,
            env=env,
            workspace=self.workspace,
            mode=d.mode,
            llm=d.llm,
            session_id=session_id,
            max_steps=d.max_steps,
            max_usd=d.max_usd,
        )

    async def _open(self, opts: RuntimeOptions) -> AcpSession:
        session = AcpSession(self, self._conn())
        bus = EventBus()
        bus.subscribe(session.on_event)
        load_dotenv(opts.project_dir)  # credenciais do projeto (nunca sobrescreve o ambiente)
        session.runtime = await self._build(opts, session.approve, bus)
        self.project_dir = opts.project_dir
        self.sessions[session.id] = session
        return session

    def _describe(self, session: AcpSession) -> dict[str, Any]:
        assert session.runtime is not None
        rt = session.runtime
        return {
            "dengcode": {
                "stack": rt.stack.name,
                "env": rt.options.env,
                "workspace": rt.options.workspace,
                "model": rt.agent.models.main,
                "llm": rt.agent.llm.name,
                "mode": rt.agent.policy.mode.value,
            }
        }

    async def new_session(self, params: dict[str, Any]) -> dict[str, Any]:
        cwd = _cwd(params)
        # mcpServers é aceito e ignorado: o DengCode não usa servidores MCP nesta fase
        opts = self._options(cwd, stack=self.defaults.stack, env=self.defaults.env)
        session = await self._open(opts)
        return {"sessionId": session.id, "_meta": self._describe(session)}

    async def load_session(self, params: dict[str, Any]) -> dict[str, Any]:
        sid = _str(params, "sessionId")
        cwd = _cwd(params)
        meta = SessionStore(sessions_dir(self.workspace, self.home)).load(sid).meta
        old = self.sessions.pop(sid, None)
        if old is not None:
            old.cancel()
        opts = self._options(
            cwd, stack=meta.stack or self.defaults.stack, env=meta.env, session_id=sid
        )
        session = await self._open(opts)
        assert session.runtime is not None
        for update in mapping.replay_updates(session.runtime.agent.session.messages, session.root):
            await session.update(update)
        return {"_meta": self._describe(session)}

    async def prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params)
        text = mapping.prompt_text(params.get("prompt"))
        if not text.strip():
            raise JsonRpcError(INVALID_PARAMS, "prompt vazio (só blocos de texto são aceitos)")
        return await session.prompt(text)

    # ------------------------------------------------------------------ extensões
    async def lineage(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params)
        target = _str(params, "target")
        direction = params.get("direction", "down")
        if direction not in ("down", "up"):
            raise JsonRpcError(INVALID_PARAMS, "direction precisa ser 'down' ou 'up'")
        depth = params.get("depth", 3)
        if not isinstance(depth, int) or depth < 1:
            raise JsonRpcError(INVALID_PARAMS, "depth precisa ser um inteiro ≥ 1")
        empty: dict[str, Any] = {"root": target, "nodes": [], "edges": []}
        assert session.runtime is not None
        rt = session.runtime
        transform = rt.adapters.get("transform")
        catalog = rt.data.catalog
        has_catalog_lineage = catalog is not None and catalog.capabilities().supports_lineage
        if transform is None and not has_catalog_lineage:
            return empty
        dialect = "duckdb"
        if rt.data.engines:
            dialect = rt.data.engine(None)[1].capabilities().sql_dialect
        graph = await LineageService(transform, catalog, dialect).graph()
        found = graph.resolve(target)
        if found is None:
            return empty
        root, _ = found
        edges = graph.edges(root, direction=direction, depth=depth)
        ids = [root, *(n for e in edges for n in (e.source, e.target))]
        nodes = [
            {"id": n, "kind": "column" if n in graph.columns else "table"}
            for n in dict.fromkeys(ids)
        ]
        return {
            "root": root,
            "nodes": nodes,
            "edges": [{"source": e.source, "target": e.target, "via": e.via} for e in edges],
        }

    async def stack_list(self, params: dict[str, Any]) -> dict[str, Any]:
        loader = StackLoader(self.project_dir, self.home)
        stacks: list[dict[str, Any]] = []
        for src in loader.list():
            try:
                desc = loader.load(src.name).description or ""
            except DengCodeError as exc:
                desc = f"inválida: {exc}"
            stacks.append({"name": src.name, "origin": src.origin, "description": desc})
        active = {"stack": self.defaults.stack, "env": self.defaults.env}
        return {"stacks": stacks, "active": active}

    async def stack_use(self, params: dict[str, Any]) -> dict[str, Any]:
        stack = _str(params, "stack")
        env = params.get("env") or self.defaults.env
        if not isinstance(env, str):
            raise JsonRpcError(INVALID_PARAMS, "env precisa ser texto")
        config = StackLoader(self.project_dir, self.home).load(stack)
        config.environment(env)  # valida o ambiente
        self.defaults.stack, self.defaults.env = stack, env
        return {"stack": stack, "env": env}

    async def workspace_list(self, params: dict[str, Any]) -> dict[str, Any]:
        active = self.workspace
        return {"workspaces": sorted({*list_workspaces(self.home), active}), "active": active}

    async def workspace_use(self, params: dict[str, Any]) -> dict[str, Any]:
        name = validate_workspace_name(_str(params, "workspace"))
        workspace_dir(name, self.home).mkdir(parents=True, exist_ok=True)
        self.defaults.workspace = name
        return {"workspace": name}


async def serve_stdio(server: AcpServer) -> None:
    """Roda o servidor no stdin/stdout do processo (o stdout fica exclusivo do protocolo)."""
    from dengcode_engine.acp.stdio import process_stdio

    await server.serve(process_stdio())
