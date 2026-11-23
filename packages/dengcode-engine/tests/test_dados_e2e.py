"""Critérios da fase 3 com DuckDB + disco local + FakeLLM (sem rede)."""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from dengcode_core.agent.loop import ApprovalAnswer
from dengcode_core.audit import AuditLog
from dengcode_core.events import ApprovalRequested
from dengcode_core.llm.messages import ToolResultPart
from dengcode_engine.paths import workspace_dir
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_engine.runtime import Runtime, RuntimeOptions, build_runtime
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, FakeToolCall, FakeTurn, call, text

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "ecommerce"

STACK = """name: t
llm: { provider: fake }
storage: { adapter: local_fs, root: "." }
compute: [ { adapter: duckdb, database: ".dengcode/t.duckdb" } ]
catalog: { adapter: duckdb }
environments:
  dev: { permission_mode: ask }
  prod: { permission_mode: ask }
"""

SILVER = """CREATE TABLE silver_pedidos AS
SELECT DISTINCT CAST(id_pedido AS INTEGER) AS id_pedido,
       CAST(id_cliente AS INTEGER) AS id_cliente,
       COALESCE(TRY_STRPTIME(data_pedido, '%Y-%m-%d'),
                TRY_STRPTIME(data_pedido, '%d/%m/%Y'))::DATE AS data_pedido,
       LOWER(TRIM(status)) AS status,
       CAST(TRIM(valor_total) AS DECIMAL(12, 2)) AS valor_total
FROM read_csv('dados/pedidos.csv', all_varchar = true)"""


class Approvals:
    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[ApprovalRequested] = []

    async def __call__(self, req: ApprovalRequested) -> ApprovalAnswer:
        self.asked.append(req)
        return ApprovalAnswer(self.answers.pop(0) if self.answers else False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    shutil.copytree(EXAMPLES, proj / "dados", ignore=shutil.ignore_patterns("*.py", "dbt"))
    (proj / ".dengcode" / "stacks").mkdir(parents=True)
    (proj / ".dengcode" / "stacks" / "t.yaml").write_text(STACK, encoding="utf-8")
    return proj


async def _runtime(
    project: Path, home: Path, script: list[FakeTurn], approver: Approvals, env: str = "dev"
) -> tuple[Runtime, FakeLLM]:
    fake = RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter)
    registry = AdapterRegistry([*AdapterRegistry.discover().all(), fake])
    rt = await build_runtime(
        RuntimeOptions(project_dir=project, stack="t", env=env),
        registry=registry,
        approver=approver,
        home=home,
    )
    llm = rt.agent.llm
    assert isinstance(llm, FakeLLM)
    llm.script = script
    return rt, llm


def _pii_values() -> list[str]:
    with (EXAMPLES / "clientes.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return (
        [r["email"] for r in rows]
        + [r["cpf"] for r in rows]
        + [re.sub(r"\D", "", r["cpf"]) for r in rows]
    )


def _results(llm: FakeLLM) -> list[ToolResultPart]:
    """Resultados de ferramenta do histórico final (a última requisição contém todos)."""
    return [p for m in llm.requests[-1].messages for p in m.tool_results()]


async def test_pii_never_reaches_the_llm(project: Path, tmp_path: Path) -> None:
    script = [
        FakeTurn(
            tool_calls=[
                FakeToolCall("storage_sample", {"uri": "dados/clientes.csv", "rows": 50}),
                FakeToolCall("file_read", {"path": "dados/clientes.csv"}),
                FakeToolCall("query_run", {"sql": "SELECT * FROM read_csv('dados/clientes.csv')"}),
                FakeToolCall(
                    "query_run",
                    {"sql": "SELECT email, cpf FROM 'dados/clientes.csv' WHERE id_cliente < 5"},
                ),
            ]
        ),
        text("Vi os clientes."),
    ]
    rt, llm = await _runtime(project, tmp_path / "home", script, Approvals())
    result = await rt.agent.run("explore os clientes")
    assert result.reason == "end"
    results = _results(llm)
    assert len(results) == 4
    assert not any(r.is_error for r in results), [r.content[:200] for r in results]
    sent = llm.sent_text()
    leaked = [v for v in _pii_values() if v in sent]
    assert leaked == []
    assert "***.***.***-" in sent  # a máscara preserva o formato


