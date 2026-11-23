"""Data diff com DuckDB real: linhas a mais, duplicadas, nulos novos."""

from __future__ import annotations

import pytest

from dengcode_adapter_duckdb.engine import DuckDBQueryEngine
from dengcode_core.errors import ConfigError
from dengcode_core.verify.diff import DataDiff

BASE = """CREATE TABLE antes AS SELECT * FROM (VALUES
  (1, 'site', 10.0, 'a@x.com'), (2, 'app', 20.0, 'b@x.com'), (3, 'loja', 30.0, 'c@x.com')
) t(id, canal, valor, email)"""


@pytest.fixture
async def engine() -> DuckDBQueryEngine:
    eng = DuckDBQueryEngine()
    await eng.execute(BASE)
    return eng


async def test_identical_relations_have_no_warnings(engine: DuckDBQueryEngine) -> None:
    await engine.execute("CREATE TABLE depois AS SELECT * FROM antes")
    report = await DataDiff(engine).compare("antes", "depois", ["id"])
    assert report.rows_before == report.rows_after == 3
    assert report.changed_rows == 0
    assert report.warnings == []
    assert "alertas: nenhum" in report.summary()


async def test_extra_rows_and_new_keys(engine: DuckDBQueryEngine) -> None:
    await engine.execute(
        "CREATE TABLE depois AS SELECT * FROM antes UNION ALL SELECT 4, 'site', 40.0, 'd@x.com'"
    )
    report = await DataDiff(engine).compare("antes", "depois", ["id"])
    assert (report.rows_before, report.rows_after) == (3, 4)
    assert report.only_after == 1
    assert report.sample_only_after == ["4"]
    assert any("contagem de linhas mudou" in w for w in report.warnings)


async def test_duplicates(engine: DuckDBQueryEngine) -> None:
    await engine.execute(
        "CREATE TABLE depois AS SELECT * FROM antes UNION ALL SELECT * FROM antes WHERE id = 2"
    )
    report = await DataDiff(engine).compare("antes", "depois", ["id"])
    assert report.duplicate_keys_before == 0
    assert report.duplicate_keys_after == 1
    assert any("chaves duplicadas novas" in w for w in report.warnings)


async def test_new_nulls_changed_rows_and_columns(engine: DuckDBQueryEngine) -> None:
    await engine.execute(
        "CREATE TABLE depois AS SELECT id, CASE WHEN id = 3 THEN NULL ELSE canal END AS canal, "
        "valor * 2 AS valor, email, 'novo' AS canal_venda FROM antes"
    )
    report = await DataDiff(engine).compare("antes", "depois", ["id"])
    canal = next(c for c in report.columns if c.name == "canal")
    assert (canal.nulls_before, canal.nulls_after) == (0, 1)
    assert any("nulos novos em canal" in w for w in report.warnings)
    assert report.changed_rows == 3
    assert report.added_columns == ["canal_venda"]
    valor = next(c for c in report.columns if c.name == "valor")
    assert valor.avg_before == pytest.approx(20.0)
    assert valor.avg_after == pytest.approx(40.0)
    summary = report.summary()
    assert "a@x.com" not in summary  # PII: sem min/max nem top valores


async def test_missing_key_is_config_error(engine: DuckDBQueryEngine) -> None:
    await engine.execute("CREATE TABLE depois AS SELECT canal FROM antes")
    with pytest.raises(ConfigError, match="chave ausente"):
        await DataDiff(engine).compare("antes", "depois", ["id"])


async def test_transpiles_to_engine_dialect() -> None:
    class Spy(DuckDBQueryEngine):
        dialect_seen: list[str] = []  # noqa: RUF012

    eng = Spy()
    await eng.execute(BASE)
    await eng.execute("CREATE TABLE depois AS SELECT * FROM antes")
    diff = DataDiff(eng)
    assert diff.dialect == "duckdb"
    report = await diff.compare("main.antes", "main.depois", ["id", "canal"])
    assert report.key == ["id", "canal"]
