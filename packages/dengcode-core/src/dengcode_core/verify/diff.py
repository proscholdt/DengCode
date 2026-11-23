# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false
# (os stubs do pyarrow são incompletos para Schema.field().type)
"""Verifier / data diff: compara duas relações (antes/depois, dev/prod).

Todo SQL é gerado em DuckDB e transpilado com sqlglot para o dialeto do motor;
cada instrução é conferida pelo SqlGuard (precisa ser READ) antes de executar.
"""

from __future__ import annotations

import logging
from typing import Any

import pyarrow as pa
import sqlglot
from pydantic import BaseModel, Field
from sqlglot import exp
from sqlglot.errors import ParseError

from dengcode_core.errors import ConfigError, DengCodeError
from dengcode_core.ports.query_engine import QueryEnginePort
from dengcode_core.privacy.pii import PiiMasker, is_pii_column
from dengcode_core.sql.guard import SqlKind, analyze

log = logging.getLogger(__name__)

BASE = "duckdb"
TOP_N = 5
MAX_TOP_COLUMNS = 8


class ColumnDiff(BaseModel):
    name: str
    data_type: str
    nulls_before: int | None = None
    nulls_after: int | None = None
    distinct_before: int | None = None
    distinct_after: int | None = None
    min_before: str | None = None
    min_after: str | None = None
    max_before: str | None = None
    max_after: str | None = None
    avg_before: float | None = None
    avg_after: float | None = None
    top_before: list[tuple[str, int]] = Field(default_factory=lambda: list[tuple[str, int]]())
    top_after: list[tuple[str, int]] = Field(default_factory=lambda: list[tuple[str, int]]())


class DataDiffReport(BaseModel):
    before: str
    after: str
    key: list[str]
    rows_before: int
    rows_after: int
    duplicate_keys_before: int
    duplicate_keys_after: int
    only_before: int
    only_after: int
    sample_only_before: list[str] = Field(default_factory=lambda: list[str]())
    sample_only_after: list[str] = Field(default_factory=lambda: list[str]())
    changed_rows: int | None = None
    added_columns: list[str] = Field(default_factory=lambda: list[str]())
    removed_columns: list[str] = Field(default_factory=lambda: list[str]())
    columns: list[ColumnDiff] = Field(default_factory=lambda: list[ColumnDiff]())

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.rows_after != self.rows_before:
            delta = self.rows_after - self.rows_before
            out.append(
                f"contagem de linhas mudou: {self.rows_before} → {self.rows_after} ({delta:+d})"
            )
        if self.duplicate_keys_after > self.duplicate_keys_before:
            out.append(
                f"chaves duplicadas novas: {self.duplicate_keys_before} → "
                f"{self.duplicate_keys_after}"
            )
        if self.only_before:
            out.append(f"{self.only_before} chave(s) sumiram")
        if self.only_after:
            out.append(f"{self.only_after} chave(s) novas")
        for c in self.columns:
            if (
                c.nulls_before is not None
                and c.nulls_after is not None
                and c.nulls_after > c.nulls_before
            ):
                out.append(f"nulos novos em {c.name}: {c.nulls_before} → {c.nulls_after}")
        return out

    def summary(self) -> str:
        lines = [
            f"data diff · antes: {self.before} · depois: {self.after} · chave: "
            f"{', '.join(self.key)}",
            f"linhas: {self.rows_before} → {self.rows_after}",
            f"chaves duplicadas: {self.duplicate_keys_before} → {self.duplicate_keys_after}",
            f"só no antes: {self.only_before} · só no depois: {self.only_after}"
            + (
                f" · linhas alteradas (mesma chave): {self.changed_rows}"
                if self.changed_rows is not None
                else ""
            ),
        ]
        if self.added_columns:
            lines.append(f"colunas novas: {', '.join(self.added_columns)}")
        if self.removed_columns:
            lines.append(f"colunas removidas: {', '.join(self.removed_columns)}")
        if self.sample_only_before:
            lines.append(f"exemplos só no antes: {', '.join(self.sample_only_before)}")
        if self.sample_only_after:
            lines.append(f"exemplos só no depois: {', '.join(self.sample_only_after)}")
        lines.append("por coluna:")
        for c in self.columns:
            parts = [f"nulos {_pair(c.nulls_before, c.nulls_after)}"]
            if c.distinct_before is not None or c.distinct_after is not None:
                parts.append(f"distintos {_pair(c.distinct_before, c.distinct_after)}")
            if c.min_before is not None or c.min_after is not None:
                parts.append(f"min {_pair(c.min_before, c.min_after)}")
                parts.append(f"max {_pair(c.max_before, c.max_after)}")
            if c.avg_before is not None or c.avg_after is not None:
                parts.append(f"média {_pair(_fmt(c.avg_before), _fmt(c.avg_after))}")
            if c.top_after or c.top_before:
                parts.append(f"top depois {_top(c.top_after)}")
            lines.append(f"- {c.name} ({c.data_type}): " + "; ".join(parts))
        warnings = self.warnings
        lines.append("alertas: " + ("; ".join(warnings) if warnings else "nenhum"))
        return "\n".join(lines)


