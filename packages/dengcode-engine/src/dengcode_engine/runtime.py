"""Composição: stack + adapters registrados → um ``Agent`` pronto para uso.

É o único lugar que liga porta → adapter concreto (via registro de entry points).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dengcode_core.agent.loop import Agent, Approver, Budget, ModelRoles, deny_all
from dengcode_core.checkpoints import CheckpointStore
from dengcode_core.errors import ConfigError
from dengcode_core.events import EventBus, ProviderFallback
from dengcode_core.llm.fallback import FallbackLLM
from dengcode_core.llm.port import LLMPort
from dengcode_core.policy.permission import (
    DEFAULT_SHELL_ALLOWLIST,
    PermissionMode,
    PermissionPolicy,
)
from dengcode_core.ports.models import AdapterContext, PortKind
from dengcode_core.prompts.render import render_system_prompt
from dengcode_core.session import Session, SessionMeta, SessionStore
from dengcode_core.tools.base import Tool, ToolContext
from dengcode_core.tools.builtin import builtin_tools
from dengcode_core.tools.registry import ToolRegistry
from dengcode_engine.config.credentials import resolve_value
from dengcode_engine.config.loader import StackLoader
from dengcode_engine.config.schema import PLACEHOLDER_MARK, LLMConfig, StackConfig
from dengcode_engine.paths import dengcode_home, sessions_dir, workspace_dir
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


def build_runtime(
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
    tools = ToolRegistry([*builtin_tools(), *(extra_tools or [])])
    tool_ctx = ToolContext(
        workspace_root=project_dir,
        session_id=session.id,
        env=opts.env,
        checkpoints=CheckpointStore.for_session(project_dir, session.id),
    )
    prompt = session.meta.system_prompt or render_system_prompt(
        workspace=opts.workspace,
        project_dir=str(project_dir),
        stack=stack.name,
        env=opts.env,
        mode=policy.mode.value,
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
    )
    return Runtime(
        agent=agent,
        stack=stack,
        options=opts,
        registry=registry,
        store=store,
        adapters={"llm": llm},
    )
