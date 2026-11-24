"""Fumaça com subprocesso real: JSON-RPC por stdin/stdout (thread de leitura no Windows)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dengcode_testkit.acp_client import AcpTestClient

FAKE_SERVER = Path(__file__).with_name("fake_server.py")


async def test_subprocess_roundtrip_with_edit(project: Path, home: Path, tmp_path: Path) -> None:
    script = tmp_path / "roteiro.json"
    turns = [
        {
            "tool_calls": [
                {
                    "name": "file_edit",
                    "arguments": {
                        "path": "models/clientes.sql",
                        "old_text": "from clientes",
                        "new_text": "from main.clientes",
                    },
                }
            ]
        },
        {"text": "pronto: tabela qualificada"},
    ]
    script.write_text(json.dumps(turns), encoding="utf-8")
    client = await AcpTestClient.spawn(
        [sys.executable, str(FAKE_SERVER), str(script), "t"],
        cwd=project,
        env={"DENGCODE_HOME": str(home), "PYTHONIOENCODING": "utf-8"},
        permissions=["allow_once"],
    )
    try:
        init = await client.initialize()
        assert init["protocolVersion"] == 1
        sid = await client.new_session(project)
        result = await client.prompt(sid, "qualifique a tabela")
        assert result["stopReason"] == "end_turn"
        assert client.agent_text() == "pronto: tabela qualificada"
        assert len(client.permission_requests()) == 1
        text = (project / "models" / "clientes.sql").read_text(encoding="utf-8")
        assert "from main.clientes" in text
        stacks = await client.request("_dengcode/stack.list", {})
        assert any(s["name"] == "t" for s in stacks["stacks"])
    finally:
        await client.close()
    # stdin fechado → o servidor encerra sozinho, sem erro
    assert client.process is not None
    assert client.process.returncode == 0
