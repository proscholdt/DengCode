"""Lineage de colunas a partir do SQL (sqlglot.lineage).

Para cada modelo (relação de saída + SQL compilado) gera arestas
``origem.tabela.coluna → modelo.coluna``. Nomes de tabela são normalizados para
os dois últimos componentes em minúsculas (``schema.tabela``), o que casa o SQL
compilado (``"db"."schema"."tabela"``) com as relações declaradas pelos modelos.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import Node
from sqlglot.lineage import lineage as _sqlglot_lineage  # pyright: ignore[reportUnknownVariableType]
from sqlglot.optimizer.qualify import qualify as _sqlglot_qualify  # pyright: ignore[reportUnknownVariableType]

from dengcode_core.ports.models import LineageEdge

log = logging.getLogger(__name__)

Schema = dict[str, dict[str, str]]

# assinaturas do sqlglot são parcialmente tipadas; fixamos o que usamos
_lineage = cast(Callable[..., Node], _sqlglot_lineage)
_qualify = cast(Callable[..., exp.Expr], _sqlglot_qualify)


def normalize(name: str, parts: int = 2) -> str:
    pieces = [p.strip().strip('"`[]').lower() for p in name.split(".") if p.strip()]
    return ".".join(pieces[-parts:])


def table_of(table: exp.Table) -> str:
    return normalize(".".join(p for p in (table.catalog, table.db, table.name) if p))


def output_columns(sql: str, dialect: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError:
        return []
    if not isinstance(tree, exp.Query):
        return []
    return [s.alias_or_name for s in tree.selects if s.alias_or_name and s.alias_or_name != "*"]


def _leaves(node: Node) -> list[Node]:
    return [n for n in node.walk() if not n.downstream]


def _nested(schema: Schema) -> dict[str, Any]:
    """``{"s.t": {col: tipo}}`` → ``{"s": {"t": {col: tipo}}}`` (formato do sqlglot)."""
    out: dict[str, Any] = {}
    for name, cols in schema.items():
        parts = name.split(".")
        if len(parts) != 2:
            continue
        out.setdefault(parts[0], {})[parts[1]] = dict(cols)
    return out


def _expand_star(sql: str, dialect: str, schema: dict[str, Any]) -> str:
    """Expande ``SELECT *`` com as colunas conhecidas (sem schema, devolve o SQL igual)."""
    if not schema or "*" not in sql:
        return sql
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
        qualified = _qualify(
            tree,
            schema=schema,
            dialect=dialect,
            validate_qualify_columns=False,
            quote_identifiers=False,
        )
        return qualified.sql(dialect=dialect)
    except (SqlglotError, KeyError, ValueError) as exc:
        log.debug("não foi possível expandir SELECT *: %s", exc)
        return sql


def column_lineage(
    model: str, sql: str, dialect: str, schema: Schema | None = None
) -> list[LineageEdge]:
    """Arestas de coluna de um modelo. ``schema`` ajuda a expandir ``SELECT *``."""
    target = normalize(model)
    nested = _nested(schema or {})
    sql = _expand_star(sql, dialect, nested)
    edges: list[LineageEdge] = []
    for column in output_columns(sql, dialect):
        try:
            root = _lineage(column, sql, cast(Any, nested), dialect=dialect)
        except (SqlglotError, KeyError, ValueError) as exc:
            log.debug("lineage de %s.%s falhou: %s", model, column, exc)
            continue
        for leaf in _leaves(root):
            if not isinstance(leaf.source, exp.Table):
                continue  # ex.: count(*) termina no SELECT, sem coluna de origem
            src_col = leaf.name.rsplit(".", 1)[-1].lower()
            edges.append(
                LineageEdge(
                    source=f"{table_of(leaf.source)}.{src_col}",
                    target=f"{target}.{column.lower()}",
                    via=model,
                )
            )
    return _dedupe(edges)


def build_column_lineage(
    models: Mapping[str, str], dialect: str, schema: Schema | None = None
) -> list[LineageEdge]:
    """``models``: relação → SQL compilado. Colunas de saída de cada modelo entram no
    schema usado pelos seguintes (ajuda com ``SELECT *`` entre modelos)."""
    known: Schema = dict(schema or {})
    # várias passadas: um SELECT * só expande depois que o modelo de origem é conhecido
    for _ in range(len(models) + 1):
        changed = False
        for relation, sql in models.items():
            cols = output_columns(_expand_star(sql, dialect, _nested(known)), dialect)
            rel = normalize(relation)
            if cols and rel not in known:
                known[rel] = {c.lower(): "VARCHAR" for c in cols}
                changed = True
        if not changed:
            break
    edges: list[LineageEdge] = []
    for relation, sql in models.items():
        edges += column_lineage(relation, sql, dialect, known)
    return _dedupe(edges)


def _dedupe(edges: list[LineageEdge]) -> list[LineageEdge]:
    seen: set[tuple[str, str]] = set()
    out: list[LineageEdge] = []
    for e in edges:
        key = (e.source, e.target)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out
