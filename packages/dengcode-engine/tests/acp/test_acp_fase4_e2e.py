"""Fluxo da fase 4 inteiro pelo ACP (dbt e DuckDB reais, FakeLLM roteirizado).

"Adicione a coluna canal_venda em silver_pedidos e propague até gold_vendas_diarias":
o cliente ACP recebe os diffs, aprova cada escrita por ``session/request_permission``
e o turno termina com run/test verdes e data diff. Reusa o roteiro de
``tests/test_fase4_e2e.py``.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.integration

FASE4 = Path(__file__).resolve().parents[1] / "test_fase4_e2e.py"


def _fase4() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dengcode_fase4_cenario", FASE4)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ecommerce(tmp_path: Path) -> Path:
    f4 = _fase4()
    proj = tmp_path / "ecommerce"
    shutil.copytree(
        f4.EXAMPLE,
        proj,
        ignore=shutil.ignore_patterns(
            "target", "logs", "*.duckdb", "*.duckdb.wal", ".user.yml", "gerar_dados.py"
        ),
    )
    (proj / ".dengcode" / "stacks" / "t.yaml").write_text(
        "name: t\nextends: ecommerce\nllm: { provider: fake }\n", encoding="utf-8"
    )
    return proj.resolve()


async def test_phase4_flow_over_acp(ecommerce: Path, connector: Any) -> None:
    f4 = _fase4()
    async with connector(default_permission="allow_once") as acp:
        acp.project = ecommerce
        sid = await acp.session()
        rt = acp.server.sessions[sid].runtime
        assert rt is not None
        assert rt.agent.ctx.workspace_root == ecommerce
        # estado inicial: o projeto materializado como está hoje
        transform = rt.adapters["transform"]
        base = await transform.run("+gold_vendas_diarias", target="dev")
        assert base.success, base.logs

        acp.script(sid, f4.phase4_script(f"dengcode_tmp_{sid}"))
        result = await acp.client.prompt(
            sid,
            "adicione a coluna canal_venda em silver_pedidos e propague até gold_vendas_diarias",
        )
        assert result["stopReason"] == "end_turn", result

        # só as escritas de verdade pediram permissão (snapshots no schema temporário, não)
        asked = [p["_meta"]["dengcode"]["name"] for p in acp.client.permission_requests()]
        assert asked == ["file_edit"] * 5 + ["transform_run"]

        # toda edição chegou ao cliente como diff ACP do arquivo inteiro
        edits = [u for u in acp.client.updates("tool_call") if u["kind"] == "edit"]
        assert len(edits) == 5
        diffs = [c for u in edits for c in u["content"] if c["type"] == "diff"]
        assert len(diffs) == 5
        silver = ecommerce / f4.SILVER
        assert diffs[0]["path"] == str(silver)
        assert "canal_venda" not in diffs[0]["oldText"]
        assert "canal_venda" in diffs[0]["newText"]
        assert "canal_venda" in silver.read_text(encoding="utf-8")

        finals = [u for u in acp.client.updates("tool_call_update") if u.get("content")]
        failed = [u for u in finals if u["status"] == "failed"]
        assert not failed, failed
        texts = [
            c["content"]["text"] for u in finals for c in u["content"] if c["type"] == "content"
        ]
        assert any("SUCESSO" in t for t in texts)  # run e test verdes
        assert any("colunas novas: canal_venda" in t for t in texts)  # data diff
        assert "canal_venda adicionado" in acp.client.agent_text()
