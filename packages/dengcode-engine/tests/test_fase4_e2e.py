"""Critério da fase 4 com dbt e DuckDB reais (FakeLLM roteirizado, sem rede).

"Adicione a coluna canal_venda em silver_pedidos e propague até
gold_vendas_diarias" → impacto, edições aprovadas, run/test verdes e data diff.
Roda o dbt de verdade (~1 min): marcado como integração.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from dengcode_core.agent.loop import ApprovalAnswer
from dengcode_core.events import ApprovalRequested
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_engine.runtime import RuntimeOptions, build_runtime
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, call, text

pytestmark = pytest.mark.integration

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "ecommerce"
SILVER = Path("dbt/models/silver/silver_pedidos.sql")
GOLD = Path("dbt/models/gold/gold_vendas_diarias.sql")
GOLD_SCHEMA = Path("dbt/models/gold/schema.yml")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "ecommerce"
    shutil.copytree(
        EXAMPLE,
        proj,
        ignore=shutil.ignore_patterns(
            "target", "logs", "*.duckdb", "*.duckdb.wal", ".user.yml", "gerar_dados.py"
        ),
    )
    stacks = proj / ".dengcode" / "stacks"
    (stacks / "t.yaml").write_text(
        "name: t\nextends: ecommerce\nllm: { provider: fake }\n", encoding="utf-8"
    )
    return proj


def _registry() -> AdapterRegistry:
    fake = RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter)
    return AdapterRegistry([*AdapterRegistry.discover().all(), fake])


async def test_add_column_and_propagate(project: Path, tmp_path: Path) -> None:
    asked: list[ApprovalRequested] = []

    async def approve(req: ApprovalRequested) -> ApprovalAnswer:
        asked.append(req)
        return ApprovalAnswer(True)

    rt = build_runtime(
        RuntimeOptions(project_dir=project, stack="t"),
        registry=_registry(),
        approver=approve,
        home=tmp_path / "home",
    )
    transform = next(t for t in rt.agent.tools.names() if t == "transform_run")
    assert transform
    # estado inicial: o projeto materializado como está hoje
    base = await rt.agent.tools.get("transform_run").transform.run(  # type: ignore[union-attr]
        "+gold_vendas_diarias", target="dev"
    )
    assert base.success, base.logs

    silver = (project / SILVER).read_text(encoding="utf-8")
    gold = (project / GOLD).read_text(encoding="utf-8")
    old_silver = "        lower(trim(status)) as status,\n"
    new_silver = old_silver + "        lower(trim(canal)) as canal_venda,\n"
    assert old_silver in silver
    old_gold_sel = "    data_pedido,\n    count(*)"
    old_gold_grp = "group by data_pedido"
    assert old_gold_sel in gold
    assert old_gold_grp in gold
    snap = f"dengcode_tmp_{rt.agent.session.id}"

    llm = rt.agent.llm
    assert isinstance(llm, FakeLLM)
    llm.script = [
        call("transform_list_models"),
        call("impact_analysis", target="silver_pedidos.canal_venda"),
        call("data_snapshot", table="main.silver_pedidos"),
        call("data_snapshot", table="main.gold_vendas_diarias"),
        call("file_edit", path=SILVER.as_posix(), old_text=old_silver, new_text=new_silver),
        call(
            "file_edit",
            path=GOLD.as_posix(),
            old_text=old_gold_sel,
            new_text="    data_pedido,\n    canal_venda,\n    count(*)",
        ),
        call(
            "file_edit",
            path=GOLD.as_posix(),
            old_text=old_gold_grp,
            new_text="group by data_pedido, canal_venda",
        ),
        # o grão mudou para (data, canal): o teste de unicidade acompanha
        call(
            "file_edit",
            path=GOLD_SCHEMA.as_posix(),
            old_text="          - not_null\n          - unique\n",
            new_text="          - not_null\n",
        ),
        call(
            "file_edit",
            path=GOLD_SCHEMA.as_posix(),
            old_text="    columns:\n      - name: data_pedido\n",
            new_text=(
                "    data_tests:\n      - unique:\n          column_name: "
                "\"cast(data_pedido as varchar) || '|' || canal_venda\"\n"
                "    columns:\n      - name: canal_venda\n"
                "        description: Canal da venda (site, app, loja, marketplace).\n"
                "      - name: data_pedido\n"
            ),
        ),
        call("transform_compile", model="gold_vendas_diarias"),
        call("transform_run", selector="silver_pedidos+"),
        call("transform_test", selector="silver_pedidos+"),
        call(
            "data_diff",
            before=f"{snap}.silver_pedidos_antes",
            after="main.silver_pedidos",
            key=["id_pedido"],
        ),
        call(
            "data_diff",
            before=f"{snap}.gold_vendas_diarias_antes",
            after="main.gold_vendas_diarias",
            key=["data_pedido"],
        ),
        text("Relatório: canal_venda adicionado e propagado."),
    ]
    result = await rt.agent.run(
        "adicione a coluna canal_venda em silver_pedidos e propague até gold_vendas_diarias"
    )
    assert result.reason == "end", result.detail

    by_name: dict[str, Any] = {}
    calls = [p for m in llm.requests[-1].messages for p in m.tool_calls()]
    results = {p.call_id: p for m in llm.requests[-1].messages for p in m.tool_results()}
    for c in calls:
        by_name.setdefault(c.name, []).append(results[c.id])
    errors = {n: [r.content[:400] for r in rs if r.is_error] for n, rs in by_name.items()}
    errors = {n: v for n, v in errors.items() if v}
    assert not errors, errors

    impact = by_name["impact_analysis"][0].content
    assert "gold_vendas_diarias" in impact
    # só as escritas de verdade pediram aprovação (snapshots no schema temporário, não)
    assert [r.name for r in asked] == ["file_edit"] * 5 + ["transform_run"]
    assert "canal_venda" in by_name["transform_compile"][0].content
    assert "SUCESSO" in by_name["transform_run"][0].content
    assert "SUCESSO" in by_name["transform_test"][0].content
    silver_diff, gold_diff = (r.content for r in by_name["data_diff"])
    assert "colunas novas: canal_venda" in silver_diff
    assert "linhas: 300 → 300" in silver_diff  # nada perdido nem duplicado
    assert "colunas novas: canal_venda" in gold_diff
    assert "chaves duplicadas" in gold_diff  # o grão mudou: (data, canal)
