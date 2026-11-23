"""Contrato da TransformPort.

O adapter herda ``TransformContract`` numa ``Test...`` e fornece as fixtures:

- ``transform``: o adapter, apontando para um projeto de transformação local
  (cópia descartável: os testes executam ``run``/``test``);
- ``known_model``: nome de um modelo do projeto cujo ``run`` e ``test`` passam;
- ``upstream_model``: nome de um modelo do qual ``known_model`` depende direto;
- ``target``: alvo usado em ``run``/``test`` (padrão ``"dev"``).
"""

from __future__ import annotations

import asyncio

import pytest

from dengcode_core.errors import NotFoundError
from dengcode_core.ports.transform import TransformPort


class TransformContract:
    @pytest.fixture
    def target(self) -> str:
        return "dev"

    async def test_is_transform_port(self, transform: TransformPort) -> None:
        assert isinstance(transform, TransformPort)
        assert transform.name

    async def test_list_contains_known_model(
        self, transform: TransformPort, known_model: str, upstream_model: str
    ) -> None:
        models = {m.name: m for m in await transform.list_models()}
        assert known_model in models
        info = models[known_model]
        assert info.unique_id
        assert info.relation
        assert upstream_model in info.depends_on

    async def test_compile_returns_sql_and_is_cached(
        self, transform: TransformPort, known_model: str
    ) -> None:
        sql = await transform.compile(known_model)
        assert "select" in sql.lower()
        loop = asyncio.get_running_loop()
        started = loop.time()
        again = await transform.compile(known_model)
        assert again == sql
        # do cache: sem recompilar o projeto
        assert loop.time() - started < 1.0

    async def test_unknown_model_compile_is_not_found(self, transform: TransformPort) -> None:
        with pytest.raises(NotFoundError):
            await transform.compile("modelo_que_nao_existe_xyz")

    async def test_run_and_test_known_model(
        self, transform: TransformPort, known_model: str, upstream_model: str, target: str
    ) -> None:
        # o upstream precisa existir no banco antes do modelo
        upstream = await transform.run(upstream_model, target=target)
        assert upstream.success, upstream.logs
        result = await transform.run(known_model, target=target)
        assert result.success, result.logs
        assert result.command == "run"
        assert result.selector == known_model
        assert any(r.name == known_model and r.status == "success" for r in result.results)
        tested = await transform.test(known_model, target=target)
        assert tested.success, tested.logs
        assert tested.command == "test"
        assert tested.results
        assert all(r.status == "pass" for r in tested.results)

    async def test_manifest_lineage_has_upstream_edge(
        self, transform: TransformPort, known_model: str, upstream_model: str
    ) -> None:
        models = {m.name: m for m in await transform.list_models()}
        source = models[upstream_model].relation
        target = models[known_model].relation
        edges = await transform.manifest_lineage()
        assert any(e.source == source and e.target == target for e in edges)
