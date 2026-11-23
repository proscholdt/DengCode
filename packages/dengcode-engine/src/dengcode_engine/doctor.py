"""``stack doctor``: conecta em cada porta e faz uma operação inofensiva."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dengcode_core.errors import DengCodeError
from dengcode_core.llm.messages import ChatRequest, Message
from dengcode_core.ports.models import AdapterContext, PortKind
from dengcode_engine.config.schema import AdapterRef, StackConfig
from dengcode_engine.paths import workspace_dir
from dengcode_engine.registry import AdapterRegistry
from dengcode_engine.runtime import build_llm, create_adapter


@dataclass(frozen=True)
class Check:
    port: str
    adapter: str
    ok: bool
    detail: str
    elapsed_ms: int = 0


async def _probe(port: str, adapter: str, fn: Callable[[], Awaitable[str]]) -> Check:
    started = time.perf_counter()
    try:
        detail = await fn()
        ok = True
    except DengCodeError as exc:
        detail, ok = f"{type(exc).__name__}: {exc}", False
    except Exception as exc:  # o doctor nunca pode quebrar: reporta a causa
        detail, ok = f"erro inesperado {type(exc).__name__}: {exc}", False
    return Check(
        port,
        adapter,
        ok,
        detail.splitlines()[0][:200] if detail else "",
        int((time.perf_counter() - started) * 1000),
    )


async def run_doctor(
    stack: StackConfig,
    registry: AdapterRegistry,
    *,
    project_dir: Path,
    workspace: str,
    env: str,
    home: Path,
) -> list[Check]:
    ctx = AdapterContext(
        workspace=workspace,
        env=env,
        project_dir=project_dir,
        cache_dir=workspace_dir(workspace, home) / "cache",
    )
    checks: list[Check] = []

    if stack.llm is not None:

        async def llm_probe() -> str:
            llm, roles = build_llm(stack, registry, ctx)
            profile = llm.profile(roles.main)
            req = ChatRequest(model=roles.main, messages=[Message.text("user", "ping")])
            await llm.count_tokens(req)
            return (
                f"{roles.main} · contexto {profile.context_window} · tools {profile.supports_tools}"
            )

        checks.append(await _probe("llm", stack.llm.provider, llm_probe))

    peers: dict[str, Any] = {}
    for ref in stack.compute:

        async def engine_probe(ref: AdapterRef = ref) -> str:
            engine = create_adapter(registry, PortKind.QUERY_ENGINE, ref, ctx)
            peers.setdefault("query_engine", engine)
            result = await engine.execute("SELECT 1 AS ok", max_rows=1, timeout_s=30)
            return f"SELECT 1 em {result.elapsed_ms} ms ({engine.capabilities().sql_dialect})"

        checks.append(await _probe("query_engine", ref.adapter, engine_probe))

    pctx = AdapterContext(
        workspace=workspace, env=env, project_dir=project_dir, cache_dir=ctx.cache_dir, peers=peers
    )
    if stack.storage is not None:
        storage_ref = stack.storage

        async def storage_probe() -> str:
            storage = create_adapter(registry, PortKind.STORAGE, storage_ref, pctx)
            items = await storage.list("", limit=5)
            return f"{len(items)} item(ns) na raiz"

        checks.append(await _probe("storage", storage_ref.adapter, storage_probe))
    if stack.catalog is not None:
        catalog_ref = stack.catalog

        async def catalog_probe() -> str:
            catalog = create_adapter(registry, PortKind.CATALOG, catalog_ref, pctx)
            schemas = await catalog.list_schemas()
            return f"{len(schemas)} schema(s)"

        checks.append(await _probe("catalog", catalog_ref.adapter, catalog_probe))

    optional: list[tuple[str, PortKind, AdapterRef | None, Callable[[Any], Awaitable[str]]]] = [
        ("orchestrator", PortKind.ORCHESTRATOR, stack.orchestrator,
         lambda a: _count(a.list_pipelines(), "pipeline(s)")),
        ("stream", PortKind.STREAM, stack.streaming,
         lambda a: _count(a.list_topics(), "tópico(s)")),
        ("transform", PortKind.TRANSFORM, stack.transform,
         lambda a: _count(a.list_models(), "modelo(s)")),
    ]
    for port_name, port, ref, op in optional:
        if ref is None:
            continue

        async def probe(ref: AdapterRef = ref, port: PortKind = port,
                        op: Callable[[Any], Awaitable[str]] = op) -> str:
            return await op(create_adapter(registry, port, ref, pctx))

        checks.append(await _probe(port_name, ref.adapter, probe))
    return checks


async def _count(coro: Awaitable[list[Any]], label: str) -> str:
    return f"{len(await coro)} {label}"
