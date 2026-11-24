from __future__ import annotations

from pathlib import Path

from dengcode_core.llm.messages import Message, TextPart, ToolCallPart, ToolResultPart
from dengcode_engine.acp import mapping


def test_tool_kinds_cover_our_tools_and_risks() -> None:
    assert mapping.tool_kind("file_read") == "read"
    assert mapping.tool_kind("file_edit") == "edit"
    assert mapping.tool_kind("file_search") == "search"
    assert mapping.tool_kind("shell_run") == "execute"
    assert mapping.tool_kind("query_run") == "execute"
    assert mapping.tool_kind("ferramenta_nova", "read") == "read"
    assert mapping.tool_kind("ferramenta_nova", "write_local") == "edit"
    assert mapping.tool_kind("ferramenta_nova") == "other"


def test_titles_are_short_and_readable() -> None:
    assert mapping.tool_title("file_edit", {"path": "a.sql"}) == "Editar a.sql"
    long_sql = "select " + ", ".join(f"c{i}" for i in range(100)) + " from t"
    title = mapping.tool_title("query_run", {"sql": long_sql})
    assert title.startswith("SQL: select c0")
    assert len(title) <= 85
    assert mapping.tool_title("transform_list_models", {}) == "transform_list_models"


def test_edit_diff_full_file(tmp_path: Path) -> None:
    (tmp_path / "m.sql").write_text("a\nb\nb\n", encoding="utf-8", newline="\n")
    one = mapping.edit_diff(
        "file_edit", {"path": "m.sql", "old_text": "a", "new_text": "x"}, tmp_path
    )
    assert one is not None
    assert one["oldText"] == "a\nb\nb\n"
    assert one["newText"] == "x\nb\nb\n"
    every = {"path": "m.sql", "old_text": "b", "new_text": "y", "replace_all": True}
    assert mapping.edit_diff("file_edit", every, tmp_path)["newText"] == "a\ny\ny\n"  # type: ignore[index]
    missing = {"path": "m.sql", "old_text": "zzz", "new_text": "y"}
    assert mapping.edit_diff("file_edit", missing, tmp_path) is None
    new = mapping.edit_diff("file_write", {"path": "novo.sql", "content": "select 1"}, tmp_path)
    assert new is not None
    assert new["oldText"] is None
    assert Path(new["path"]).is_absolute()
    assert mapping.edit_diff("file_read", {"path": "m.sql"}, tmp_path) is None


def test_prompt_text_joins_blocks() -> None:
    blocks = [
        {"type": "text", "text": "explique"},
        {"type": "resource_link", "uri": "file:///x/m.sql", "name": "m.sql"},
        {"type": "resource", "resource": {"uri": "file:///x/n.sql", "text": "select 1"}},
        {"type": "image", "data": "...", "mimeType": "image/png"},
    ]
    text = mapping.prompt_text(blocks)
    assert text.startswith("explique")
    assert "[m.sql](file:///x/m.sql)" in text
    assert "select 1" in text


def test_parse_plan_needs_sequential_numbered_items() -> None:
    assert mapping.parse_plan("Vou:\n1. ler\n2) editar\n3. testar") == ["ler", "editar", "testar"]
    assert mapping.parse_plan("1. só um item") == []
    assert mapping.parse_plan("texto solto 2024. não é plano\n5. nem isto") == []


def test_replay_updates_follow_history(tmp_path: Path) -> None:
    msgs = [
        Message.text("system", "x"),
        Message.text("user", "oi"),
        Message(
            role="assistant",
            parts=[
                TextPart(text="vou ler"),
                ToolCallPart(id="c1", name="file_read", arguments={"path": "a"}),
            ],
        ),
        Message(role="tool", parts=[ToolResultPart(call_id="c1", content="erro", is_error=True)]),
    ]
    kinds = [u["sessionUpdate"] for u in mapping.replay_updates(msgs, tmp_path)]
    assert kinds == ["user_message_chunk", "agent_message_chunk", "tool_call", "tool_call_update"]
    assert mapping.replay_updates(msgs, tmp_path)[-1]["status"] == "failed"
