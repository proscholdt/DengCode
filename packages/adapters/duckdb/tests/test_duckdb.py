from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dengcode_adapter_duckdb.catalog import DuckDBCatalog
from dengcode_adapter_duckdb.engine import DuckDBQueryEngine
from dengcode_core.errors import NotFoundError
from dengcode_core.ports.models import AdapterContext, TableRef
from dengcode_testkit.contracts.data import CatalogContract, QueryEngineContract


@pytest.fixture
def engine() -> DuckDBQueryEngine:
    return DuckDBQueryEngine(duckdb.connect(":memory:"))


class TestDuckDBQueryEngine(QueryEngineContract):
    @pytest.fixture
    def slow_sql(self) -> str:
        return "SELECT count(*) FROM range(10000000000) a, range(1000) b"


class TestDuckDBCatalog(CatalogContract):
    @pytest.fixture
    async def seeded_table(self, engine: DuckDBQueryEngine) -> TableRef:
        await engine.execute("CREATE SCHEMA vendas")
        await engine.execute("CREATE TABLE vendas.clientes (id INTEGER, email VARCHAR)")
        await engine.execute(
            "INSERT INTO vendas.clientes VALUES (1, 'a@x.com'), (2, NULL), (3, 'c@x.com')"
        )
        return TableRef(schema="vendas", name="clientes")

    @pytest.fixture
    def seeded_schema(self) -> str:
        return "vendas"

    @pytest.fixture
    def catalog(self, engine: DuckDBQueryEngine) -> DuckDBCatalog:
        return DuckDBCatalog(engine.conn)


def test_catalog_shares_connection_with_peer_engine(engine: DuckDBQueryEngine) -> None:
    ctx = AdapterContext(workspace="t", peers={"query_engine": engine})
    assert DuckDBCatalog.from_config({}, ctx)._conn is engine.conn


async def test_file_database_relative_to_project(tmp_path: Path) -> None:
    ctx = AdapterContext(workspace="t", project_dir=tmp_path)
    eng = DuckDBQueryEngine.from_config({"database": "dados/local.duckdb"}, ctx)
    await eng.execute("CREATE TABLE t AS SELECT 1 AS x")
    assert (tmp_path / "dados" / "local.duckdb").exists()


async def test_reads_csv_files_directly(tmp_path: Path, engine: DuckDBQueryEngine) -> None:
    csv = tmp_path / "pedidos.csv"
    csv.write_text("id,valor\n1,10.5\n2,20\n", encoding="utf-8")
    result = await engine.execute(f"SELECT sum(valor) AS total FROM '{csv.as_posix()}'")
    assert result.preview.column("total").to_pylist() == [30.5]


async def test_missing_table_is_not_found(engine: DuckDBQueryEngine) -> None:
    with pytest.raises(NotFoundError):
        await engine.execute("SELECT * FROM nao_existe")
