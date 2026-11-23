"""Critérios da fase 5: investigação com memória episódica e isolamento de workspaces.

FakeLLM roteirizado + orquestrador fake + memória SQLite real (FTS5).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from dengcode_core.agent.loop import ApprovalAnswer
from dengcode_core.events import ApprovalRequested
from dengcode_core.llm.messages import ToolResultPart
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_engine.runtime import Runtime, RuntimeOptions, build_runtime
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, FakeTurn, call, text
from dengcode_testkit.fake_ops import FakeOrchestrator

STACK = """name: t
llm: { provider: fake }
compute: [ { adapter: duckdb } ]
catalog: { adapter: duckdb }
orchestrator: { adapter: fake_orch }
memory: { adapter: sqlite }
"""


class FakeOrchAdapter:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="fake_orch", port=PortKind.ORCHESTRATOR, version="0"
    )

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> FakeOrchestrator:
        return FakeOrchestrator()


def _registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            *AdapterRegistry.discover().all(),
            RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter),
            RegisteredAdapter("fake_orch", None, FakeOrchAdapter.info, FakeOrchAdapter),
        ]
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".dengcode" / "stacks").mkdir(parents=True)
    (proj / ".dengcode" / "stacks" / "t.yaml").write_text(STACK, encoding="utf-8")
    return proj


async def _approve(req: ApprovalRequested) -> ApprovalAnswer:
    return ApprovalAnswer(True)


async def _rt(project: Path, home: Path, workspace: str, script: list[FakeTurn]) -> Runtime:
    rt = await build_runtime(
        RuntimeOptions(project_dir=project, stack="t", workspace=workspace),
        registry=_registry(),
        approver=_approve,
        home=home,
    )
    llm = rt.agent.llm
    assert isinstance(llm, FakeLLM)
    llm.script = script
    return rt


def _results(rt: Runtime) -> list[ToolResultPart]:
    llm = rt.agent.llm
    assert isinstance(llm, FakeLLM)
    return [p for m in llm.requests[-1].messages for p in m.tool_results()]


INCIDENT_BODY = (
    "**Sintoma:** a tarefa validar_pedidos falhou na execução de ontem.\n"
    "**Causa:** a origem passou a enviar pedidos sem id_cliente (12 nulos); a validação "
    "de qualidade bloqueia a carga.\n"
    "**Solução:** corrigir a extração na origem ou filtrar/quarentenar pedidos sem cliente "
    "antes da validação; reexecutar a carga."
)


async def test_why_did_the_dag_fail_and_remember(project: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = await _rt(
        project,
        home,
        "trabalho",
        [
            call("memory_search", query="carga_pedidos falhou validar_pedidos", kinds=["incident"]),
            call("pipeline_runs", pipeline_id="carga_pedidos"),
            call("pipeline_run_detail", run_id="carga_pedidos/r1"),
            call("pipeline_task_logs", run_id="carga_pedidos/r1", task_id="validar_pedidos"),
            call(
                "memory_propose",
                kind="incident",
                title="carga_pedidos falhou: id_cliente nulo na validação",
                body=INCIDENT_BODY,
                entities=["carga_pedidos", "pedidos.id_cliente"],
            ),
            text("A DAG falhou porque 12 pedidos chegaram sem id_cliente."),
        ],
    )
    assert "Investigação de falhas" in first.agent.system_prompt
    await first.agent.run("Por que a DAG carga_pedidos falhou ontem?")
    searched, runs, detail, logs, proposed = _results(first)
    assert "nada na memória" in searched.content
    assert "failed" in runs.content
    assert "validar_pedidos: failed" in detail.content
    assert "12 nulos" in logs.content
    assert "ana@x.com" not in logs.content  # o log tinha um e-mail: chega mascarado
    assert "confirmado pelo usuário" in proposed.content

    # segunda sessão, mesmo workspace: o incidente está no prompt e na busca
    second = await _rt(
        project,
        home,
        "trabalho",
        [
            call("memory_search", query="carga_pedidos falhou", kinds=["incident"]),
            text("Já aconteceu antes: mesmo incidente."),
        ],
    )
    assert "carga_pedidos falhou: id_cliente nulo" in second.agent.system_prompt
    await second.agent.run("Por que a DAG carga_pedidos falhou ontem?")
    [found] = _results(second)
    assert "carga_pedidos falhou: id_cliente nulo na validação" in found.content
    assert "verified" in found.content


async def test_workspaces_never_see_each_other(project: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    a = await _rt(
        project,
        home,
        "cliente-a",
        [
            call(
                "memory_propose",
                kind="glossary",
                title="receita líquida",
                body="Receita sem impostos e devoluções, no grão do pedido.",
                entities=["gold.vendas"],
            ),
            call("memory_search", query="receita líquida"),
            text("ok"),
        ],
    )
    await a.agent.run("guarde o termo")
    assert "receita líquida" in _results(a)[1].content

    b = await _rt(
        project,
        home,
        "cliente-b",
        [
            call("memory_search", query="receita líquida"),
            call("memory_search", query="receita"),
            text("ok"),
        ],
    )
    assert "receita líquida" not in b.agent.system_prompt
    await b.agent.run("procure o termo")
    for res in _results(b):
        assert "nada na memória" in res.content
    # os arquivos de memória também são separados por workspace
    assert (home / "workspaces" / "cliente-a" / "memory.db").exists()
    assert (home / "workspaces" / "cliente-b" / "memory.db").exists()


async def test_catalog_sync_and_stale_detection(project: Path, tmp_path: Path) -> None:
    from dengcode_engine.memory import sync_catalog

    home = tmp_path / "home"
    rt = await _rt(project, home, "w", [call("catalog_describe", table="clientes"), text("ok")])
    engine = rt.data.engines["duckdb"]
    await engine.execute("CREATE TABLE clientes (id INTEGER, email VARCHAR)")
    assert rt.data.catalog is not None
    assert rt.data.memory is not None
    assert rt.data.scope is not None
    report = await sync_catalog(rt.data.catalog, rt.data.memory, rt.data.scope, source="duckdb")
    assert report.tables == 1
    await engine.execute("ALTER TABLE clientes ADD COLUMN cidade VARCHAR")
    await rt.agent.run("descreva clientes")
    [described] = _results(rt)
    assert "o schema mudou desde o registro na memória" in described.content
    from dengcode_core.ports.memory import MemoryKind

    [rec] = await rt.data.memory.list(rt.data.scope, kinds=[MemoryKind.CATALOG_TABLE])
    assert rec.id.endswith("main.clientes")
    assert "cidade" in rec.body  # o registro foi atualizado com o ao vivo
