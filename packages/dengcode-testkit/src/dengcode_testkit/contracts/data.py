"""Contratos de StoragePort, QueryEnginePort e CatalogPort.

Cada adapter herda a classe numa ``Test...`` e fornece as fixtures descritas
no docstring de cada contrato. Escrita só contra DuckDB local ou containers.
"""

from __future__ import annotations

import asyncio

import pytest

from dengcode_core.errors import AdapterError, DengCodeError, NotFoundError, UnsupportedOperation
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.models import TableRef
from dengcode_core.ports.query_engine import QueryEnginePort
from dengcode_core.ports.storage import StoragePort

SAMPLE_CSV = b"id,nome,email\n1,Ana,ana@exemplo.com\n2,Bruno,bruno@exemplo.com\n3,Carla,\n"
"""Conteúdo que o adapter de storage deve publicar em ``sample_uri``."""


class StorageContract:
    """Fixtures: ``storage``; ``base_uri`` (pasta/prefixo); ``sample_uri`` (arquivo
    ``.csv`` dentro de ``base_uri`` com ``SAMPLE_CSV``); ``missing_uri``."""

    async def test_list_contains_sample(
        self, storage: StoragePort, base_uri: str, sample_uri: str
    ) -> None:
        items = await storage.list(base_uri)
        assert any(o.uri.rstrip("/").endswith(sample_uri.rsplit("/", 1)[-1]) for o in items)

    async def test_read_bytes_and_max(self, storage: StoragePort, sample_uri: str) -> None:
        assert (await storage.read_bytes(sample_uri)).startswith(b"id,nome")
        assert await storage.read_bytes(sample_uri, max_bytes=2) == b"id"

    async def test_sample_and_schema(self, storage: StoragePort, sample_uri: str) -> None:
        table = await storage.read_sample(sample_uri, rows=2)
        assert table.num_rows == 2
        assert table.column_names == ["id", "nome", "email"]
        schema = await storage.infer_schema(sample_uri)
        assert schema.names == ["id", "nome", "email"]

    async def test_missing_is_not_found(self, storage: StoragePort, missing_uri: str) -> None:
        with pytest.raises(NotFoundError):
            await storage.read_bytes(missing_uri)

    async def test_write_respects_capabilities(self, storage: StoragePort, base_uri: str) -> None:
        if storage.capabilities().supports_write:
            return
        with pytest.raises(UnsupportedOperation):
            await storage.write_bytes(base_uri.rstrip("/") + "/novo.txt", b"x")


CONTRACT_TABLE = "dc_contrato"


class QueryEngineContract:
    """Fixtures: ``engine`` (gravável, local/container); ``slow_sql`` (consulta que
    demora > 3 s no dialeto do engine, ou ``None`` para pular o teste de timeout)."""

    @pytest.fixture
    async def seeded(self, engine: QueryEnginePort) -> QueryEnginePort:
        await engine.execute(f"DROP TABLE IF EXISTS {CONTRACT_TABLE}")
        await engine.execute(f"CREATE TABLE {CONTRACT_TABLE} (id INTEGER, nome VARCHAR(50))")
        await engine.execute(
            f"INSERT INTO {CONTRACT_TABLE} VALUES (1, 'Ana'), (2, 'Bruno'), (3, 'Carla')"
        )
        return engine

    async def test_execute_select_returns_preview(self, seeded: QueryEnginePort) -> None:
        result = await seeded.execute(f"SELECT id, nome FROM {CONTRACT_TABLE} ORDER BY id")
        assert [c.name for c in result.columns] == ["id", "nome"]
        assert result.preview.num_rows == 3
        assert result.preview.column("nome").to_pylist() == ["Ana", "Bruno", "Carla"]
        assert result.elapsed_ms >= 0

    async def test_max_rows_is_respected(self, seeded: QueryEnginePort) -> None:
        result = await seeded.execute(f"SELECT * FROM {CONTRACT_TABLE}", max_rows=2)
        assert result.preview.num_rows == 2

    async def test_capabilities_are_consistent(self, seeded: QueryEnginePort) -> None:
        caps = seeded.capabilities()
        sql = f"SELECT * FROM {CONTRACT_TABLE}"
        if caps.supports_dry_run:
            await seeded.dry_run(sql)
        else:
            with pytest.raises(UnsupportedOperation):
                await seeded.dry_run(sql)
        if caps.supports_explain:
            assert (await seeded.explain(sql)).strip()
        else:
            with pytest.raises(UnsupportedOperation):
                await seeded.explain(sql)

    async def test_errors_are_domain_errors(self, engine: QueryEnginePort) -> None:
        with pytest.raises(AdapterError):
            await engine.execute("SELEC isso nao e sql")
        with pytest.raises(DengCodeError):
            await engine.execute("SELECT * FROM tabela_que_nao_existe_xyz")

    async def test_timeout_is_respected(
        self, engine: QueryEnginePort, slow_sql: str | None
    ) -> None:
        if slow_sql is None:
            pytest.skip("adapter sem consulta lenta para o teste")
        started = asyncio.get_running_loop().time()
        with pytest.raises(DengCodeError):
            await engine.execute(slow_sql, timeout_s=1)
        assert asyncio.get_running_loop().time() - started < 10


class CatalogContract:
    """Fixtures: ``catalog``; ``seeded_table`` (``TableRef`` de uma tabela com colunas
    ``id`` inteiro e ``email`` texto, 3 linhas, 1 e-mail nulo); ``seeded_schema``."""

    async def test_lists_schema_and_table(
        self, catalog: CatalogPort, seeded_table: TableRef, seeded_schema: str
    ) -> None:
        assert seeded_schema in await catalog.list_schemas(seeded_table.catalog)
        tables = await catalog.list_tables(seeded_schema, catalog=seeded_table.catalog)
        assert seeded_table.name in [t.name for t in tables]

    async def test_describe(self, catalog: CatalogPort, seeded_table: TableRef) -> None:
        info = await catalog.describe(seeded_table)
        assert [c.name for c in info.columns] == ["id", "email"]
        assert info.row_count in (3, None)
        again = await catalog.describe(seeded_table)
        assert info.schema_hash == again.schema_hash

    async def test_column_stats(self, catalog: CatalogPort, seeded_table: TableRef) -> None:
        if not catalog.capabilities().supports_column_stats:
            pytest.skip("catálogo sem estatísticas de coluna")
        stats = {s.column: s for s in await catalog.column_stats(seeded_table)}
        assert stats["email"].null_fraction == pytest.approx(1 / 3, abs=0.01)

    async def test_missing_table(self, catalog: CatalogPort, seeded_schema: str) -> None:
        with pytest.raises(NotFoundError):
            await catalog.describe(TableRef(schema=seeded_schema, name="nao_existe_xyz"))

    async def test_lineage_respects_capabilities(
        self, catalog: CatalogPort, seeded_table: TableRef
    ) -> None:
        if catalog.capabilities().supports_lineage:
            await catalog.lineage(seeded_table, direction="down")
        else:
            with pytest.raises(UnsupportedOperation):
                await catalog.lineage(seeded_table, direction="down")
