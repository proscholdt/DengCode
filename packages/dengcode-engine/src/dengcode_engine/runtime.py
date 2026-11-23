"""Composição: stack + adapters registrados → um ``Agent`` pronto para uso.

É o único lugar que liga porta → adapter concreto (via registro de entry points).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dengcode_core.agent.loop import Agent, Approver, Budget, ModelRoles, deny_all
from dengcode_core.audit import AuditLog
from dengcode_core.checkpoints import CheckpointStore
from dengcode_core.data.results import ResultStore
from dengcode_core.errors import ConfigError
from dengcode_core.events import EventBus, ProviderFallback
from dengcode_core.knowledge import KnowledgeBase
from dengcode_core.llm.fallback import FallbackLLM
from dengcode_core.llm.port import LLMPort
from dengcode_core.policy.permission import (
    DEFAULT_SHELL_ALLOWLIST,
    PermissionMode,
    PermissionPolicy,
)
from dengcode_core.ports.memory import Scope
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind
from dengcode_core.privacy.pii import PiiMasker
from dengcode_core.prompts.render import render_system_prompt
from dengcode_core.session import Session, SessionMeta, SessionStore
from dengcode_core.sql.cost import CostLimit
from dengcode_core.tools.base import Tool, ToolContext
from dengcode_core.tools.builtin import builtin_tools
from dengcode_core.tools.data import DataServices, data_tools
from dengcode_core.tools.ops import ops_tools
from dengcode_core.tools.registry import ToolRegistry
from dengcode_core.tools.transform import transform_tools
from dengcode_engine.config.credentials import resolve_value
from dengcode_engine.config.loader import StackLoader
from dengcode_engine.config.schema import PLACEHOLDER_MARK, AdapterRef, LLMConfig, StackConfig
from dengcode_engine.knowledge_loader import load_knowledge
from dengcode_engine.memory import workspace_summary
from dengcode_engine.paths import dengcode_home, sessions_dir, workspace_dir
from dengcode_engine.project import load_project
from dengcode_engine.registry import AdapterRegistry

log = logging.getLogger(__name__)


@dataclass
class RuntimeOptions:
    project_dir: Path
    stack: str = "local"
    env: str = "dev"
    workspace: str = "default"
    mode: str | None = None
    llm: str | None = None  # "provedor" ou "provedor:modelo"
    session_id: str | None = None
    max_steps: int = 40
    max_usd: float | None = None


@dataclass
class Runtime:
    agent: Agent
    stack: StackConfig
    options: RuntimeOptions
    registry: AdapterRegistry
    store: SessionStore
    data: DataServices
    adapters: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())

    @property
    def bus(self) -> EventBus:
        return self.agent.bus


def parse_llm_override(spec: str) -> tuple[str, str | None]:
    provider, _, model = spec.partition(":")
    if not provider:
        raise ConfigError("--llm precisa do provedor, ex.: anthropic ou openai:gpt-5")
    return provider, model or None


def _roles(cfg: LLMConfig, cls: Any) -> ModelRoles:
    defaults: dict[str, str] = dict(getattr(cls, "default_models", {}) or {})
    main = cfg.models.main if cfg.models else None
    fast = cfg.models.fast if cfg.models else None
    main = main or defaults.get("main")
    fast = fast or defaults.get("fast") or main
    if not main or main.startswith(PLACEHOLDER_MARK) or (fast or "").startswith(PLACEHOLDER_MARK):
        raise ConfigError(
            f"modelo do LLM '{cfg.provider}' não configurado: defina llm.models.main na stack "
            "ou use --llm provedor:modelo"
        )
    return ModelRoles(main=main, fast=fast or main)


def _create_llm(
    cfg: LLMConfig, registry: AdapterRegistry, ctx: AdapterContext
) -> tuple[LLMPort, ModelRoles]:
    cls = registry.get(PortKind.LLM, cfg.provider)
    roles = _roles(cfg, cls)
    llm: LLMPort = cls.from_config(resolve_value(cfg.adapter_config()), ctx)
    return llm, roles


def build_llm(
    stack: StackConfig,
    registry: AdapterRegistry,
    ctx: AdapterContext,
    *,
    override: str | None = None,
    bus: EventBus | None = None,
) -> tuple[LLMPort, ModelRoles]:
    cfg = stack.llm
    if override:
        provider, model = parse_llm_override(override)
        if cfg is not None and cfg.provider == provider:
            data = cfg.model_dump(exclude_none=True)
            if model:
                data.pop("models", None)
                data["model"] = model
            cfg = LLMConfig.model_validate(data)
        else:
            cfg = LLMConfig(provider=provider, model=model)
    if cfg is None:
        raise ConfigError(f"a stack {stack.name} não define um LLM (seção llm)")
    primary, roles = _create_llm(cfg, registry, ctx)
    if cfg.fallback is None or override:
        return primary, roles
    backup, backup_roles = _create_llm(cfg.fallback, registry, ctx)

    async def on_fallback(src: str, dst: str, reason: str) -> None:
        if bus is not None:
            await bus.publish(ProviderFallback(from_provider=src, to_provider=dst, reason=reason))

    mapping = {roles.main: backup_roles.main, roles.fast: backup_roles.fast}
    return FallbackLLM(primary, backup, mapping, on_fallback), roles


def create_adapter(
    registry: AdapterRegistry, port: PortKind, ref: AdapterRef, ctx: AdapterContext
) -> Any:
    return registry.create(port, ref.adapter, resolve_value(ref.adapter_config()), ctx)


def build_data_services(
    stack: StackConfig,
    registry: AdapterRegistry,
    ctx: AdapterContext,
    *,
    env: str,
    home: Path,
    masker: PiiMasker,
) -> DataServices:
    """Constrói compute → storage → catálogo (o catálogo pode reusar o motor via peers)."""
    engines: dict[str, Any] = {}
    metered: dict[str, bool] = {}
    for ref in stack.compute:
        key = ref.adapter if ref.adapter not in engines else f"{ref.adapter}_{len(engines)}"
        engines[key] = create_adapter(registry, PortKind.QUERY_ENGINE, ref, ctx)
        metered[key] = bool((ref.model_extra or {}).get("metered", False))
    peers: dict[str, Any] = {}
    if engines:
        peers["query_engine"] = next(iter(engines.values()))
    pctx = AdapterContext(
        workspace=ctx.workspace,
        env=ctx.env,
        secrets=ctx.secrets,
        logger=ctx.logger,
        cache_dir=ctx.cache_dir,
        project_dir=ctx.project_dir,
        peers=peers,
    )
    storage = (
        create_adapter(registry, PortKind.STORAGE, stack.storage, pctx) if stack.storage else None
    )
    catalog = (
        create_adapter(registry, PortKind.CATALOG, stack.catalog, pctx) if stack.catalog else None
    )
    env_cfg = stack.environment(env)
    limit = None
    if env_cfg.cost_limit is not None:
        limit = CostLimit(usd=env_cfg.cost_limit.usd, bytes=env_cfg.cost_limit.bytes)
    ws_dir = workspace_dir(ctx.workspace, home)
    return DataServices(
        engines=engines,
        catalog=catalog,
        storage=storage,
        results=ResultStore(ws_dir / "cache"),
        masker=masker,
        audit=AuditLog(ws_dir / "audit.db"),
        workspace=ctx.workspace,
        stack=stack.name,
        env=env,
        cost_limit=limit,
        metered=metered,
    )


MODEL_CHANGE_FLOW = """Ao mudar um modelo de transformação, siga esta ordem:
1. Localize o modelo (`transform_list_models`) e leia o arquivo.
2. Rode `impact_analysis` no modelo/coluna e liste o que é afetado downstream.
3. Faça `data_snapshot` das relações que vão mudar (o modelo e os afetados).
4. Edite os arquivos (cada edição mostra o diff e pede aprovação).
5. `transform_compile` para conferir o SQL; depois `transform_run` em dev com o
   seletor `modelo+` (o modelo e tudo abaixo dele).
