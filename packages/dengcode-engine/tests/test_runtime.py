from __future__ import annotations

from pathlib import Path

import pytest

from dengcode_core.errors import ConfigError, TransientError
from dengcode_core.events import ProviderFallback
from dengcode_core.llm.fallback import FallbackLLM
from dengcode_core.policy.permission import PermissionMode
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_engine.runtime import RuntimeOptions, build_runtime, parse_llm_override
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, FakeTurn


def _registry() -> AdapterRegistry:
    return AdapterRegistry([RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter)])


def _stack(project: Path, body: str) -> None:
    folder = project / ".dengcode" / "stacks"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "t.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


async def _rt(project: Path, home: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    opts = RuntimeOptions(project_dir=project, stack="t", **kwargs)  # type: ignore[arg-type]
    return await build_runtime(opts, registry=_registry(), home=home)


async def test_parse_llm_override() -> None:
    assert parse_llm_override("anthropic") == ("anthropic", None)
    assert parse_llm_override("ollama:qwen2.5:7b") == ("ollama", "qwen2.5:7b")


async def test_runtime_runs_a_turn_and_persists(project: Path, tmp_path: Path) -> None:
    _stack(project, "name: t\nllm: { provider: fake, script: [pronto] }\n")
    rt = await _rt(project, tmp_path / "home")
    assert rt.agent.models.main == "fake-main"  # padrão do adapter
    result = await rt.agent.run("oi")
    assert result.text == "pronto"
    assert rt.store.load(rt.agent.session.id).meta.system_prompt is not None


async def test_resume_keeps_frozen_system_prompt(project: Path, tmp_path: Path) -> None:
    _stack(project, "name: t\nllm: { provider: fake }\n")
    home = tmp_path / "home"
    rt = await _rt(project, home)
    rt.agent.session.meta.system_prompt = "prompt antigo"
    rt.store.save(rt.agent.session)
    again = await _rt(project, home, session_id=rt.agent.session.id)
    assert again.agent.system_prompt == "prompt antigo"


async def test_placeholder_model_is_config_error(project: Path, tmp_path: Path) -> None:
    _stack(project, "name: t\nllm: { provider: fake, model: '<modelo>' }\n")
    with pytest.raises(ConfigError, match="não configurado"):
        await _rt(project, tmp_path / "home")


async def test_llm_override_and_missing_adapter(project: Path, tmp_path: Path) -> None:
    _stack(project, "name: t\nllm: { provider: fake, model: a }\n")
    assert (await _rt(project, tmp_path / "home", llm="fake:b")).agent.models.main == "b"
    with pytest.raises(ConfigError, match="não está instalado"):
        await _rt(project, tmp_path / "home", llm="inexistente")


async def test_read_only_env_and_prod_auto(project: Path, tmp_path: Path) -> None:
    body = (
        "name: t\n"
        "llm: { provider: fake }\n"
        "environments:\n"
        "  dev: { permission_mode: auto }\n"
        "  prod: { permission_mode: auto }\n"
        "  audit: { read_only: true }\n"
    )
    _stack(project, body)
    home = tmp_path / "home"
    assert (await _rt(project, home, env="dev")).agent.policy.mode is PermissionMode.AUTO
    assert (await _rt(project, home, env="prod")).agent.policy.mode is PermissionMode.ASK
    assert (await _rt(project, home, env="audit")).agent.policy.mode is PermissionMode.READ_ONLY
    with pytest.raises(ConfigError, match="ambiente"):
        await _rt(project, home, env="inexistente")


async def test_fallback_is_wired_and_emits_event(project: Path, tmp_path: Path) -> None:
    body = (
        "name: t\n"
        "llm:\n"
        "  provider: fake\n"
        "  name: principal\n"
        "  model: m1\n"
        "  fallback: { provider: fake, name: reserva, model: m2, script: [do reserva] }\n"
    )
    _stack(project, body)
    rt = await _rt(project, tmp_path / "home")
    llm = rt.agent.llm
    assert isinstance(llm, FallbackLLM)
    primary = llm._primary
    assert isinstance(primary, FakeLLM)
    primary.script = [FakeTurn(error=TransientError("fora do ar"))]
    events: list[object] = []
    rt.bus.subscribe(events.append)
    result = await rt.agent.run("oi")
    assert result.text == "do reserva"
    assert any(isinstance(e, ProviderFallback) for e in events)
