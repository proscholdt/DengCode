"""Servidor ACP de teste (subprocesso): motor real + LLM falso roteirizado.

Uso (só em testes): ``python fake_server.py <roteiro.json> <stack>``. O roteiro é
uma lista de turnos ``{"text": "...", "tool_calls": [{"name": ..., "arguments": {...}}]}``.
Nenhum gancho de teste existe no código de produção: este arquivo monta o
registro de adapters com o FakeLLM e chama o mesmo ``AcpServer`` do ``dengcode acp``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, ClassVar

from dengcode_core.ports.models import AdapterInfo, PortKind
from dengcode_engine.acp.server import AcpDefaults, AcpServer
from dengcode_engine.acp.stdio import process_stdio
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_testkit.fake_llm import FakeLLM, FakeToolCall, FakeTurn


def load_script(path: Path) -> list[FakeTurn]:
    raw: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return [
        FakeTurn(
            text=t.get("text", ""),
            tool_calls=[
                FakeToolCall(c["name"], c.get("arguments", {})) for c in t.get("tool_calls", [])
            ],
        )
        for t in raw
    ]


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    script = load_script(Path(sys.argv[1]))
    stack = sys.argv[2] if len(sys.argv) > 2 else "t"

    class ScriptedLLM:
        info: ClassVar[AdapterInfo] = AdapterInfo(name="fake", port=PortKind.LLM, version="0")
        default_models: ClassVar[dict[str, str]] = {"main": "fake-main", "fast": "fake-fast"}

        @classmethod
        def from_config(cls, cfg: dict[str, Any], ctx: Any) -> FakeLLM:
            return FakeLLM(list(script))

    registry = AdapterRegistry(
        [
            *AdapterRegistry.discover().all(),
            RegisteredAdapter("fake", None, ScriptedLLM.info, ScriptedLLM),
        ]
    )
    server = AcpServer(defaults=AcpDefaults(stack=stack), registry=registry)
    transport = process_stdio()
    # depois de proteger o stdout, um print perdido vai para o stderr (não quebra o protocolo)
    print("ruído no stdout que não pode chegar ao cliente")
    asyncio.run(server.serve(transport))


if __name__ == "__main__":
    main()
