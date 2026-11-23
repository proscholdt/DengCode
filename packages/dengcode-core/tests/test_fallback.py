from __future__ import annotations

from dengcode_core.errors import TransientError
from dengcode_core.llm.events import TextDelta
from dengcode_core.llm.fallback import FallbackLLM
from dengcode_core.llm.messages import ChatRequest, Message
from dengcode_testkit.fake_llm import FakeLLM, FakeTurn, text


async def test_switches_to_fallback_on_transient_error() -> None:
    primary = FakeLLM([FakeTurn(error=TransientError("esgotado"))])
    primary.name = "principal"
    backup = FakeLLM([text("oi do reserva"), text("de novo")])
    backup.name = "reserva"
    seen: list[tuple[str, str, str]] = []
    llm = FallbackLLM(
        primary, backup, {"m1": "m2"}, on_fallback=lambda a, b, r: seen.append((a, b, r))
    )

    req = ChatRequest(model="m1", messages=[Message.text("user", "oi")])
    events = [e async for e in llm.stream(req)]
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "oi do reserva"
    assert seen[0][:2] == ("principal", "reserva")
    assert backup.requests[0].model == "m2"
    # a troca vale para o resto da sessão
    _ = [e async for e in llm.stream(req)]
    assert len(primary.requests) == 1
    assert llm.name == "reserva"