async def test_silver_model_requires_approval_and_works(project: Path, tmp_path: Path) -> None:
    check = "SELECT count(*) AS n, count(DISTINCT id_pedido) AS d FROM silver_pedidos"
    script = [
        call("query_run", sql=SILVER),  # recusado
        call("query_run", sql=SILVER),  # aprovado
        call("query_run", sql=check),
        text("silver_pedidos criada, sem duplicatas"),
    ]
    approvals = Approvals(False, True)
    rt, llm = await _runtime(project, tmp_path / "home", script, approvals)
    await rt.agent.run("crie um modelo silver de pedidos deduplicando e tipando")
    assert len(approvals.asked) == 2  # só as escritas pediram aprovação
    card = approvals.asked[0]
    assert card.preview_kind == "sql"
    assert card.details["sql_kind"] == "ddl"
    assert card.details["tables_written"] == ["silver_pedidos"]
    refused, created, counted = _results(llm)
    assert refused.is_error
    assert "recusou" in refused.content
    assert not created.is_error, created.content
    assert "linhas: 1" in counted.content
    n, d = re.findall(r"\| (\d+) \| (\d+) \|", counted.content)[0]
    assert n == d  # deduplicado
    audit = AuditLog(workspace_dir("default", tmp_path / "home") / "audit.db").list()
    assert {r.decision for r in audit} == {"refused", "executed"}


async def test_no_write_without_approval(project: Path, tmp_path: Path) -> None:
    script = [
        call("query_run", sql="CREATE TABLE x AS SELECT 1 AS a"),
        call("query_run", sql="SELECT * FROM x"),
        text("ok"),
    ]
    rt, llm = await _runtime(project, tmp_path / "home", script, Approvals())  # recusa tudo
    await rt.agent.run("crie x")
    _, select = _results(llm)
    assert select.is_error  # a tabela não existe: nada foi escrito
    assert "NotFoundError" in select.content


async def test_drop_in_prod_is_denied_without_asking(project: Path, tmp_path: Path) -> None:
    script = [call("query_run", sql="DROP TABLE IF EXISTS silver_pedidos"), text("bloqueado")]
    approvals = Approvals(True)
    rt, llm = await _runtime(project, tmp_path / "home", script, approvals, env="prod")
    await rt.agent.run("apague a tabela")
    assert approvals.asked == []
    [res] = _results(llm)
    assert res.is_error
    assert "bloqueado pela política" in res.content


async def test_exploratory_read_gets_limit_and_is_audited(project: Path, tmp_path: Path) -> None:
    script = [call("query_run", sql="SELECT * FROM read_csv('dados/itens_pedido.csv')"), text("ok")]
    rt, llm = await _runtime(project, tmp_path / "home", script, Approvals())
    await rt.agent.run("veja os itens")
    [res] = _results(llm)
    assert "LIMIT 1000 injetado" in res.content
    assert "resultado completo salvo em" in res.content
    [rec] = AuditLog(workspace_dir("default", tmp_path / "home") / "audit.db").list()
    assert rec.kind == "read"
    assert "LIMIT 1000" in rec.sql


async def test_catalog_tools(project: Path, tmp_path: Path) -> None:
    setup = "CREATE TABLE clientes AS SELECT * FROM read_csv('dados/clientes.csv')"
    script: list[FakeTurn] = [
        call("query_run", sql=setup),
        FakeTurn(
            tool_calls=[
                FakeToolCall("catalog_list_tables", {}),
                FakeToolCall("catalog_describe", {"table": "clientes"}),
                FakeToolCall("catalog_column_stats", {"table": "clientes"}),
            ]
        ),
        text("ok"),
    ]
    rt, llm = await _runtime(project, tmp_path / "home", script, Approvals(True))
    await rt.agent.run("descreva")
    _, listed, described, stats = _results(llm)
    assert "clientes" in listed.content
    assert "- email:" in described.content
    assert "PII" in described.content
    assert "email: nulos" in stats.content
    assert "PII: omitido" in stats.content
    sent: Any = llm.sent_text()
    assert not any(v in sent for v in _pii_values())
