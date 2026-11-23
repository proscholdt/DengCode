from __future__ import annotations

import pytest

from dengcode_core.sql.guard import SqlKind, analyze, inject_limit, pretty

DIALECTS = ["duckdb", "postgres", "spark", "tsql", "trino", "bigquery"]

R, M, D, A, U = SqlKind.READ, SqlKind.DML, SqlKind.DDL, SqlKind.ADMIN, SqlKind.UNKNOWN


@pytest.mark.parametrize(
    ("dialect", "sql", "kind"),
    [
        *[(d, "SELECT id, nome FROM vendas.clientes WHERE id > 1", R) for d in DIALECTS],
        *[(d, "WITH c AS (SELECT * FROM a) SELECT * FROM c", R) for d in DIALECTS],
        *[(d, "INSERT INTO t (a) SELECT a FROM s", M) for d in DIALECTS],
        *[(d, "UPDATE t SET a = 1 WHERE id = 2", M) for d in DIALECTS],
        *[(d, "DELETE FROM t WHERE id = 2", M) for d in DIALECTS],
        *[(d, "CREATE TABLE t2 AS SELECT * FROM t", D) for d in DIALECTS],
        *[(d, "DROP TABLE t", D) for d in DIALECTS],
        ("duckdb", "SHOW TABLES", R),
        ("duckdb", "DESCRIBE t", R),
        ("postgres", "EXPLAIN SELECT * FROM t", R),
        ("postgres", "EXPLAIN ANALYZE DELETE FROM t", M),
        ("postgres", "EXPLAIN (ANALYZE, BUFFERS) UPDATE t SET a = 1", M),
        ("spark", "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", M),
        ("duckdb", "COPY t TO 'saida.csv'", M),
        ("duckdb", "ALTER TABLE t ADD COLUMN c INT", D),
        ("duckdb", "TRUNCATE t", D),
        ("tsql", "SELECT * INTO nova FROM t", D),
        ("postgres", "GRANT SELECT ON t TO analista", A),
        ("duckdb", "SET threads = 4", A),
        ("duckdb", "PRAGMA version", A),
        ("duckdb", "ATTACH 'outro.db'", A),
        ("spark", "OPTIMIZE t", A),
        ("postgres", "VACUUM t", A),
        ("postgres", "CALL atualiza()", A),
        ("postgres", "SELEC x", U),
        ("duckdb", "isso não é sql (", U),
        ("duckdb", "", U),
    ],
)
def test_classification(dialect: str, sql: str, kind: SqlKind) -> None:
    assert analyze(sql, dialect).kind is kind


def test_multi_statement_takes_highest_risk() -> None:
    a = analyze("SELECT 1; DELETE FROM t WHERE id = 1; DROP TABLE x", "duckdb")
    assert a.statements == 3
    assert a.kind is SqlKind.DDL
    assert a.dangerous == ["DROP TABLE"]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("DELETE FROM t", ["DELETE sem WHERE"]),
        ("UPDATE t SET a = 1", ["UPDATE sem WHERE"]),
        ("DROP TABLE t", ["DROP TABLE"]),
        ("DROP SCHEMA s", ["DROP SCHEMA"]),
        ("TRUNCATE t", ["TRUNCATE"]),
        ("ALTER TABLE t DROP COLUMN a", ["ALTER ... DROP"]),
        ("CREATE OR REPLACE TABLE t AS SELECT 1", ["CREATE OR REPLACE (sobrescreve)"]),
        ("DELETE FROM t WHERE id = 1", []),
        ("SELECT * FROM t", []),
    ],
)
def test_dangerous(sql: str, expected: list[str]) -> None:
    assert analyze(sql, "duckdb").dangerous == expected


def test_tables_read_and_written_ignore_ctes() -> None:
    a = analyze(
        "WITH a AS (SELECT * FROM bronze.pedidos) INSERT INTO silver.pedidos SELECT * "
        "FROM a JOIN bronze.clientes c ON true",
        "duckdb",
    )
    assert a.tables_written == ["silver.pedidos"]
    assert a.tables_read == ["bronze.clientes", "bronze.pedidos"]


def test_three_part_names_and_quoting() -> None:
    a = analyze('SELECT * FROM "Cat"."Sch"."Tab"', "postgres")
    assert a.tables_read == ["cat.sch.tab"]
    b = analyze("SELECT * FROM `proj.ds.tab`", "bigquery")
    assert b.tables_read == ["proj.ds.tab"]


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("duckdb", "SELECT a FROM t LIMIT 100"),
        ("postgres", "SELECT a FROM t LIMIT 100"),
        ("spark", "SELECT a FROM t LIMIT 100"),
        ("trino", "SELECT a FROM t LIMIT 100"),
        ("bigquery", "SELECT a FROM t LIMIT 100"),
        ("tsql", "SELECT TOP 100 a FROM t"),
    ],
)
def test_inject_limit_preserves_dialect(dialect: str, expected: str) -> None:
    assert inject_limit("SELECT a FROM t", 100, dialect) == expected


def test_inject_limit_skips_when_not_applicable() -> None:
    assert inject_limit("SELECT a FROM t LIMIT 5", 100) is None
    assert inject_limit("DELETE FROM t", 100) is None
    assert inject_limit("SELECT 1; SELECT 2", 100) is None
    assert inject_limit("SELECT TOP 3 a FROM t", 100, "tsql") is None
    assert analyze("SELECT a FROM t LIMIT 5").has_limit


def test_pretty_formats_or_falls_back() -> None:
    assert "\n" in pretty("select a, b from t where a > 1 and b < 2")
    assert pretty("VACUUM t", "postgres") == "VACUUM t"
