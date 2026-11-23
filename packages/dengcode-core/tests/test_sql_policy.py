from __future__ import annotations

from typing import Any

import pytest

from dengcode_core.policy.permission import Decision, PermissionMode, PermissionPolicy
from dengcode_core.sql.guard import analyze
from dengcode_core.tools.base import ActionRequest, Risk


def _req(sql: str, **extra: Any) -> ActionRequest:
    a = analyze(sql, "duckdb")
    details = {
        "sql_kind": a.kind.value,
        "dangerous": a.dangerous,
        "tables_written": a.tables_written,
        **extra,
    }
    return ActionRequest("query_run", Risk.SQL, details=details)


def decide(policy: PermissionPolicy, sql: str, **extra: Any) -> Decision:
    return policy.decide(_req(sql, **extra)).decision


@pytest.mark.parametrize(
    ("sql", "dev", "prod"),
    [
        ("SELECT * FROM t", Decision.ALLOW, Decision.ALLOW),
        ("INSERT INTO t VALUES (1)", Decision.ASK, Decision.DENY),
        ("UPDATE t SET a = 1 WHERE id = 1", Decision.ASK, Decision.DENY),
        ("CREATE TABLE silver.x AS SELECT 1", Decision.ASK, Decision.DENY),
        ("DROP TABLE silver.x", Decision.ASK, Decision.DENY),
        ("GRANT SELECT ON t TO u", Decision.ASK, Decision.DENY),
        ("isso não é sql", Decision.ASK, Decision.DENY),
        ("SELECT 1; DROP TABLE x", Decision.ASK, Decision.DENY),
    ],
)
def test_default_table(sql: str, dev: Decision, prod: Decision) -> None:
    assert decide(PermissionPolicy(env="dev"), sql) is dev
    assert decide(PermissionPolicy(env="prod"), sql) is prod


def test_drop_in_prod_is_denied_even_with_prod_writes() -> None:
    policy = PermissionPolicy(env="prod", allow_prod_writes=True)
    assert decide(policy, "DROP TABLE gold.fato") is Decision.DENY
    assert decide(policy, "INSERT INTO t VALUES (1)") is Decision.ASK


def test_dangerous_is_never_auto_or_remembered() -> None:
    policy = PermissionPolicy(mode=PermissionMode.AUTO)
    req = _req("DELETE FROM t")
    verdict = policy.decide(req)
    assert verdict.decision is Decision.ASK
    assert "DELETE sem WHERE" in verdict.warnings
    policy.grant(req)
    assert policy.decide(req).decision is Decision.ASK


def test_auto_mode_allows_plain_dml_in_dev_only() -> None:
    assert decide(PermissionPolicy(mode=PermissionMode.AUTO), "INSERT INTO t VALUES (1)") is (
        Decision.ALLOW
    )
    assert decide(PermissionPolicy(mode=PermissionMode.AUTO), "CREATE TABLE t (a INT)") is (
        Decision.ASK
    )


def test_read_only_denies_all_writes() -> None:
    policy = PermissionPolicy(mode=PermissionMode.READ_ONLY)
    assert decide(policy, "SELECT 1") is Decision.ALLOW
    assert decide(policy, "INSERT INTO t VALUES (1)") is Decision.DENY


def test_protected_tables_deny_any_write() -> None:
    policy = PermissionPolicy(protected_tables=("gold.fato_vendas",))
    assert decide(policy, "INSERT INTO gold.fato_vendas VALUES (1)") is Decision.DENY
    assert decide(policy, "INSERT INTO lake.gold.fato_vendas VALUES (1)") is Decision.DENY
    assert decide(policy, "SELECT * FROM gold.fato_vendas") is Decision.ALLOW


def test_cost_reason_turns_read_into_ask() -> None:
    policy = PermissionPolicy()
    assert decide(policy, "SELECT * FROM t", cost_reason="acima do teto") is Decision.ASK


def test_session_grant_by_sql_kind() -> None:
    policy = PermissionPolicy()
    insert = _req("INSERT INTO t VALUES (1)")
    policy.grant(insert)
    assert policy.decide(_req("INSERT INTO t VALUES (2)")).decision is Decision.ALLOW
    assert policy.decide(_req("CREATE TABLE z (a INT)")).decision is Decision.ASK
