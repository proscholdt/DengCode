"""Testes de protocolo: o AcpServer dirigido pelo cliente de teste em memória + FakeLLM."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from dengcode_testkit.acp_client import AcpError
from dengcode_testkit.fake_llm import FakeToolCall, FakeTurn, call, text

Harness = Any  # fixture ``acp`` do conftest (servidor + cliente de teste)
MODEL = "select id,\n       nome\nfrom clientes\n"
NEW_MODEL = MODEL.replace("nome\n", "nome,\n       email\n")


def _edit() -> FakeTurn:
    return call(
        "file_edit", path="models/clientes.sql", old_text="nome\n", new_text="nome,\n       email\n"
    )


async def test_initialize_handshake_and_capabilities(acp: Harness) -> None:
    init = await acp.client.initialize()
    assert init["protocolVersion"] == 1
    caps = init["agentCapabilities"]
    assert caps["loadSession"] is True
    assert caps["promptCapabilities"] == {"image": False, "audio": False, "embeddedContext": False}
    assert caps["_meta"]["dengcode"] == {
        "version": 1,
        "extensions": ["sqlApproval", "dataPreview", "lineage", "stack", "workspace"],
    }
    assert init["agentInfo"]["name"] == "dengcode"
    assert init["authMethods"] == []


async def test_unknown_methods_and_notifications(acp: Harness) -> None:
    await acp.client.initialize()
    with pytest.raises(AcpError) as err:
        await acp.client.request("_outro/coisa", {})
    assert err.value.code == -32601
    with pytest.raises(AcpError) as err2:
        await acp.client.request("session/inexistente", {})
    assert err2.value.code == -32601
    await acp.client.notify("_outro/aviso", {"x": 1})  # ignorada, sem resposta
    await acp.client.send_raw("{isto não é json")
    with pytest.raises(AcpError) as err3:
        await acp.client.prompt("nao-existe", "oi")
    assert err3.value.code == -32002
    # a conexão continua de pé
    assert (await acp.client.initialize())["protocolVersion"] == 1


async def test_new_session_and_prompt_streams_text(acp: Harness) -> None:
    sid = await acp.session()
    acp.script(sid, [text("olá, sou o DengCode")])
    result = await acp.client.prompt(sid, "oi")
    assert result["stopReason"] == "end_turn"
    assert result["_meta"]["dengcode"]["reason"] == "end"
    chunks = acp.client.updates("agent_message_chunk")
    assert len(chunks) >= 2  # streaming de verdade, não um bloco só
    assert acp.client.agent_text() == "olá, sou o DengCode"
    [usage] = acp.client.updates("usage_update")
    assert usage["size"] > 0
    assert usage["cost"]["currency"] == "USD"
    assert all(p["sessionId"] == sid for p in acp.client.notifications("session/update"))


async def test_edit_with_diff_permission_allow_changes_file(acp: Harness) -> None:
    acp.client.permissions = ["allow_once"]
    sid = await acp.session()
    acp.script(sid, [_edit(), text("feito")])
    result = await acp.client.prompt(sid, "adicione email")
    assert result["stopReason"] == "end_turn"
    target = acp.project / "models" / "clientes.sql"
    assert target.read_text(encoding="utf-8") == NEW_MODEL

    [pending] = acp.client.updates("tool_call")
    assert pending["status"] == "pending"
    assert pending["kind"] == "edit"
    assert pending["title"] == "Editar models/clientes.sql"
    [diff] = [c for c in pending["content"] if c["type"] == "diff"]
    assert Path(diff["path"]) == target.resolve()
    assert diff["oldText"] == MODEL
    assert diff["newText"] == NEW_MODEL
    assert pending["locations"] == [{"path": str(target.resolve())}]

    [perm] = acp.client.permission_requests()
    assert perm["toolCall"]["toolCallId"] == pending["toolCallId"]
    assert [o["optionId"] for o in perm["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert [o["kind"] for o in perm["options"]] == ["allow_once", "allow_always", "reject_once"]
    assert perm["_meta"]["dengcode"]["name"] == "file_edit"

    updates = acp.client.updates("tool_call_update")
    assert [u["status"] for u in updates] == ["in_progress", "completed"]
    assert any(c["type"] == "diff" for c in updates[-1]["content"])
    order = acp.client.order()
    assert order.index("session/request_permission") > 0
    # o tool_call (pending) chega antes do pedido de permissão
    first_update = next(
        i
        for i, r in enumerate(acp.client.received)
        if r.method == "session/update" and r.params["update"]["sessionUpdate"] == "tool_call"
    )
    assert first_update < order.index("session/request_permission")


async def test_edit_rejected_keeps_file(acp: Harness) -> None:
    acp.client.permissions = ["reject_once"]
    sid = await acp.session()
    acp.script(sid, [_edit(), text("ok, não mudei")])
    await acp.client.prompt(sid, "adicione email")
    assert (acp.project / "models" / "clientes.sql").read_text(encoding="utf-8") == MODEL
    [final] = acp.client.updates("tool_call_update")
    assert final["status"] == "failed"
    assert all(c["type"] != "diff" for c in final["content"])
    assert "recusou" in final["content"][0]["content"]["text"]


async def test_allow_always_is_remembered_in_the_session(acp: Harness) -> None:
    acp.client.permissions = ["allow_always"]
    sid = await acp.session()
    second = call(
        "file_edit",
        path="models/clientes.sql",
        old_text="from clientes",
        new_text="from main.clientes",
    )
    acp.script(sid, [_edit(), second, text("ok")])
    await acp.client.prompt(sid, "mude")
    assert len(acp.client.permission_requests()) == 1
    assert "from main.clientes" in (acp.project / "models" / "clientes.sql").read_text(
        encoding="utf-8"
    )


async def test_policy_denial_is_a_failed_tool_call(acp: Harness, tmp_path: Path) -> None:
    sid = await acp.session()
    outside = tmp_path / "fora.txt"
    acp.script(sid, [call("file_write", path=str(outside), content="x"), text("ok")])
    await acp.client.prompt(sid, "escreva fora")
    assert not outside.exists()
    assert acp.client.permission_requests() == []
    [tc] = acp.client.updates("tool_call")
    assert tc["status"] == "failed"
    assert "bloqueado" in tc["content"][0]["content"]["text"]


async def test_sql_approval_comes_before_permission(acp: Harness) -> None:
    acp.client.permissions = ["allow_once"]
    sid = await acp.session()
    acp.script(sid, [call("query_run", sql="create table vendas as select 1 as id"), text("ok")])
    await acp.client.prompt(sid, "crie a tabela")
    order = acp.client.order()
    assert order.index("_dengcode/sqlApproval") < order.index("session/request_permission")
    [card] = acp.client.notifications("_dengcode/sqlApproval")
    [perm] = acp.client.permission_requests()
    assert card["toolCallId"] == perm["toolCall"]["toolCallId"]
    assert card["sessionId"] == sid
    assert card["engine"] == "duckdb"
    assert card["kind"] in ("ddl", "write", "create")
    assert any("vendas" in t for t in card["tablesWritten"])
    assert "CREATE TABLE" in card["sql"].upper()
    for key in ("statements", "tablesRead", "cost", "warnings", "plan"):
        assert key in card
    [tc] = acp.client.updates("tool_call")
    assert tc["kind"] == "execute"
    assert tc["title"].startswith("SQL: create table")


async def test_data_preview_after_query(acp: Harness) -> None:
    sid = await acp.session()
    sql = "select 1 as id, 'ana.souza@empresa.com' as email union all select 2, 'b@c.com'"
    acp.script(sid, [call("query_run", sql=sql), text("ok")])
    await acp.client.prompt(sid, "consulte")
    assert acp.client.permission_requests() == []  # leitura é livre
    [preview] = acp.client.notifications("_dengcode/dataPreview")
    [tc] = acp.client.updates("tool_call")
    assert preview["toolCallId"] == tc["toolCallId"]
    assert preview["sessionId"] == sid
    assert [c["name"] for c in preview["columns"]] == ["id", "email"]
    assert len(preview["rows"]) == 2
    assert preview["totalRows"] == 2
    assert preview["resultPath"].endswith(".parquet")
    raw = json.dumps(preview)
    assert "ana.souza@empresa.com" not in raw  # PII nunca crua
    # a notificação vem depois do fim da tool call
    order = [
        r.params["update"].get("status") if r.method == "session/update" else r.method
        for r in acp.client.received
    ]
    assert order.index("_dengcode/dataPreview") > order.index("completed")


async def test_cancel_mid_turn(project: Path, connector: Any) -> None:
    holder: dict[str, Any] = {}

    async def cancel_while_asking(params: dict[str, Any]) -> None:
        await holder["client"].cancel(params["sessionId"])
        await asyncio.sleep(0.05)

    async with connector(on_permission=cancel_while_asking) as acp:
        holder["client"] = acp.client
        sid = await acp.session()
        acp.script(sid, [_edit(), text("não deveria chegar aqui"), text("segundo turno")])
        result = await acp.client.prompt(sid, "edite")
        assert result["stopReason"] == "cancelled"
        assert (project / "models" / "clientes.sql").read_text(encoding="utf-8") == MODEL
        [closed] = acp.client.updates("tool_call_update")
        assert closed["status"] == "failed"
        # a sessão segue usável depois do cancelamento
        acp.llm(sid).script = [text("segundo turno")]
        again = await acp.client.prompt(sid, "e agora?")
        assert again["stopReason"] == "end_turn"


async def test_load_session_replays_history(project: Path, connector: Any) -> None:
    async with connector() as first:
        sid = await first.session()
        first.script(
            sid, [call("file_read", path="models/clientes.sql"), text("o modelo lê clientes")]
        )
        await first.client.prompt(sid, "o que faz o modelo?")

    async with connector() as second:
        init = await second.client.initialize()
        assert init["agentCapabilities"]["loadSession"]
        loaded = await second.client.request(
            "session/load", {"sessionId": sid, "cwd": str(project), "mcpServers": []}
        )
        assert loaded["_meta"]["dengcode"]["stack"] == "t"
        kinds = [u["sessionUpdate"] for u in second.client.updates()]
        assert kinds == [
            "user_message_chunk",
            "tool_call",
            "tool_call_update",
            "agent_message_chunk",
        ]
        user, tc, done, answer = second.client.updates()
        assert user["content"]["text"] == "o que faz o modelo?"
        assert tc["kind"] == "read"
        assert tc["title"] == "Ler models/clientes.sql"
        assert done["status"] == "completed"
        assert done["toolCallId"] == tc["toolCallId"]
        assert answer["content"]["text"] == "o modelo lê clientes"
        # e continua a conversa com o histórico carregado
        second.script(sid, [text("continuando")])
        assert (await second.client.prompt(sid, "e mais?"))["stopReason"] == "end_turn"
        llm = second.llm(sid)
        assert "o que faz o modelo?" in llm.sent_text()

        with pytest.raises(AcpError) as err:
            await second.client.request(
                "session/load", {"sessionId": "naoexiste", "cwd": str(project), "mcpServers": []}
            )
        assert err.value.code == -32002


async def test_stack_and_workspace_extensions(acp: Harness) -> None:
    await acp.client.initialize()
    listed = await acp.client.request("_dengcode/stack.list", {})
    by_name = {s["name"]: s for s in listed["stacks"]}
    assert by_name["t"]["origin"] == "projeto"
    assert by_name["t"]["description"] == "stack de teste"
    assert "opensource" in by_name
    assert listed["active"] == {"stack": "t", "env": "dev"}

    assert await acp.client.request("_dengcode/stack.use", {"stack": "t2", "env": "dev"}) == {
        "stack": "t2",
        "env": "dev",
    }
    with pytest.raises(AcpError):
        await acp.client.request("_dengcode/stack.use", {"stack": "nao-existe"})
    with pytest.raises(AcpError):
        await acp.client.request("_dengcode/stack.use", {"stack": "t", "env": "marte"})

    ws = await acp.client.request("_dengcode/workspace.list", {})
    assert ws["active"] == "default"
    assert "default" in ws["workspaces"]
    assert await acp.client.request("_dengcode/workspace.use", {"workspace": "analise"}) == {
        "workspace": "analise"
    }
    with pytest.raises(AcpError):
        await acp.client.request("_dengcode/workspace.use", {"workspace": "Nome Inválido"})
    ws2 = await acp.client.request("_dengcode/workspace.list", {})
    assert ws2["active"] == "analise"
    assert "analise" in ws2["workspaces"]

    # vale para sessões novas
    created = await acp.client.request("session/new", {"cwd": str(acp.project), "mcpServers": []})
    meta = created["_meta"]["dengcode"]
    assert meta["stack"] == "t2"
    assert meta["workspace"] == "analise"


async def test_lineage_extension(project: Path, connector: Any) -> None:
    async with connector(stack="tl") as acp:
        sid = await acp.session()
        graph = await acp.client.request(
            "_dengcode/lineage",
            {"sessionId": sid, "target": "silver_pedidos", "direction": "down", "depth": 2},
        )
        assert graph["root"] == "main.silver_pedidos"
        pairs = {(e["source"], e["target"]) for e in graph["edges"]}
        assert ("main.silver_pedidos", "main.gold") in pairs
        assert all(set(e) == {"source", "target", "via"} for e in graph["edges"])
        kinds = {n["id"]: n["kind"] for n in graph["nodes"]}
        assert kinds["main.silver_pedidos"] == "table"
        cols = await acp.client.request(
            "_dengcode/lineage",
            {"sessionId": sid, "target": "silver_pedidos.canal", "direction": "up", "depth": 3},
        )
        assert cols["root"] == "main.silver_pedidos.canal"
        assert any(e["source"] == "main.bronze_pedidos.canal" for e in cols["edges"])
        assert all(n["kind"] == "column" for n in cols["nodes"])
        missing = await acp.client.request(
            "_dengcode/lineage",
            {"sessionId": sid, "target": "nada", "direction": "down", "depth": 1},
        )
        assert missing == {"root": "nada", "nodes": [], "edges": []}
        with pytest.raises(AcpError):
            await acp.client.request(
                "_dengcode/lineage", {"sessionId": sid, "target": "x", "direction": "lado"}
            )


async def test_lineage_without_transform_is_empty(acp: Harness) -> None:
    sid = await acp.session()
    graph = await acp.client.request(
        "_dengcode/lineage", {"sessionId": sid, "target": "x", "direction": "down", "depth": 2}
    )
    assert graph == {"root": "x", "nodes": [], "edges": []}


async def test_numbered_plan_becomes_plan_update(acp: Harness) -> None:
    sid = await acp.session()
    first = FakeTurn(
        text="Plano:\n1. Ler o modelo\n2. Explicar as colunas\n",
        tool_calls=[FakeToolCall("file_read", {"path": "models/clientes.sql"})],
    )
    acp.script(sid, [first, text("pronto")])
    await acp.client.prompt(sid, "explique")
    plans = acp.client.updates("plan")
    assert [p["entries"][0]["status"] for p in plans] == ["pending", "completed"]
    assert [e["content"] for e in plans[0]["entries"]] == ["Ler o modelo", "Explicar as colunas"]


async def test_llm_failure_is_a_jsonrpc_error(acp: Harness) -> None:
    sid = await acp.session()
    acp.script(sid, [])  # roteiro vazio: o FakeLLM falha
    with pytest.raises(AcpError) as err:
        await acp.client.prompt(sid, "oi")
    assert err.value.code == -32603
    assert err.value.data == {"reason": "error"}
    with pytest.raises(AcpError) as empty:
        await acp.client.prompt(sid, "   ")
    assert empty.value.code == -32602
