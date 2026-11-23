from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from dengcode_core.agent.loop import Agent, ApprovalAnswer, Budget, ModelRoles
from dengcode_core.checkpoints import CheckpointStore
from dengcode_core.context import UNTRUSTED_OPEN
from dengcode_core.errors import AdapterError, ConfigError
from dengcode_core.events import ApprovalRequested, EventBus, SessionEnded, ToolCallFinished
from dengcode_core.llm.messages import ToolResultPart
from dengcode_core.policy.permission import PermissionPolicy
from dengcode_core.session import Session, SessionMeta, SessionStore
from dengcode_core.tools.base import ToolContext
from dengcode_core.tools.builtin import builtin_tools
from dengcode_core.tools.registry import ToolRegistry
from dengcode_testkit.fake_llm import FakeLLM, FakeToolCall, FakeTurn, call, text


class Approvals:
    def __init__(self, *answers: ApprovalAnswer) -> None:
        self.answers = list(answers)
        self.asked: list[ApprovalRequested] = []

    async def __call__(self, req: ApprovalRequested) -> ApprovalAnswer:
        self.asked.append(req)
        return self.answers.pop(0) if self.answers else ApprovalAnswer(False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text("def soma(a, b):\n    return a - b\n", encoding="utf-8")
    return tmp_path


def make_agent(
    project: Path,
    llm: FakeLLM,
    approver: Approvals | None = None,
    *,
    budget: Budget | None = None,
    events: list[Any] | None = None,
) -> Agent:
    bus = EventBus()
    if events is not None:
        bus.subscribe(events.append)
    session = Session(SessionMeta(workspace="teste", project_dir=str(project)))
    return Agent(
        llm=llm,
        models=ModelRoles("principal", "rapido"),
        tools=ToolRegistry(builtin_tools()),
        policy=PermissionPolicy(),
        tool_ctx=ToolContext(
            project, session.id, checkpoints=CheckpointStore.for_session(project, session.id)
        ),
        session=session,
        system_prompt="sistema de teste",
        approver=approver or Approvals(),
        bus=bus,
        budget=budget,
        store=SessionStore(project / ".sessions"),
    )


def last_results(llm: FakeLLM) -> list[ToolResultPart]:
    return llm.requests[-1].messages[-1].tool_results()


async def test_plain_text_turn_ends() -> None:
    llm = FakeLLM([text("Olá!")])
    agent = make_agent(Path.cwd(), llm)
    result = await agent.run("oi")
    assert result.reason == "end"
    assert result.text == "Olá!"
    assert llm.requests[0].messages[0].role == "system"
    assert {t.name for t in llm.requests[0].tools} >= {"file_read", "shell_run"}


async def test_read_tool_result_goes_back_wrapped_as_untrusted(project: Path) -> None:
    llm = FakeLLM([call("file_read", path="calc.py"), text("achei o bug")])
    result = await make_agent(project, llm).run("leia")
    assert result.reason == "end"
    [res] = last_results(llm)
    assert "return a - b" in res.content
    assert res.content.startswith(UNTRUSTED_OPEN)


async def test_edit_approved_applies_and_undo_restores(project: Path) -> None:
    llm = FakeLLM(
        [call("file_edit", path="calc.py", old_text="a - b", new_text="a + b"), text("corrigido")]
    )
    approvals = Approvals(ApprovalAnswer(True))
    events: list[Any] = []
    agent = make_agent(project, llm, approvals, events=events)
    await agent.run("corrija")
    assert "a + b" in (project / "calc.py").read_text(encoding="utf-8")
    [asked] = approvals.asked
    assert asked.preview_kind == "diff"
    assert asked.preview is not None
    assert "+    return a + b" in asked.preview
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert "diff" in finished[0].metadata
    assert agent.ctx.checkpoints is not None
    agent.ctx.checkpoints.undo()
    assert "a - b" in (project / "calc.py").read_text(encoding="utf-8")


async def test_edit_refused_keeps_file_and_tells_model(project: Path) -> None:
    llm = FakeLLM(
        [
            call("file_edit", path="calc.py", old_text="a - b", new_text="a + b"),
            text("ok, não alterei"),
        ]
    )
    await make_agent(project, llm, Approvals(ApprovalAnswer(False))).run("corrija")
    assert "a - b" in (project / "calc.py").read_text(encoding="utf-8")
    [res] = last_results(llm)
    assert res.is_error
    assert "recusou" in res.content


async def test_remember_grants_rest_of_session(project: Path) -> None:
    llm = FakeLLM(
        [
            call("file_write", path="a.txt", content="1"),
            call("file_write", path="b.txt", content="2"),
            text("pronto"),
        ]
    )
    approvals = Approvals(ApprovalAnswer(True, remember=True))
    await make_agent(project, llm, approvals).run("crie")
    assert len(approvals.asked) == 1
    assert (project / "b.txt").exists()


async def test_write_outside_workspace_is_denied_without_asking(project: Path) -> None:
    llm = FakeLLM([call("file_write", path="../fora.txt", content="x"), text("não deu")])
    approvals = Approvals(ApprovalAnswer(True))
    await make_agent(project, llm, approvals).run("escreva fora")
    assert approvals.asked == []
    assert not (project.parent / "fora.txt").exists()
    [res] = last_results(llm)
    assert "bloqueado pela política" in res.content


async def test_unknown_tool_and_invalid_args_become_errors(project: Path) -> None:
    turn = FakeTurn(
        tool_calls=[FakeToolCall("nao_existe", {}), FakeToolCall("file_read", {"caminho": "x"})]
    )
    llm = FakeLLM([turn, text("ok")])
    await make_agent(project, llm).run("x")
    r1, r2 = last_results(llm)
    assert r1.is_error
    assert "desconhecida" in r1.content
    assert r2.is_error
    assert "argumentos inválidos" in r2.content


async def test_parallel_reads_keep_order(project: Path) -> None:
    (project / "b.py").write_text("B = 1\n", encoding="utf-8")
    turn = FakeTurn(
        tool_calls=[
            FakeToolCall("file_read", {"path": "calc.py"}),
            FakeToolCall("file_read", {"path": "b.py"}),
        ]
    )
    llm = FakeLLM([turn, text("li os dois")])
    await make_agent(project, llm).run("leia")
    r1, r2 = last_results(llm)
    assert "soma" in r1.content
    assert "B = 1" in r2.content


async def test_max_steps_budget(project: Path) -> None:
    llm = FakeLLM([call("file_list") for _ in range(5)])
    result = await make_agent(project, llm, budget=Budget(max_steps=2)).run("liste")
    assert result.reason == "max_steps"
    assert result.steps == 2


async def test_cost_budget_and_usage(project: Path) -> None:
    llm = FakeLLM(
        [
            FakeTurn(
                tool_calls=[FakeToolCall("file_list", {})], input_tokens=1_000_000, output_tokens=0
            )
        ]
        * 3
    )
    agent = make_agent(project, llm, budget=Budget(max_usd=5.0))
    result = await agent.run("x")
    assert result.reason == "budget"
    assert agent.session.meta.cost_usd == pytest.approx(6.0)
    assert agent.session.meta.input_tokens == 2_000_000


async def test_llm_error_ends_turn_with_error(project: Path) -> None:
    events: list[Any] = []
    llm = FakeLLM([FakeTurn(error=AdapterError("caiu"))])
    result = await make_agent(project, llm, events=events).run("x")
    assert result.reason == "error"
    assert isinstance(events[-1], SessionEnded)


async def test_cancel_closes_dangling_tool_calls(project: Path) -> None:
    started = asyncio.Event()

    class Slow(Approvals):
        async def __call__(self, req: ApprovalRequested) -> ApprovalAnswer:
            started.set()
            await asyncio.sleep(30)
            return ApprovalAnswer(True)

    llm = FakeLLM([call("file_write", path="a.txt", content="1")])
    agent = make_agent(project, llm, Slow())
    task = asyncio.create_task(agent.run("crie"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    last = agent.session.messages[-1]
    assert last.role == "tool"
    assert "cancelado" in last.tool_results()[0].content
    assert not (project / "a.txt").exists()


async def test_model_without_tools_is_refused(project: Path) -> None:
    agent = make_agent(project, FakeLLM([], supports_tools=False))
    with pytest.raises(ConfigError, match="não suporta ferramentas"):
        await agent.run("x")


async def test_session_is_persisted(project: Path) -> None:
    agent = make_agent(project, FakeLLM([text("oi")]))
    await agent.run("olá")
    loaded = SessionStore(project / ".sessions").load(agent.session.id)
    assert [m.role for m in loaded.messages] == ["user", "assistant"]