6. `transform_test` com o mesmo seletor.
7. `data_diff` entre cada cópia do snapshot e a relação nova (use a chave do modelo).
8. Relatório final: impacto, arquivos alterados, resultado dos testes e o que o
   data diff mostrou (e se é o esperado)."""


def prompt_sections(
    knowledge: KnowledgeBase, *, has_transform: bool, has_data: bool
) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    if has_transform:
        sections.append(("Fluxo para mudar um modelo", MODEL_CHANGE_FLOW))
    elif has_data:
        sections.append(
            (
                "Verificação com dados",
                "Antes de mudar uma tabela, faça `data_snapshot`; depois compare com `data_diff`.",
            )
        )
    if len(knowledge):
        sections.append(
            ("Conhecimento disponível (leia com `knowledge_read`)", knowledge.index_text())
        )
    return sections


def _stack_infos(stack: StackConfig, registry: AdapterRegistry) -> list[AdapterInfo]:
    refs: list[tuple[PortKind, str]] = [(PortKind.QUERY_ENGINE, c.adapter) for c in stack.compute]
    for port, ref in (
        (PortKind.STORAGE, stack.storage),
        (PortKind.CATALOG, stack.catalog),
        (PortKind.TRANSFORM, stack.transform),
        (PortKind.ORCHESTRATOR, stack.orchestrator),
        (PortKind.STREAM, stack.streaming),
    ):
        if ref is not None:
            refs.append((port, ref.adapter))
    return [registry.get(port, name).info for port, name in refs if registry.has(port, name)]


def build_policy(stack: StackConfig, env: str, mode: str | None) -> PermissionPolicy:
    env_cfg = stack.environment(env)
    chosen = PermissionMode(mode or env_cfg.permission_mode)
    if env_cfg.read_only:
        chosen = PermissionMode.READ_ONLY
    if env == "prod" and chosen is PermissionMode.AUTO:
        log.warning("modo auto não vale em prod; usando ask")
        chosen = PermissionMode.ASK
    allow = stack.policy.shell_allowlist
    return PermissionPolicy(
        mode=chosen,
        env=env,
        shell_allowlist=tuple(allow) if allow is not None else DEFAULT_SHELL_ALLOWLIST,
        protected_tables=tuple(stack.policy.protected_tables),
        allow_prod_writes=env_cfg.allow_prod_writes,
    )


INVESTIGATION_FLOW = """Ao investigar uma falha de pipeline ou de dados:
1. Antes de tudo, `memory_search` com o nome da pipeline/tabela e o sintoma (tipo
   incident) — se já aconteceu, cite o incidente e confira se a causa se repete.
