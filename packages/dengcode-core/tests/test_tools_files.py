from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dengcode_core.checkpoints import CheckpointStore, latest_session_store
from dengcode_core.tools.base import ToolContext
from dengcode_core.tools.files import (
    FileEdit,
    FileEditArgs,
    FileList,
    FileListArgs,
    FileRead,
    FileReadArgs,
    FileSearch,
    FileSearchArgs,
    FileWrite,
    FileWriteArgs,
)
from dengcode_core.tools.shell import ShellRun, ShellRunArgs

PY = f'"{sys.executable}"'


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(
        "def soma(a, b):\n    return a - b\n", encoding="utf-8"
    )
    return ToolContext(tmp_path, "s1", checkpoints=CheckpointStore.for_session(tmp_path, "s1"))


async def test_read_numbers_lines(ctx: ToolContext) -> None:
    r = await FileRead().run(FileReadArgs(path="src/calc.py"), ctx)
    assert "     2\t    return a - b" in r.content


async def test_read_missing_is_error(ctx: ToolContext) -> None:
    r = await FileRead().run(FileReadArgs(path="nada.py"), ctx)
    assert r.is_error


async def test_list_and_search(ctx: ToolContext) -> None:
    r = await FileList().run(FileListArgs(recursive=True), ctx)
    assert "src/calc.py" in r.content
    s = await FileSearch().run(FileSearchArgs(pattern=r"return a"), ctx)
    assert "src/calc.py:2:" in s.content


async def test_search_python_fallback(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    s = await FileSearch().run(FileSearchArgs(pattern=r"def soma"), ctx)
    assert "src/calc.py:1:" in s.content


async def test_edit_preview_apply_and_undo(ctx: ToolContext) -> None:
    tool = FileEdit()
    args = FileEditArgs(path="src/calc.py", old_text="a - b", new_text="a + b")
    preview = await tool.preview(args, ctx)
    assert preview is not None
    assert preview.kind == "diff"
    assert "-    return a - b" in preview.text
    assert "+    return a + b" in preview.text
    target = ctx.workspace_root / "src" / "calc.py"
    assert "a - b" in target.read_text(encoding="utf-8")  # o preview não escreve
    r = await tool.run(args, ctx)
    assert not r.is_error
    assert "a + b" in target.read_text(encoding="utf-8")
    store = latest_session_store(ctx.workspace_root)
    assert store is not None
    assert store.undo() is not None
    assert "a - b" in target.read_text(encoding="utf-8")


async def test_edit_ambiguous_is_error(ctx: ToolContext) -> None:
    (ctx.workspace_root / "d.txt").write_text("x x", encoding="utf-8")
    r = await FileEdit().run(FileEditArgs(path="d.txt", old_text="x", new_text="y"), ctx)
    assert r.is_error
    assert "2 vezes" in r.content


async def test_write_new_file_and_undo_removes_it(ctx: ToolContext) -> None:
    r = await FileWrite().run(FileWriteArgs(path="novo/arq.txt", content="olá\n"), ctx)
    assert not r.is_error
    target = ctx.workspace_root / "novo" / "arq.txt"
    assert target.read_text(encoding="utf-8") == "olá\n"
    assert ctx.checkpoints is not None
    ctx.checkpoints.undo()
    assert not target.exists()


async def test_write_assess_flags_outside_path(ctx: ToolContext) -> None:
    req = await FileWrite().assess(FileWriteArgs(path="../fora.txt", content=""), ctx)
    assert not ctx.inside_workspace(req.paths_written[0])


async def test_shell_runs_in_workspace(ctx: ToolContext) -> None:
    cmd = f'{PY} -c "import os; print(os.listdir())"'
    r = await ShellRun().run(ShellRunArgs(command=cmd), ctx)
    assert not r.is_error
    assert "src" in r.content
    assert r.metadata["exit_code"] == 0


async def test_shell_nonzero_exit_and_timeout(ctx: ToolContext) -> None:
    r = await ShellRun().run(ShellRunArgs(command=f'{PY} -c "exit(3)"'), ctx)
    assert r.is_error
    assert r.metadata["exit_code"] == 3
    slow = f'{PY} -c "import time; time.sleep(10)"'
    t = await ShellRun().run(ShellRunArgs(command=slow, timeout_s=1), ctx)
    assert t.is_error
    assert "tempo esgotado" in t.content
