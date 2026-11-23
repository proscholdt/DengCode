"""SqlGuard: análise e classificação de SQL com sqlglot (sqlguard.md).

Regras:
- várias instruções numa string: vale a de maior risco;
- parse falhou ou instrução não mapeada: ``UNKNOWN`` (tratado como DDL);
- ordem de risco: READ < DML < ADMIN < DDL = UNKNOWN.
"""

from __future__ import annotations

import re
from enum import StrEnum

import sqlglot
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

DEFAULT_DIALECT = "duckdb"


class SqlKind(StrEnum):
    READ = "read"
    DML = "dml"
    DDL = "ddl"
    ADMIN = "admin"
    UNKNOWN = "unknown"


RISK: dict[SqlKind, int] = {
    SqlKind.READ: 0,
    SqlKind.DML: 1,
    SqlKind.ADMIN: 2,
    SqlKind.DDL: 3,
    SqlKind.UNKNOWN: 3,
}


class SqlAnalysis(BaseModel):
    kind: SqlKind
    statements: int
    tables_read: list[str]
    tables_written: list[str]
    dangerous: list[str]
    has_limit: bool
    dialect: str
    error: str | None = None

    @property
    def is_read(self) -> bool:
        return self.kind is SqlKind.READ


_READ_TYPES: tuple[type[exp.Expr], ...] = (
    exp.Select,
    exp.SetOperation,
    exp.Show,
    exp.Describe,
    exp.Subquery,
    exp.Values,
)
_DML_TYPES: tuple[type[exp.Expr], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Copy,
)
_DDL_TYPES: tuple[type[exp.Expr], ...] = (exp.Create, exp.Alter, exp.Drop, exp.TruncateTable)
_ADMIN_TYPES: tuple[type[exp.Expr], ...] = (
    exp.Grant,
    exp.Revoke,
    exp.Set,
    exp.Use,
    exp.Pragma,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Attach,
    exp.Detach,
    exp.Install,
)
_EXPLAIN_ANALYZE = re.compile(
    r"^(\([^)]*\bANALY[SZ]E\b[^)]*\)|ANALY[SZ]E\b(\s+VERBOSE\b)?)", re.IGNORECASE
)
_READ_COMMANDS = {"SHOW", "DESCRIBE", "DESC", "LIST"}
_ADMIN_COMMANDS = {
    "CALL",
    "OPTIMIZE",
    "VACUUM",
    "ANALYZE",
    "REFRESH",
    "MSCK",
    "GRANT",
    "REVOKE",
    "SET",
    "USE",
    "LOAD",
    "INSTALL",
    "CHECKPOINT",
    "EXEC",
    "EXECUTE",
    "KILL",
    "RESET",
    "DENY",
}


