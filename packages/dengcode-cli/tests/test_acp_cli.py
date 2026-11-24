"""``dengcode acp`` e ``chat --acp``: o servidor real em subprocesso e a CLI como cliente."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from dengcode_cli.acp_client import AcpChatClient, server_command
from dengcode_core.agent.loop import ApprovalAnswer
from dengcode_core.events import (
    ApprovalRequested,
    AssistantText,
    SessionEnded,
    ToolCallFinished,
    ToolCallStarted,
)
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_engine.runtime import RuntimeOptions, build_runtime
from dengcode_testkit.acp_client import AcpTestClient
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, FakeToolCall, FakeTurn

FAKE_SERVER = (
    Path(__file__).resolve().parents[2] / "dengcode-engine" / "tests" / "acp" / "fake_server.py"
)
MODEL = "select id,\n       nome\nfrom clientes\n"
TURNS: list[dict[str, Any]] = [
    {
        "text": "Vou qualificar a tabela.",
        "tool_calls": [
            {
                "name": "file_edit",
                "arguments": {
                    "path": "models/clientes.sql",
                    "old_text": "from clientes",
                    "new_text": "from main.clientes",
                },
            }
        ],
    },
    {"text": "pronto"},
]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".dengcode" / "stacks").mkdir(parents=True)
    (proj / ".dengcode" / "stacks" / "t.yaml").write_text(
        "name: t\ndescription: teste\nllm: { provider: fake }\n", encoding="utf-8"
    )
    (proj / "models").mkdir()
    (proj / "models" / "clientes.sql").write_text(MODEL, encoding="utf-8", newline="\n")
    return proj


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("DENGCODE_HOME", str(path))
    return path


def _turns() -> list[FakeTurn]:
    return [
        FakeTurn(
            text=t.get("text", ""),
            tool_calls=[FakeToolCall(c["name"], c["arguments"]) for c in t.get("tool_calls", [])],
        )
        for t in TURNS
    ]


def _shape(events: list[BaseModel]) -> list[tuple[str, ...]]:
    """O que o terminal mostra, sem ids: texto junto, ferramentas e fim do turno."""
    out: list[tuple[str, ...]] = []
    for ev in events:
        if isinstance(ev, AssistantText):
            if out and out[-1][0] == "text":
                out[-1] = ("text", out[-1][1] + ev.delta)
            else:
                out.append(("text", ev.delta))
        elif isinstance(ev, ToolCallStarted):
            out.append(("start", ev.name, json.dumps(ev.arguments, sort_keys=True)))
        elif isinstance(ev, ToolCallFinished):
            out.append(("finish", ev.name, str(ev.is_error), ev.content.splitlines()[0]))
        elif isinstance(ev, SessionEnded):
            out.append(("end", ev.reason))
    return out


async def test_dengcode_acp_command_speaks_protocol(project: Path, home: Path) -> None:
    client = await AcpTestClient.spawn(
        [sys.executable, "-m", "dengcode_cli.main", "acp", "--project", str(project)],
        cwd=project,
        env={"DENGCODE_HOME": str(home), "DENGCODE_STACK": "t"},
    )
    try:
        init = await client.initialize()
        assert init["agentCapabilities"]["_meta"]["dengcode"]["version"] == 1
        stacks = await client.request("_dengcode/stack.list", {})
        assert stacks["active"] == {"stack": "t", "env": "dev"}  # DENGCODE_STACK
        assert any(s["name"] == "t" and s["origin"] == "projeto" for s in stacks["stacks"])
        ws = await client.request("_dengcode/workspace.list", {})
        assert ws["active"] == "default"
    finally:
        await client.close()
    assert client.process is not None
    assert client.process.returncode == 0


def test_server_command_uses_same_interpreter(tmp_path: Path) -> None:
    cmd = server_command(stack="s", env="prod", project=tmp_path, mode="ask", workspace="w")
    assert cmd[:4] == [sys.executable, "-m", "dengcode_cli.main", "acp"]
    assert cmd[cmd.index("--stack") + 1] == "s"
    assert cmd[cmd.index("--workspace") + 1] == "w"
    assert "--llm" not in cmd


async def test_chat_acp_parity_with_in_process(project: Path, home: Path, tmp_path: Path) -> None:
    asked: list[ApprovalRequested] = []

    async def approve(req: ApprovalRequested) -> ApprovalAnswer:
        asked.append(req)
        return ApprovalAnswer(approved=True)

    # 1) modo local (in-process)
    local: list[BaseModel] = []
    fake = RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter)
    rt = await build_runtime(
        RuntimeOptions(project_dir=project, stack="t"),
        registry=AdapterRegistry([*AdapterRegistry.discover().all(), fake]),
        approver=approve,
        home=home,
    )
    assert isinstance(rt.agent.llm, FakeLLM)
    rt.agent.llm.script = _turns()
    rt.bus.subscribe(local.append)
    await rt.agent.run("qualifique")
    target = project / "models" / "clientes.sql"
    assert "from main.clientes" in target.read_text(encoding="utf-8")
    target.write_text(MODEL, encoding="utf-8", newline="\n")

    # 2) a mesma conversa pelo ACP (subprocesso)
    script = tmp_path / "roteiro.json"
    script.write_text(json.dumps(TURNS), encoding="utf-8")
    remote: list[BaseModel] = []
    client = AcpChatClient(remote.append, approve)
    await client.start([sys.executable, str(FAKE_SERVER), str(script), "t"], cwd=project)
    try:
        meta = await client.open(project)
        assert meta["stack"] == "t"
        assert await client.prompt("qualifique") == "end"
    finally:
        await client.close()
    assert "from main.clientes" in target.read_text(encoding="utf-8")

    assert _shape(remote) == _shape(local)
    local_ask, remote_ask = asked
    assert remote_ask.name == local_ask.name == "file_edit"
    assert remote_ask.risk == local_ask.risk
    assert remote_ask.preview_kind == local_ask.preview_kind == "diff"
    assert remote_ask.preview == local_ask.preview
    assert remote_ask.reason == local_ask.reason


async def test_chat_acp_resume_and_refusal(project: Path, home: Path, tmp_path: Path) -> None:
    script = tmp_path / "roteiro.json"
    script.write_text(json.dumps(TURNS), encoding="utf-8")
    events: list[BaseModel] = []

    async def refuse(req: ApprovalRequested) -> ApprovalAnswer:
        return ApprovalAnswer(approved=False)

    cmd = [sys.executable, str(FAKE_SERVER), str(script), "t"]
    first = AcpChatClient(events.append, refuse)
    await first.start(cmd, cwd=project)
    try:
        await first.open(project)
        assert await first.prompt("qualifique") == "end"
    finally:
        await first.close()
    assert (project / "models" / "clientes.sql").read_text(encoding="utf-8") == MODEL
    failed = [e for e in events if isinstance(e, ToolCallFinished) and e.is_error]
    assert len(failed) == 1
    assert "recusou" in failed[0].content

    replayed: list[BaseModel] = []
    second = AcpChatClient(replayed.append, refuse)
    await second.start(cmd, cwd=project)
    try:
        meta = await second.open(project, resume=first.session_id)
        assert meta["stack"] == "t"
        assert second.session_id == first.session_id
        assert replayed == []  # o histórico não é redesenhado (igual ao modo local)
    finally:
        await second.close()


def test_chat_acp_command_end_to_end(
    project: Path, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import dengcode_cli.acp_client as acp_client
    from dengcode_cli.main import app

    script = tmp_path / "roteiro.json"
    script.write_text(json.dumps(TURNS), encoding="utf-8")
    monkeypatch.setattr(
        acp_client,
        "server_command",
        lambda **_: [sys.executable, str(FAKE_SERVER), str(script), "t"],
    )
    result = CliRunner().invoke(
        app,
        ["chat", "--acp", "--stack", "t", "--project", str(project)],
        input="qualifique\ns\n/cost\n/sair\n",
    )
    assert result.exit_code == 0, result.output
    assert "DengCode" in result.output
    assert "(ACP)" in result.output
    assert "Aprovação: file_edit" in result.output
    assert "pronto" in result.output
    assert "tokens:" in result.output
    assert "from main.clientes" in (project / "models" / "clientes.sql").read_text(encoding="utf-8")