def _fmt(v: float | None) -> str | None:
    return None if v is None else f"{v:.4g}"


def _pair(a: object, b: object) -> str:
    a_s, b_s = ("∅" if a is None else str(a)), ("∅" if b is None else str(b))
    return a_s if a_s == b_s else f"{a_s} → {b_s}"


def _top(items: list[tuple[str, int]]) -> str:
    return ", ".join(f"{v}={n}" for v, n in items) or "-"


def _ident(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect=BASE)


def _relation(name: str) -> str:
    try:
        return exp.to_table(name, dialect=BASE).sql(dialect=BASE)
    except (ValueError, ParseError) as exc:
        raise ConfigError(f"relação inválida: {name!r}") from exc


def _is_numeric(t: pa.DataType) -> bool:
    return bool(pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_decimal(t))


def _is_ordered(t: pa.DataType) -> bool:
    return _is_numeric(t) or bool(pa.types.is_temporal(t))


def _is_text(t: pa.DataType) -> bool:
    return bool(pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_boolean(t))


class DataDiff:
    def __init__(
        self, engine: QueryEnginePort, *, masker: PiiMasker | None = None, timeout_s: int = 300
    ) -> None:
        self.engine = engine
        self.dialect = engine.capabilities().sql_dialect
        self.masker = masker or PiiMasker()
        self.timeout_s = timeout_s

    async def _query(self, sql: str, max_rows: int = 100) -> pa.Table:
        target = sqlglot.transpile(sql, read=BASE, write=self.dialect)[0]
        if analyze(target, self.dialect).kind is not SqlKind.READ:
            raise ConfigError("data diff só executa leituras")
        result = await self.engine.execute(target, max_rows=max_rows, timeout_s=self.timeout_s)
        return result.preview

    async def _scalar(self, sql: str) -> int:
        table = await self._query(sql, 1)
        value = table.column(0)[0].as_py() if table.num_rows else 0
        return int(value or 0)

    async def _schema(self, rel: str) -> pa.Schema:
        return (await self._query(f"SELECT * FROM {rel} LIMIT 0", 1)).schema

    async def compare(
        self, before: str, after: str, key: list[str], columns: list[str] | None = None
    ) -> DataDiffReport:
        if not key:
            raise ConfigError("informe ao menos uma coluna de chave")
        b, a = _relation(before), _relation(after)
        sb, sa = await self._schema(b), await self._schema(a)
        names_b, names_a = list(sb.names), list(sa.names)
        missing = [k for k in key if k not in names_b or k not in names_a]
        if missing:
            raise ConfigError(f"chave ausente em uma das relações: {', '.join(missing)}")
        common = [c for c in names_a if c in names_b]
        if columns:
            common = [c for c in common if c in columns or c in key]
        keys = ", ".join(_ident(k) for k in key)

        rows_b = await self._scalar(f"SELECT count(*) FROM {b}")
        rows_a = await self._scalar(f"SELECT count(*) FROM {a}")
        dup = "SELECT count(*) FROM (SELECT {k} FROM {r} GROUP BY {k} HAVING count(*) > 1) d"
        dup_b = await self._scalar(dup.format(k=keys, r=b))
        dup_a = await self._scalar(dup.format(k=keys, r=a))
        only = "SELECT {k} FROM {x} EXCEPT SELECT {k} FROM {y}"
        only_b_sql, only_a_sql = only.format(k=keys, x=b, y=a), only.format(k=keys, x=a, y=b)
        only_b = await self._scalar(f"SELECT count(*) FROM ({only_b_sql}) o")
        only_a = await self._scalar(f"SELECT count(*) FROM ({only_a_sql}) o")
        sample_b = await self._sample(f"{only_b_sql} LIMIT 5", key)
        sample_a = await self._sample(f"{only_a_sql} LIMIT 5", key)

        compared = [c for c in common if c not in key]
        changed: int | None = None
        if compared:
            on = " AND ".join(f"x.{_ident(k)} = y.{_ident(k)}" for k in key)
            diff = " OR ".join(f"x.{_ident(c)} IS DISTINCT FROM y.{_ident(c)}" for c in compared)
            changed = await self._scalar(
                f"SELECT count(*) FROM {b} x JOIN {a} y ON {on} WHERE {diff}"
            )

        col_diffs: list[ColumnDiff] = []
        top_budget = MAX_TOP_COLUMNS
        for name in names_a if not columns else [c for c in names_a if c in columns]:
            t_after = sa.field(name).type
            in_before = name in names_b
            diff_c = ColumnDiff(name=name, data_type=str(t_after))
            await self._fill(diff_c, a, name, t_after, side="after")
            if in_before:
                await self._fill(diff_c, b, name, sb.field(name).type, side="before")
            if _is_text(t_after) and not is_pii_column(name) and top_budget > 0:
                top_budget -= 1
                diff_c.top_after = await self._top(a, name)
                if in_before:
                    diff_c.top_before = await self._top(b, name)
            col_diffs.append(diff_c)

        return DataDiffReport(
            before=before,
            after=after,
            key=key,
            rows_before=rows_b,
            rows_after=rows_a,
            duplicate_keys_before=dup_b,
            duplicate_keys_after=dup_a,
            only_before=only_b,
            only_after=only_a,
            sample_only_before=sample_b,
            sample_only_after=sample_a,
            changed_rows=changed,
            added_columns=[c for c in names_a if c not in names_b],
            removed_columns=[c for c in names_b if c not in names_a],
            columns=col_diffs,
        )

    async def _sample(self, sql: str, key: list[str]) -> list[str]:
        try:
            table = await self._query(sql, 5)
        except DengCodeError as exc:
            log.debug("amostra do diff falhou: %s", exc)
            return []
        rows = table.to_pylist()
        return [self.masker.text("/".join(str(r[k]) for k in key)) for r in rows]

    async def _fill(self, c: ColumnDiff, rel: str, name: str, t: pa.DataType, side: str) -> None:
        q = _ident(name)
        pii = is_pii_column(name)
        parts = [f"count(*) - count({q}) AS nulls", f"count(DISTINCT {q}) AS distinct_"]
        if _is_ordered(t) and not pii:
            parts += [f"CAST(min({q}) AS VARCHAR) AS min_", f"CAST(max({q}) AS VARCHAR) AS max_"]
        if _is_numeric(t) and not pii:
            parts.append(f"avg(CAST({q} AS DOUBLE)) AS avg_")
        row: dict[str, Any] = (
            await self._query(f"SELECT {', '.join(parts)} FROM {rel}", 1)
        ).to_pylist()[0]
        setattr(c, f"nulls_{side}", int(row["nulls"]))
        setattr(c, f"distinct_{side}", int(row["distinct_"]))
        if "min_" in row:
            setattr(c, f"min_{side}", row["min_"])
            setattr(c, f"max_{side}", row["max_"])
        if "avg_" in row and row["avg_"] is not None:
            setattr(c, f"avg_{side}", float(row["avg_"]))

    async def _top(self, rel: str, name: str) -> list[tuple[str, int]]:
        q = _ident(name)
        table = await self._query(
            f"SELECT CAST({q} AS VARCHAR) AS v, count(*) AS n FROM {rel} "
            f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {TOP_N}",
            TOP_N,
        )
        return [(self.masker.text(str(r["v"])), int(r["n"])) for r in table.to_pylist()]