2. `pipeline_runs` → `pipeline_run_detail` da execução com falha → `pipeline_task_logs`
   da tarefa que falhou. Use as ferramentas de dados para confirmar a causa.
3. Explique a causa com evidências e proponha a correção.
4. Ao final, `memory_propose` do tipo incident com: sintoma, causa e solução (sem
   valores de linha nem dados pessoais) e as entidades envolvidas."""


@dataclass
class OpsAdapters:
    orchestrator: Any = None
    stream: Any = None
    stream_compute: Any = None
    embedding: Any = None
    memory: Any = None


def build_ops(stack: StackConfig, registry: AdapterRegistry, ctx: AdapterContext) -> OpsAdapters:
    """Orquestração, streaming, embeddings e memória. Memória sem adapter instalado = sem
    memória (aviso), para o motor seguir funcionando."""
    ops = OpsAdapters()
    if stack.orchestrator:
        ops.orchestrator = create_adapter(registry, PortKind.ORCHESTRATOR, stack.orchestrator, ctx)
    if stack.streaming:
        ops.stream = create_adapter(registry, PortKind.STREAM, stack.streaming, ctx)
    if stack.stream_compute:
        ops.stream_compute = create_adapter(
            registry, PortKind.STREAM_COMPUTE, stack.stream_compute[0], ctx
        )
    if stack.embedding and registry.has(PortKind.EMBEDDING, stack.embedding.provider):
        cfg = {"model": stack.embedding.model, **(stack.embedding.model_extra or {})}
        ops.embedding = registry.create(
            PortKind.EMBEDDING,
            stack.embedding.provider,
            resolve_value({k: v for k, v in cfg.items() if v}),
            ctx,
        )
    elif stack.embedding:
        log.warning(
            "embedding '%s' não instalado: memória só com busca textual", stack.embedding.provider
        )
    if registry.has(PortKind.MEMORY_STORE, stack.memory.adapter):
        mctx = replace(ctx, peers={**ctx.peers, "embedding": ops.embedding})
        ops.memory = create_adapter(registry, PortKind.MEMORY_STORE, stack.memory, mctx)
    else:
        log.warning("memória '%s' não instalada: sessão sem memória", stack.memory.adapter)
    return ops


async def build_runtime(
    opts: RuntimeOptions,
    *,
    registry: AdapterRegistry | None = None,
    approver: Approver = deny_all,
    bus: EventBus | None = None,
    home: Path | None = None,
    extra_tools: list[Tool[Any]] | None = None,
) -> Runtime:
    home = home or dengcode_home()
    registry = registry or AdapterRegistry.discover()
    project_dir = opts.project_dir.resolve()
    stack = StackLoader(project_dir, home).load(opts.stack)
    bus = bus or EventBus()
    cache = workspace_dir(opts.workspace, home) / "cache"
    ctx = AdapterContext(
        workspace=opts.workspace, env=opts.env, cache_dir=cache, project_dir=project_dir
    )
    llm, roles = build_llm(stack, registry, ctx, override=opts.llm, bus=bus)
    policy = build_policy(stack, opts.env, opts.mode)
    project_notes, project_protected = load_project(project_dir)
    if project_protected:
        policy.protected_tables = tuple({*policy.protected_tables, *project_protected})
    masker = PiiMasker()
    data = build_data_services(stack, registry, ctx, env=opts.env, home=home, masker=masker)
    scope = Scope(workspace=opts.workspace, project=str(project_dir), env=opts.env)
    ops = build_ops(stack, registry, ctx)
    data.memory, data.scope = ops.memory, scope

    store = SessionStore(sessions_dir(opts.workspace, home))
    if opts.session_id:
        session = store.load(opts.session_id)
    else:
        session = Session(
            SessionMeta(
                workspace=opts.workspace,
                project_dir=str(project_dir),
                stack=stack.name,
                env=opts.env,
            )
        )
    transform = (
        create_adapter(registry, PortKind.TRANSFORM, stack.transform, ctx)
        if stack.transform
        else None
    )
    knowledge = load_knowledge(
        adapters=_stack_infos(stack, registry), home=home, project_dir=project_dir
    )
    tools = ToolRegistry(
        [
            *builtin_tools(),
            *data_tools(data),
            *transform_tools(data, transform, default_target=opts.env, knowledge=knowledge),
            *ops_tools(
                orchestrator=ops.orchestrator,
                stream=ops.stream,
                stream_compute=ops.stream_compute,
                memory=ops.memory,
                scope=scope,
            ),
            *(extra_tools or []),
        ]
    )
    tool_ctx = ToolContext(
        workspace_root=project_dir,
        session_id=session.id,
        env=opts.env,
        checkpoints=CheckpointStore.for_session(project_dir, session.id),
    )
    prompt = session.meta.system_prompt
    if prompt is None:
        sections: list[tuple[str, str]] = []
        if project_notes:
            sections.append(("Regras do projeto (.dengcode/PROJETO.md)", project_notes))
        if ops.memory is not None:
            sections.append(
                (
                    f"Memória do workspace `{opts.workspace}`",
                    await workspace_summary(ops.memory, scope),
                )
            )
        if ops.orchestrator is not None:
            sections.append(("Investigação de falhas", INVESTIGATION_FLOW))
        sections += prompt_sections(
            knowledge, has_transform=transform is not None, has_data=bool(data.engines)
        )
        prompt = render_system_prompt(
            workspace=opts.workspace,
            project_dir=str(project_dir),
            stack=stack.name,
            env=opts.env,
            mode=policy.mode.value,
            sections=sections,
        )
    session.meta.system_prompt = prompt
    agent = Agent(
        llm=llm,
        models=roles,
        tools=tools,
        policy=policy,
        tool_ctx=tool_ctx,
        session=session,
        system_prompt=prompt,
        approver=approver,
        bus=bus,
        budget=Budget(max_steps=opts.max_steps, max_usd=opts.max_usd),
        store=store,
        masker=masker,
    )
    return Runtime(
        agent=agent,
        stack=stack,
        options=opts,
        registry=registry,
        store=store,
        data=data,
        adapters={
            "llm": llm,
            "transform": transform,
            "orchestrator": ops.orchestrator,
            "stream": ops.stream,
            "memory": ops.memory,
        },
    )
