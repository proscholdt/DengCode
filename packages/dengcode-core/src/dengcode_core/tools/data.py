"""Ferramentas de dados, criadas a partir das portas ativas da stack.

Fluxo de ``query_run`` (sqlguard.md): SqlGuard.analyze → CostGuard → política
(no loop) → [aprovação] → execute com timeout → Parquet + prévia mascarada →
auditoria. Descrições neutras: nenhuma ferramenta cita produto pelo nome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from dengcode_core.audit import AuditLog, AuditRecord
from dengcode_core.data.results import ResultStore, render_for_llm
from dengcode_core.errors import ConfigError, DengCodeError, UnsupportedOperation
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.models import TableRef
from dengcode_core.ports.query_engine import QueryEnginePort
from dengcode_core.ports.storage import StoragePort
from dengcode_core.privacy.pii import PII_TAGS, PiiMasker, is_pii_column
from dengcode_core.sql.cost import CostCheck, CostLimit, check_cost
from dengcode_core.sql.guard import SqlAnalysis, analyze, inject_limit, pretty
from dengcode_core.tools.base import ActionRequest, Preview, Risk, Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)


@dataclass
class DataServices:
    """Tudo que as ferramentas de dados usam; montado pelo motor."""

    engines: dict[str, QueryEnginePort] = field(
        default_factory=lambda: dict[str, QueryEnginePort]()
    )
    catalog: CatalogPort | None = None
    storage: StoragePort | None = None
    results: ResultStore | None = None
    masker: PiiMasker = field(default_factory=PiiMasker)
    audit: AuditLog | None = None
    workspace: str = "default"
    stack: str | None = None
    env: str = "dev"
    cost_limit: CostLimit | None = None
    metered: dict[str, bool] = field(default_factory=lambda: dict[str, bool]())
    max_rows: int = 10_000
    timeout_s: int = 300
    explore_limit: int = 1_000

    def engine(self, name: str | None) -> tuple[str, QueryEnginePort]:
        if not self.engines:
            raise ConfigError("nenhum motor de consulta configurado na stack (compute)")
        key = name or next(iter(self.engines))
        if key not in self.engines:
            raise ConfigError(f"motor '{key}' não está na stack (há: {', '.join(self.engines)})")
        return key, self.engines[key]

    def record(self, **kwargs: Any) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(
                AuditRecord(workspace=self.workspace, stack=self.stack, env=self.env, **kwargs)
            )
        except OSError:
            log.exception("falha ao gravar auditoria")


def _ref(text: str) -> TableRef:
    try:
        return TableRef.parse(text)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


# ------------------------------------------------------------------- consulta
class QueryRunArgs(BaseModel):
    sql: str = Field(description="Uma instrução SQL no dialeto do motor.")
    engine: str | None = Field(default=None, description="Motor (padrão: o primeiro da stack).")
    max_rows: int | None = Field(
        default=None, ge=1, le=100_000, description="Máximo de linhas trazidas."
    )


class QueryRun(Tool[QueryRunArgs]):
    name: ClassVar[str] = "query_run"
    description: ClassVar[str] = (
        "Executa SQL no motor de consulta da stack. Leitura é livre (LIMIT é injetado em "
        "consultas exploratórias); escrita, DDL e DML pedem aprovação e podem ser bloqueadas. "
        "Devolve schema, contagem, prévia mascarada e estatísticas."
    )
    risk: ClassVar[Risk] = Risk.SQL
    Args = QueryRunArgs

    def __init__(self, services: DataServices) -> None:
        self.s = services
        self._cache: dict[tuple[str, str], tuple[SqlAnalysis, CostCheck]] = {}

    async def _inspect(self, args: QueryRunArgs) -> tuple[str, SqlAnalysis, CostCheck]:
        key, engine = self.s.engine(args.engine)
        cached = self._cache.get((key, args.sql))
        if cached is not None:
            return key, *cached
        analysis = analyze(args.sql, engine.capabilities().sql_dialect)
        cost = await check_cost(
            engine,
            args.sql,
            analysis,
            limit=self.s.cost_limit,
            metered=self.s.metered.get(key, False),
        )
        self._cache[(key, args.sql)] = (analysis, cost)
        return key, analysis, cost

    async def assess(self, args: QueryRunArgs, ctx: ToolContext) -> ActionRequest:
        key, analysis, cost = await self._inspect(args)
        return ActionRequest(
            self.name,
            self.risk,
            details={
                "engine": key,
                "sql_kind": analysis.kind.value,
                "statements": analysis.statements,
                "tables_read": analysis.tables_read,
                "tables_written": analysis.tables_written,
                "dangerous": analysis.dangerous,
                "cost": cost.describe(),
                "cost_reason": cost.reason,
                "parse_error": analysis.error,
            },
        )

    async def preview(self, args: QueryRunArgs, ctx: ToolContext) -> Preview | None:
        _, analysis, cost = await self._inspect(args)
        details: dict[str, Any] = {"plan": (cost.plan or "")[:4000]} if cost.plan else {}
        return Preview("sql", pretty(args.sql, analysis.dialect), details)

    async def on_blocked(
        self, args: QueryRunArgs, request: ActionRequest, decision: str, reason: str
    ) -> None:
        self.s.record(
            engine=request.details.get("engine"),
            sql=args.sql,
            kind=request.details.get("sql_kind"),
            decision=decision,
            error=reason,
        )

    async def _pii_columns(self, analysis: SqlAnalysis) -> list[str]:
        catalog = self.s.catalog
        if catalog is None or not catalog.capabilities().supports_tags:
            return []
        cols: list[str] = []
        for name in analysis.tables_read[:10]:
            try:
                info = await catalog.describe(TableRef.parse(name))
            except (DengCodeError, ValueError):
                continue
            cols += [c.name for c in info.columns if PII_TAGS.intersection(c.tags)]
        return cols

    async def run(self, args: QueryRunArgs, ctx: ToolContext) -> ToolResult:
        key, engine = self.s.engine(args.engine)
        _, analysis, cost = await self._inspect(args)
        sql = args.sql
        note: str | None = None
        if analysis.is_read and not analysis.has_limit:
            limited = inject_limit(sql, self.s.explore_limit, analysis.dialect)
            if limited is not None:
                sql = limited
                note = f"aviso: LIMIT {self.s.explore_limit} injetado (consulta exploratória)"
        max_rows = args.max_rows or self.s.max_rows
        try:
            result = await engine.execute(sql, max_rows=max_rows, timeout_s=self.s.timeout_s)
        except DengCodeError as exc:
            self.s.record(
                engine=key,
                sql=sql,
                kind=analysis.kind.value,
                decision="executed",
                estimated_cost=cost.describe(),
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        path = None
        if self.s.results is not None and result.preview.num_rows:
            path = self.s.results.save(result.preview, result.query_id)
        self.s.record(
            engine=key,
            sql=sql,
            kind=analysis.kind.value,
            decision="executed",
            estimated_cost=cost.describe(),
            actual_cost=result.cost.model_dump_json() if result.cost else None,
            rows=result.total_rows if result.total_rows is not None else result.preview.num_rows,
            elapsed_ms=result.elapsed_ms,
        )
        if not analysis.is_read and not result.preview.num_rows:
            return ToolResult(
                f"ok: {analysis.kind.value.upper()} executado em {result.elapsed_ms} ms",
                metadata={"sql": sql},
            )
        text = render_for_llm(
            result.preview,
            total_rows=result.total_rows,
            path=path,
            masker=self.s.masker,
            pii_columns=await self._pii_columns(analysis),
            note=note,
        )
        return ToolResult(
            text,
            metadata={
                "sql": sql,
                "rows": result.preview.num_rows,
                "result_path": str(path) if path else None,
            },
        )


class QueryExplainArgs(BaseModel):
    sql: str = Field(description="Consulta a explicar (não é executada).")
    engine: str | None = Field(default=None, description="Motor (padrão: o primeiro).")


class QueryExplain(Tool[QueryExplainArgs]):
    name: ClassVar[str] = "query_explain"
    description: ClassVar[str] = "Mostra o plano de execução de uma consulta sem executá-la."
    risk: ClassVar[Risk] = Risk.READ
    Args = QueryExplainArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, services: DataServices) -> None:
        self.s = services

    async def run(self, args: QueryExplainArgs, ctx: ToolContext) -> ToolResult:
        _, engine = self.s.engine(args.engine)
        if analyze(args.sql, engine.capabilities().sql_dialect).statements != 1:
            return ToolResult("explique uma instrução por vez", is_error=True)
        return ToolResult(await engine.explain(args.sql))


# ------------------------------------------------------------------- catálogo
class CatalogListArgs(BaseModel):
    schema_name: str | None = Field(default=None, description="Schema; vazio lista todos.")
    catalog: str | None = Field(default=None, description="Catálogo (quando houver vários).")


class CatalogListTables(Tool[CatalogListArgs]):
    name: ClassVar[str] = "catalog_list_tables"
    description: ClassVar[str] = "Lista schemas e tabelas conhecidas pelo catálogo."
    risk: ClassVar[Risk] = Risk.READ
    Args = CatalogListArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, catalog: CatalogPort) -> None:
        self.catalog = catalog

    async def run(self, args: CatalogListArgs, ctx: ToolContext) -> ToolResult:
        schemas = (
            [args.schema_name]
            if args.schema_name
            else await self.catalog.list_schemas(args.catalog)
        )
        lines: list[str] = []
        for schema in schemas[:50]:
            tables = await self.catalog.list_tables(schema, catalog=args.catalog)
            lines.append(f"{schema}: " + (", ".join(t.name for t in tables) or "(vazio)"))
        return ToolResult("\n".join(lines) or "(nenhum schema)")


class TableArgs(BaseModel):
    table: str = Field(description="Tabela: nome, schema.nome ou catalogo.schema.nome.")


class CatalogDescribe(Tool[TableArgs]):
    name: ClassVar[str] = "catalog_describe"
    description: ClassVar[str] = (
        "Descreve uma tabela: colunas, tipos, tags (ex.: PII), linhas, formato e hash do schema."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = TableArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, catalog: CatalogPort) -> None:
        self.catalog = catalog

    async def run(self, args: TableArgs, ctx: ToolContext) -> ToolResult:
        info = await self.catalog.describe(_ref(args.table))
        lines = [
            f"tabela: {info.ref.qualified()}",
            f"linhas: {info.row_count if info.row_count is not None else 'desconhecido'}",
            f"formato: {info.table_format or '-'} · local: {info.location or '-'}",
            f"schema_hash: {info.schema_hash}",
        ]
        if info.description:
            lines.append(f"descrição: {info.description}")
        lines.append("colunas:")
        for c in info.columns:
            flags = [] if c.nullable is None else (["nulo"] if c.nullable else ["não nulo"])
            if is_pii_column(c.name, c.tags):
                flags.append("PII")
            tags = f" tags={sorted(c.tags)}" if c.tags else ""
            desc = f" — {c.description}" if c.description else ""
            lines.append(f"- {c.name}: {c.data_type} [{', '.join(flags)}]{tags}{desc}")
        return ToolResult("\n".join(lines))


class StatsArgs(BaseModel):
    table: str = Field(description="Tabela.")
    columns: list[str] | None = Field(default=None, description="Colunas (padrão: todas).")


class CatalogColumnStats(Tool[StatsArgs]):
    name: ClassVar[str] = "catalog_column_stats"
    description: ClassVar[str] = (
        "Estatísticas por coluna: fração de nulos, distintos, mínimo e máximo "
        "(omitidos em colunas PII)."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = StatsArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, catalog: CatalogPort, masker: PiiMasker) -> None:
        self.catalog = catalog
        self.masker = masker

    async def run(self, args: StatsArgs, ctx: ToolContext) -> ToolResult:
        ref = _ref(args.table)
        if not self.catalog.capabilities().supports_column_stats:
            raise UnsupportedOperation("o catálogo não oferece estatísticas de coluna")
        info = await self.catalog.describe(ref)
        tags = {c.name: c.tags for c in info.columns}
        lines: list[str] = []
        for s in await self.catalog.column_stats(ref, args.columns):
            pii = is_pii_column(s.column, tags.get(s.column, frozenset()))
            nulls = f"{s.null_fraction:.2%}" if s.null_fraction is not None else "?"
            rng = (
                "PII: omitido"
                if pii
                else (
                    f"min {self.masker.text(s.min or '∅')} · max {self.masker.text(s.max or '∅')}"
                )
            )
            lines.append(f"- {s.column}: nulos {nulls}, distintos {s.distinct_count}, {rng}")
        return ToolResult("\n".join(lines) or "(sem estatísticas)")


# ------------------------------------------------------------------- storage
class StorageListArgs(BaseModel):
    uri: str = Field(default=".", description="Pasta/prefixo a listar.")
    recursive: bool = Field(default=False, description="Incluir subpastas.")


class StorageList(Tool[StorageListArgs]):
    name: ClassVar[str] = "storage_list"
    description: ClassVar[str] = "Lista arquivos/objetos do armazenamento de dados da stack."
    risk: ClassVar[Risk] = Risk.READ
    Args = StorageListArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, storage: StoragePort) -> None:
        self.storage = storage

    async def run(self, args: StorageListArgs, ctx: ToolContext) -> ToolResult:
        items = await self.storage.list(args.uri, recursive=args.recursive, limit=500)
        lines = [
            f"{'[dir] ' if o.is_dir else ''}{o.uri}"
            + (f" ({o.size_bytes} bytes)" if o.size_bytes is not None else "")
            for o in items
        ]
        return ToolResult("\n".join(lines) or "(vazio)")


class StorageSampleArgs(BaseModel):
    uri: str = Field(description="Arquivo/objeto tabular (csv, parquet, json).")
    rows: int = Field(default=20, ge=1, le=200, description="Linhas da amostra.")
    fmt: str | None = Field(default=None, description="Formato, se a extensão não disser.")


class StorageSample(Tool[StorageSampleArgs]):
    name: ClassVar[str] = "storage_sample"
    description: ClassVar[str] = (
        "Lê uma amostra de um arquivo tabular do armazenamento (PII mascarada) com estatísticas."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = StorageSampleArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, storage: StoragePort, masker: PiiMasker) -> None:
        self.storage = storage
        self.masker = masker

    async def run(self, args: StorageSampleArgs, ctx: ToolContext) -> ToolResult:
        table = await self.storage.read_sample(args.uri, rows=args.rows, fmt=args.fmt)
        text = render_for_llm(
            table,
            total_rows=None,
            path=None,
            masker=self.masker,
            preview_rows=args.rows,
            note=f"amostra das primeiras {table.num_rows} linhas",
        )
        return ToolResult(text)


class StorageSchemaArgs(BaseModel):
    uri: str = Field(description="Arquivo/objeto tabular.")
    fmt: str | None = Field(default=None, description="Formato, se a extensão não disser.")


class StorageSchema(Tool[StorageSchemaArgs]):
    name: ClassVar[str] = "storage_schema"
    description: ClassVar[str] = "Infere o schema (colunas e tipos) de um arquivo tabular."
    risk: ClassVar[Risk] = Risk.READ
    Args = StorageSchemaArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, storage: StoragePort) -> None:
        self.storage = storage

    async def run(self, args: StorageSchemaArgs, ctx: ToolContext) -> ToolResult:
        schema = await self.storage.infer_schema(args.uri, fmt=args.fmt)
        return ToolResult(schema.to_string(show_schema_metadata=False))


def data_tools(services: DataServices) -> list[Tool[Any]]:
    tools: list[Tool[Any]] = []
    if services.engines:
        tools += [QueryRun(services), QueryExplain(services)]
    if services.catalog is not None:
        tools += [
            CatalogListTables(services.catalog),
            CatalogDescribe(services.catalog),
            CatalogColumnStats(services.catalog, services.masker),
        ]
    if services.storage is not None:
        tools += [
            StorageList(services.storage),
            StorageSample(services.storage, services.masker),
            StorageSchema(services.storage),
        ]
    return tools