def table_name(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return ".".join(parts).lower()


def _tables(node: exp.Expr) -> list[exp.Table]:
    return [t for t in node.find_all(exp.Table) if t.name]


def _cte_names(node: exp.Expr) -> set[str]:
    return {c.alias_or_name.lower() for c in node.find_all(exp.CTE)}


def _written(node: exp.Expr) -> list[exp.Table]:
    if isinstance(node, exp.Select):
        into = node.args.get("into")
        return [t for t in into.find_all(exp.Table)] if isinstance(into, exp.Expr) else []
    if isinstance(node, exp.TruncateTable):
        return [t for t in node.expressions if isinstance(t, exp.Table)]
    if isinstance(node, exp.Drop):
        targets: list[exp.Expr] = list(node.args.get("tables") or []) or [node.this]
        return [t for t in targets if isinstance(t, exp.Table)]
    if isinstance(node, (*_DML_TYPES, *_DDL_TYPES)):
        target = node.this
        if isinstance(target, exp.Table):
            return [target]
        if isinstance(target, exp.Expr):  # ex.: INSERT INTO t (cols) → Schema(t)
            found = target.find(exp.Table)
            return [found] if found else []
    return []


def _dangerous(node: exp.Expr) -> list[str]:
    out: list[str] = []
    if isinstance(node, exp.Delete) and not node.args.get("where"):
        out.append("DELETE sem WHERE")
    if isinstance(node, exp.Update) and not node.args.get("where"):
        out.append("UPDATE sem WHERE")
    if isinstance(node, exp.Drop):
        kind = str(node.args.get("kind") or "TABLE").upper()
        out.append(f"DROP {kind}")
    if isinstance(node, exp.TruncateTable):
        out.append("TRUNCATE")
    if isinstance(node, exp.Alter):
        actions: list[object] = list(node.args.get("actions") or [])
        for action in actions:
            if isinstance(action, exp.Drop):
                out.append("ALTER ... DROP")
    if isinstance(node, exp.Create) and node.args.get("replace"):
        out.append("CREATE OR REPLACE (sobrescreve)")
    return out


def _has_limit(node: exp.Expr) -> bool:
    """Leitura tabular com limite? (instruções que não são SELECT: não se aplica → True)."""
    if isinstance(node, exp.Select):
        return any(node.args.get(k) for k in ("limit", "fetch", "top"))
    if isinstance(node, exp.SetOperation):
        return bool(node.args.get("limit"))
    return True


def _classify(node: exp.Expr, dialect: str) -> tuple[SqlKind, SqlAnalysis | None]:
    """Devolve o tipo e, para EXPLAIN ANALYZE, a análise da instrução interna."""
    if isinstance(node, exp.Select) and node.args.get("into"):
        return SqlKind.DDL, None
    if isinstance(node, _READ_TYPES):
        return SqlKind.READ, None
    if isinstance(node, _DML_TYPES):
        return SqlKind.DML, None
    if isinstance(node, _DDL_TYPES):
        return SqlKind.DDL, None
    if isinstance(node, _ADMIN_TYPES):
        return SqlKind.ADMIN, None
    if isinstance(node, exp.Command):
        keyword = str(node.this).upper()
        expr = node.expression
        if isinstance(expr, exp.Literal):
            rest = str(expr.this).strip()
        elif isinstance(expr, exp.Expr):
            rest = expr.sql().strip()
        else:
            rest = str(expr or "").strip()
        if keyword == "EXPLAIN":
            inner = _EXPLAIN_ANALYZE.sub("", rest, count=1).strip()
            if inner != rest:  # EXPLAIN ANALYZE executa a instrução
                analysis = analyze(inner, dialect)
                return analysis.kind, analysis
            return SqlKind.READ, None
        if keyword in _READ_COMMANDS:
            return SqlKind.READ, None
        if keyword in _ADMIN_COMMANDS:
            return SqlKind.ADMIN, None
    return SqlKind.UNKNOWN, None


def analyze(sql: str, dialect: str = DEFAULT_DIALECT) -> SqlAnalysis:
    try:
        nodes = [n for n in sqlglot.parse(sql, read=dialect) if n is not None]
    except (ParseError, TokenError) as exc:
        count = max(1, len([s for s in sql.split(";") if s.strip()]))
        return SqlAnalysis(
            kind=SqlKind.UNKNOWN,
            statements=count,
            tables_read=[],
            tables_written=[],
            dangerous=[],
            has_limit=False,
            dialect=dialect,
            error=str(exc).splitlines()[0][:300],
        )
    if not nodes:
        return SqlAnalysis(
            kind=SqlKind.UNKNOWN,
            statements=0,
            tables_read=[],
            tables_written=[],
            dangerous=[],
            has_limit=False,
            dialect=dialect,
            error="SQL vazio",
        )
    kind = SqlKind.READ
    read: list[str] = []
    written: list[str] = []
    dangerous: list[str] = []
    for node in nodes:
        node_kind, inner = _classify(node, dialect)
        if RISK[node_kind] > RISK[kind]:
            kind = node_kind
        if inner is not None:
            read += inner.tables_read
            written += inner.tables_written
            dangerous += inner.dangerous
            continue
        ctes = _cte_names(node)
        w = [table_name(t) for t in _written(node)]
        written += w
        read += [n for t in _tables(node) if (n := table_name(t)) not in ctes and n not in w]
        dangerous += _dangerous(node)
    return SqlAnalysis(
        kind=kind,
        statements=len(nodes),
        tables_read=sorted(set(read)),
        tables_written=sorted(set(written)),
        dangerous=dangerous,
        has_limit=len(nodes) == 1 and _has_limit(nodes[0]),
        dialect=dialect,
    )


def inject_limit(sql: str, limit: int, dialect: str = DEFAULT_DIALECT) -> str | None:
    """Acrescenta LIMIT a um único SELECT sem limite, preservando o dialeto.

    Devolve ``None`` quando não se aplica (não é leitura simples ou já tem limite).
    """
    try:
        nodes = [n for n in sqlglot.parse(sql, read=dialect) if n is not None]
    except (ParseError, TokenError):
        return None
    if len(nodes) != 1:
        return None
    node = nodes[0]
    if not isinstance(node, (exp.Select, exp.SetOperation)) or _has_limit(node):
        return None
    if isinstance(node, exp.Select) and node.args.get("into"):
        return None
    return node.limit(limit).sql(dialect=dialect)


def pretty(sql: str, dialect: str = DEFAULT_DIALECT) -> str:
    """SQL formatado para o card de aprovação (cai para o original se não parsear)."""
    try:
        nodes = [n for n in sqlglot.parse(sql, read=dialect) if n is not None]
    except (ParseError, TokenError):
        return sql.strip()
    if not nodes or any(isinstance(n, exp.Command) for n in nodes):
        return sql.strip()
    return ";\n".join(n.sql(dialect=dialect, pretty=True) for n in nodes)
