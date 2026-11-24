"""Fixtures dos testes de protocolo ACP: servidor + cliente de teste em memória."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dengcode_core.ports.models import AdapterContext, AdapterInfo, LineageEdge, PortKind
from dengcode_core.ports.transform import ModelInfo, TransformRunResult
from dengcode_engine.acp.server import AcpDefaults, AcpServer
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter
from dengcode_testkit.acp_client import AcpTestClient, memory_transport_pair
from dengcode_testkit.fake_llm import FakeLLM, FakeLLMAdapter, FakeTurn

SILVER = "select cast(id as int) as id_pedido, canal from main.bronze_pedidos"
GOLD = "select canal, count(*) as n from main.silver_pedidos group by 1"
MODEL = "select id,\n       nome\nfrom clientes\n"


class FakeTransform:
    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="fake_transform", port=PortKind.TRANSFORM, version="0"
    )
    name = "fake_transform"

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> FakeTransform:
        return cls()

    async def list_models(self, selector: str | None = None) -> list[ModelInfo]:
        return [
            ModelInfo(
                name="silver_pedidos",
                unique_id="m.s",
                relation="main.silver_pedidos",
                depends_on=["bronze_pedidos"],
            ),
            ModelInfo(
                name="gold", unique_id="m.g", relation="main.gold", depends_on=["silver_pedidos"]
            ),
        ]

    async def compile(self, model: str) -> str:
        return {"silver_pedidos": SILVER, "gold": GOLD}[model]

    async def run(self, selector: str, *, target: str) -> TransformRunResult:
        return TransformRunResult(success=True, command="run", selector=selector, target=target)

    async def test(self, selector: str, *, target: str) -> TransformRunResult:
        return TransformRunResult(success=True, command="test", selector=selector, target=target)

    async def manifest_lineage(self) -> list[LineageEdge]:
        return [LineageEdge(source="main.silver_pedidos", target="main.gold", via="gold")]


STACKS = {
    "t": "name: t\ndescription: stack de teste\nllm: { provider: fake }\n"
    "compute: [ { adapter: duckdb } ]\n",
    "t2": "name: t2\nllm: { provider: fake }\n",
    "tl": "name: tl\nllm: { provider: fake }\ncompute: [ { adapter: duckdb } ]\n"
    "transform: { adapter: fake_transform }\n",
}


def write_project(proj: Path) -> Path:
    stacks = proj / ".dengcode" / "stacks"
    stacks.mkdir(parents=True)
    for name, text in STACKS.items():
        (stacks / f"{name}.yaml").write_text(text, encoding="utf-8")
    (proj / "models").mkdir()
    # newline="\n": no Windows o write_text trocaria \n por \r\n e o old_text não casaria
    (proj / "models" / "clientes.sql").write_text(MODEL, encoding="utf-8", newline="\n")
    return proj


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return write_project(tmp_path / "proj")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture(scope="session")
def registry() -> AdapterRegistry:
    return AdapterRegistry(
        [
            *AdapterRegistry.discover().all(),
            RegisteredAdapter("fake", None, FakeLLMAdapter.info, FakeLLMAdapter),
            RegisteredAdapter("fake_transform", None, FakeTransform.info, FakeTransform),
        ]
    )


@dataclass
class Harness:
    server: AcpServer
    client: AcpTestClient
    project: Path

    def llm(self, session_id: str) -> FakeLLM:
        rt = self.server.sessions[session_id].runtime
        assert rt is not None
        llm = rt.agent.llm
        assert isinstance(llm, FakeLLM)
        return llm

    def script(self, session_id: str, turns: list[FakeTurn]) -> None:
        self.llm(session_id).script = list(turns)

    async def session(self) -> str:
        await self.client.initialize()
        return await self.client.new_session(self.project)


@contextlib.asynccontextmanager
async def connect(
    project: Path, home: Path, registry: AdapterRegistry, stack: str = "t", **client: Any
) -> AsyncIterator[Harness]:
    server_end, client_end = memory_transport_pair()
    server = AcpServer(
        defaults=AcpDefaults(stack=stack), registry=registry, home=home, project_dir=project
    )
    serving = asyncio.create_task(server.serve(server_end))
    acp = AcpTestClient(client_end, **client)
    acp.start()
    try:
        yield Harness(server, acp, project)
    finally:
        server_end.close()  # EOF para o cliente
        client_end.close()  # EOF para o servidor
        await acp.close()
        async with asyncio.timeout(10):
            await serving


Connect = Callable[..., contextlib.AbstractAsyncContextManager[Harness]]


@pytest.fixture
def connector(project: Path, home: Path, registry: AdapterRegistry) -> Connect:
    """``async with connector(stack="tl", permissions=[...]) as h`` — servidor + cliente novos."""

    def make(stack: str = "t", **client: Any) -> contextlib.AbstractAsyncContextManager[Harness]:
        return connect(project, home, registry, stack, **client)

    return make


@pytest.fixture
async def acp(connector: Connect) -> AsyncIterator[Harness]:
    async with connector() as h:
        yield h
