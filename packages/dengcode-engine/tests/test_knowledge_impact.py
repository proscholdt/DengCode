from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from dengcode_core.errors import NotFoundError
from dengcode_core.llm.messages import ToolResultPart
from dengcode_core.ports.models import AdapterContext, AdapterInfo, LineageEdge, PortKind
from dengcode_core.ports.transform import ModelInfo, NodeResult, TransformRunResult
from dengcode_engine.knowledge_loader import folder_entries, load_knowledge
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_engine.runtime import RuntimeOptions, build_runtime
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, call, text

SILVER = "select cast(id as int) as id_pedido, canal from main.bronze_pedidos"
GOLD = "select canal, count(*) as n from main.silver_pedidos group by 1"


class FakeTransform:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="fake_transform", port=PortKind.TRANSFORM, version="0"
    )
    name = "fake_transform"
    runs: ClassVar[list[str]] = []

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> FakeTransform:
        return cls()

    async def list_models(self, selector: str | None = None) -> list[ModelInfo]:
        return [
            ModelInfo(
                name="silver_pedidos",
                unique_id="m.s",
                relation="main.silver_pedidos",
                depends_on=["bronze_pedidos"],
            ),
            ModelInfo(
                name="gold", unique_id="m.g", relation="main.gold", depends_on=["silver_pedidos"]
            ),
        ]

    async def compile(self, model: str) -> str:
        return {"silver_pedidos": SILVER, "gold": GOLD}[model]

    async def run(self, selector: str, *, target: str) -> TransformRunResult:
        self.runs.append(f"run {selector} {target}")
        return TransformRunResult(
            success=True,
            command="run",
            selector=selector,
            target=target,
            results=[NodeResult(name="silver_pedidos", status="success")],
        )

    async def test(self, selector: str, *, target: str) -> TransformRunResult:
        return TransformRunResult(
            success=False,
            command="test",
            selector=selector,
            target=target,
            logs="falhou not_null",
            results=[NodeResult(name="not_null_x", status="fail", message="1 linha")],
        )

    async def manifest_lineage(self) -> list[LineageEdge]:
        return [LineageEdge(source="main.silver_pedidos", target="main.gold", via="gold")]


STACK = """name: t
llm: { provider: fake }
compute: [ { adapter: duckdb } ]
transform: { adapter: fake_transform }
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".dengcode" / "stacks").mkdir(parents=True)
    (proj / ".dengcode" / "stacks" / "t.yaml").write_text(STACK, encoding="utf-8")
    (proj / ".dengcode" / "knowledge").mkdir()
    (proj / ".dengcode" / "knowledge" / "convencoes.md").write_text(
        "# Convenções do time\n\nTabelas silver usam prefixo silver_.\n", encoding="utf-8"
    )
    return proj


def _registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            *AdapterRegistry.discover().all(),
            RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter),
            RegisteredAdapter("fake_transform", None, FakeTransform.info, FakeTransform),
        ]
    )


def test_folder_entries_and_precedence(tmp_path: Path, project: Path) -> None:
    [entry] = folder_entries(project / ".dengcode" / "knowledge", "projeto")
    assert entry.id == "convencoes"
    assert entry.title == "Convenções do time"
    assert entry.summary.startswith("Tabelas silver")
    home = tmp_path / "home"
    (home / "knowledge").mkdir(parents=True)
    (home / "knowledge" / "convencoes.md").write_text("# pessoal\n\nx\n", encoding="utf-8")
    kb = load_knowledge(adapters=[], home=home, project_dir=project)
    assert kb.read("convencoes").startswith("# Convenções do time")  # projeto > pessoal
    with pytest.raises(NotFoundError):
        kb.read("nao-existe")


async def test_runtime_wires_transform_impact_and_knowledge(project: Path, tmp_path: Path) -> None:
    rt = build_runtime(
        RuntimeOptions(project_dir=project, stack="t"), registry=_registry(), home=tmp_path / "home"
    )
    names = set(rt.agent.tools.names())
    assert {
        "transform_list_models",
        "transform_run",
        "transform_test",
        "impact_analysis",
        "data_snapshot",
        "data_diff",
        "knowledge_read",
    } <= names
    assert "Fluxo para mudar um modelo" in rt.agent.system_prompt
    assert "`convencoes`" in rt.agent.system_prompt
    llm = rt.agent.llm
    assert isinstance(llm, FakeLLM)
    llm.script = [
        call("impact_analysis", target="bronze_pedidos.canal"),
        call("impact_analysis", target="silver_pedidos.canal_venda"),
        call("transform_test", selector="silver_pedidos+"),
        call("knowledge_read", id="convencoes"),
        text("pronto"),
    ]
    await rt.agent.run("analise")
    results: list[ToolResultPart] = [p for m in llm.requests[-1].messages for p in m.tool_results()]
    col, new_col, tested, kb = results
    assert "main.gold.canal" in col.content
    assert "main.bronze_pedidos.canal → main.silver_pedidos.canal → main.gold.canal" in (
        col.content
    )
    assert "ainda não existe no lineage" in new_col.content
    assert "main.gold" in new_col.content
    assert tested.is_error
    assert "not_null_x: fail" in tested.content
    assert "Tabelas silver" in kb.content


async def test_transform_run_asks_and_snapshot_is_free_in_dev(
    project: Path, tmp_path: Path
) -> None:
    asked: list[str] = []

    async def approver(req: Any) -> Any:
        from dengcode_core.agent.loop import ApprovalAnswer

        asked.append(req.name)
        return ApprovalAnswer(True)

    rt = build_runtime(
        RuntimeOptions(project_dir=project, stack="t"),
        registry=_registry(),
        approver=approver,
        home=tmp_path / "home",
    )
    llm = rt.agent.llm
    assert isinstance(llm, FakeLLM)
    await rt.data.engines["duckdb"].execute("CREATE TABLE gold AS SELECT 1 AS id, 'site' AS c")
    llm.script = [
        call("data_snapshot", table="gold"),
        call("transform_run", selector="silver_pedidos+"),
        call(
            "data_diff",
            before=f"dengcode_tmp_{rt.agent.session.id}.gold_antes",
            after="gold",
            key=["id"],
        ),
        text("ok"),
    ]
    await rt.agent.run("mude")
    assert asked == ["transform_run"]  # o snapshot no schema temporário não pede aprovação
    assert FakeTransform.runs[-1] == "run silver_pedidos+ dev"
    results = [p for m in llm.requests[-1].messages for p in m.tool_results()]
    assert "cópia criada" in results[0].content
    assert "alertas: nenhum" in results[2].content
