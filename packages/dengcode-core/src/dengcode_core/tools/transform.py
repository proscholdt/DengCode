"""Ferramentas de transformação, lineage, verificação e conhecimento (fase 4)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from dengcode_core.errors import DengCodeError
from dengcode_core.knowledge import KnowledgeBase
from dengcode_core.lineage.columns import build_column_lineage
from dengcode_core.lineage.graph import LineageGraph
from dengcode_core.policy.permission import SCRATCH_SCHEMA_PREFIX
from dengcode_core.ports.catalog import CatalogPort
from dengcode_core.ports.models import LineageEdge, TableRef
from dengcode_core.ports.transform import TransformPort, TransformRunResult
from dengcode_core.sql.guard import analyze
from dengcode_core.tools.base import ActionRequest, Preview, Risk, Tool, ToolContext, ToolResult
from dengcode_core.tools.data import DataServices
from dengcode_core.verify.diff import DataDiff


def scratch_schema(session_id: str) -> str:
    return f"{SCRATCH_SCHEMA_PREFIX}{session_id.lower()}"


def _run_text(result: TransformRunResult) -> str:
    head = (
        f"{result.command} {result.selector} (alvo {result.target or '-'}): "
        f"{'SUCESSO' if result.success else 'FALHOU'}"
    )
    lines = [head]
    for r in result.results:
        extra = f" · {r.message}" if r.message and r.status not in ("success", "pass") else ""
        time = f" ({r.elapsed_s:.1f}s)" if r.elapsed_s is not None else ""
        lines.append(f"- {r.name}: {r.status}{time}{extra}")
    if not result.success and result.logs:
        lines += ["logs (final):", result.logs[-3000:]]
    return "\n".join(lines)


class LineageService:
    """Monta o grafo: SQL compilado dos modelos (colunas) + manifesto + catálogo."""

    def __init__(
        self, transform: TransformPort | None, catalog: CatalogPort | None, dialect: str
    ) -> None:
        self.transform = transform
        self.catalog = catalog
        self.dialect = dialect

    async def graph(self) -> LineageGraph:
        column_edges: list[LineageEdge] = []
        table_edges: list[LineageEdge] = []
        if self.transform is not None:
            models = await self.transform.list_models()
            compiled: dict[str, str] = {}
            for m in models:
                try:
                    compiled[m.relation or m.name] = await self.transform.compile(m.name)
                except DengCodeError:
                    continue
            column_edges += build_column_lineage(compiled, self.dialect)
            table_edges += await self.transform.manifest_lineage()
        if self.catalog is not None and self.catalog.capabilities().supports_lineage:
            tables = {e.source for e in table_edges} | {e.target for e in table_edges}
            for name in sorted(tables)[:50]:
                try:
                    table_edges += await self.catalog.lineage(
                        TableRef.parse(name), direction="down", depth=1
                    )
                except (DengCodeError, ValueError):
                    continue
        return LineageGraph(column_edges, table_edges)


# ----------------------------------------------------------------- transformação
class NoArgs(BaseModel):
    selector: str | None = Field(default=None, description="Seletor (padrão: todos).")


class TransformListModels(Tool[NoArgs]):
    name: ClassVar[str] = "transform_list_models"
    description: ClassVar[str] = (
        "Lista os modelos do projeto de transformação: arquivo, relação, materialização e "
        "dependências."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = NoArgs
    parallel_safe: ClassVar[bool] = True
    untrusted_output: ClassVar[bool] = False

    def __init__(self, transform: TransformPort) -> None:
        self.transform = transform

    async def run(self, args: NoArgs, ctx: ToolContext) -> ToolResult:
        models = await self.transform.list_models(args.selector)
        lines = [
            f"- {m.name} [{m.materialized or '?'}] → {m.relation or '?'} · arquivo "
            f"{m.path or '?'} · depende de: {', '.join(m.depends_on) or '-'}"
            for m in models
        ]
        return ToolResult("\n".join(lines) or "(nenhum modelo)")


class ModelArgs(BaseModel):
    model: str = Field(description="Nome do modelo.")


class TransformCompile(Tool[ModelArgs]):
    name: ClassVar[str] = "transform_compile"
    description: ClassVar[str] = "Compila um modelo e devolve o SQL final (não executa)."
    risk: ClassVar[Risk] = Risk.READ
    Args = ModelArgs
    parallel_safe: ClassVar[bool] = True

    def __init__(self, transform: TransformPort) -> None:
        self.transform = transform

    async def run(self, args: ModelArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult(await self.transform.compile(args.model))


class SelectorArgs(BaseModel):
    selector: str = Field(description="Seletor de modelos, ex.: 'silver_pedidos+'.")
    target: str | None = Field(default=None, description="Alvo/ambiente (padrão: o da sessão).")


class TransformRun(Tool[SelectorArgs]):
    name: ClassVar[str] = "transform_run"
    description: ClassVar[str] = (
        "Materializa os modelos selecionados no alvo (escreve no banco; pede aprovação)."
    )
    risk: ClassVar[Risk] = Risk.WRITE_REMOTE
    Args = SelectorArgs

    def __init__(self, transform: TransformPort, default_target: str) -> None:
        self.transform = transform
        self.default_target = default_target

    async def preview(self, args: SelectorArgs, ctx: ToolContext) -> Preview | None:
        target = args.target or self.default_target
        return Preview(
            "command", f"run --select {args.selector} --target {target}", {"target": target}
        )

    async def run(self, args: SelectorArgs, ctx: ToolContext) -> ToolResult:
        result = await self.transform.run(args.selector, target=args.target or self.default_target)
        return ToolResult(_run_text(result), is_error=not result.success)


class TransformTest(Tool[SelectorArgs]):
    name: ClassVar[str] = "transform_test"
    description: ClassVar[str] = "Roda os testes de dados dos modelos selecionados (só leitura)."
    risk: ClassVar[Risk] = Risk.READ
    Args = SelectorArgs

    def __init__(self, transform: TransformPort, default_target: str) -> None:
        self.transform = transform
        self.default_target = default_target

    async def run(self, args: SelectorArgs, ctx: ToolContext) -> ToolResult:
        result = await self.transform.test(args.selector, target=args.target or self.default_target)
        return ToolResult(_run_text(result), is_error=not result.success)


# ----------------------------------------------------------------- impacto
class ImpactArgs(BaseModel):
    target: str = Field(description="Tabela/modelo ou tabela.coluna.")
    depth: int = Field(default=5, ge=1, le=20, description="Níveis a percorrer.")
    direction: str = Field(
        default="down", pattern="^(up|down)$", description="down = quem depende; up = de onde vem."
    )


class ImpactAnalysis(Tool[ImpactArgs]):
    name: ClassVar[str] = "impact_analysis"
    description: ClassVar[str] = (
        "Lista o que depende (ou de onde vem) uma tabela ou coluna, com o caminho, usando o "
        "lineage calculado pelo SQL dos modelos e pelo catálogo."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = ImpactArgs
    parallel_safe: ClassVar[bool] = True
    untrusted_output: ClassVar[bool] = False

    def __init__(self, lineage: LineageService) -> None:
        self.lineage = lineage

    async def run(self, args: ImpactArgs, ctx: ToolContext) -> ToolResult:
        graph = await self.lineage.graph()
        found = graph.resolve(args.target)
        note = ""
        if found is None and "." in args.target:
            table = args.target.rsplit(".", 1)[0]
            found = graph.resolve(table)
            note = (
                f"(coluna '{args.target.rsplit('.', 1)[1]}' ainda não existe no lineage; "
                f"mostrando o impacto da tabela)\n"
            )
        if found is None:
            return ToolResult(f"'{args.target}' não aparece no lineage", is_error=True)
        entity, kind = found
        direction = "up" if args.direction == "up" else "down"
        items = graph.walk(entity, direction=direction, depth=args.depth)
        if not items:
            return ToolResult(
                f"{note}{entity} ({kind}): nada {'acima' if direction == 'up' else 'abaixo'}"
            )
        lines = [
            f"{note}{entity} ({kind}) · {'downstream' if direction == 'down' else 'upstream'}:"
        ]
        for item in items:
            lines.append(
                f"- nível {item.level}: {item.entity} · caminho: "
                + " → ".join(item.path)
                + (f" · via {item.via}" if item.via else "")
            )
        return ToolResult("\n".join(lines))


# ----------------------------------------------------------------- verificação
class SnapshotArgs(BaseModel):
    table: str = Field(description="Relação a copiar antes da mudança.")


class DataSnapshot(Tool[SnapshotArgs]):
    name: ClassVar[str] = "data_snapshot"
    description: ClassVar[str] = (
        "Copia uma relação para o schema temporário da sessão (antes de mudar um modelo), "
        "para comparar depois com data_diff. Devolve o nome da cópia."
    )
    risk: ClassVar[Risk] = Risk.SQL
    Args = SnapshotArgs

    def __init__(self, services: DataServices) -> None:
        self.s = services

    def _sql(self, args: SnapshotArgs, ctx: ToolContext) -> tuple[str, str, str]:
        ref = TableRef.parse(args.table)
        schema = scratch_schema(ctx.session_id)
        copy = f"{schema}.{ref.name}_antes"
        create_schema = f"CREATE SCHEMA IF NOT EXISTS {schema}"
        create = f"CREATE OR REPLACE TABLE {copy} AS SELECT * FROM {ref.qualified()}"
        return create_schema, create, copy

    async def assess(self, args: SnapshotArgs, ctx: ToolContext) -> ActionRequest:
        key, engine = self.s.engine(None)
        _, create, copy = self._sql(args, ctx)
        a = analyze(create, engine.capabilities().sql_dialect)
        return ActionRequest(
            self.name,
            self.risk,
            details={
                "engine": key,
                "sql_kind": a.kind.value,
                "tables_written": [copy.lower()],
                "tables_read": a.tables_read,
                "dangerous": [],
                "statements": 1,
            },
        )

    async def preview(self, args: SnapshotArgs, ctx: ToolContext) -> Preview | None:
        return Preview("sql", self._sql(args, ctx)[1])

    async def run(self, args: SnapshotArgs, ctx: ToolContext) -> ToolResult:
        _, engine = self.s.engine(None)
        create_schema, create, copy = self._sql(args, ctx)
        await engine.execute(create_schema)
        await engine.execute(create)
        self.s.record(engine=None, sql=create, kind="ddl", decision="executed")
        return ToolResult(f"cópia criada: {copy}")


class DiffArgs(BaseModel):
    before: str = Field(description="Relação de referência (ex.: a cópia do data_snapshot).")
    after: str = Field(description="Relação nova.")
    key: list[str] = Field(description="Coluna(s) que identificam a linha.")
    columns: list[str] | None = Field(default=None, description="Restringir colunas.")


class DataDiffTool(Tool[DiffArgs]):
    name: ClassVar[str] = "data_diff"
    description: ClassVar[str] = (
        "Compara duas relações: linhas, chaves duplicadas, linhas só de um lado, alteradas, "
        "nulos, min/max/média e valores mais comuns. Use para provar que a mudança está certa."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = DiffArgs

    def __init__(self, services: DataServices) -> None:
        self.s = services

    async def run(self, args: DiffArgs, ctx: ToolContext) -> ToolResult:
        _, engine = self.s.engine(None)
        report = await DataDiff(engine, masker=self.s.masker).compare(
            args.before, args.after, args.key, args.columns
        )
        return ToolResult(report.summary(), metadata={"warnings": len(report.warnings)})


# ----------------------------------------------------------------- conhecimento
class KnowledgeArgs(BaseModel):
    id: str = Field(description="Id do conhecimento (ver índice no prompt).")


class KnowledgeRead(Tool[KnowledgeArgs]):
    name: ClassVar[str] = "knowledge_read"
    description: ClassVar[str] = (
        "Lê um texto da base de conhecimento (fundamentos de engenharia de dados e boas "
        "práticas das ferramentas da stack)."
    )
    risk: ClassVar[Risk] = Risk.READ
    Args = KnowledgeArgs
    parallel_safe: ClassVar[bool] = True
    untrusted_output: ClassVar[bool] = False

    def __init__(self, base: KnowledgeBase) -> None:
        self.base = base

    async def run(self, args: KnowledgeArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult(self.base.read(args.id))


def transform_tools(
    services: DataServices,
    transform: TransformPort | None,
    *,
    default_target: str,
    knowledge: KnowledgeBase | None,
) -> list[Tool[Any]]:
    tools: list[Tool[Any]] = []
    if transform is not None:
        tools += [
            TransformListModels(transform),
            TransformCompile(transform),
            TransformRun(transform, default_target),
            TransformTest(transform, default_target),
        ]
    if services.engines:
        _, engine = services.engine(None)
        dialect = engine.capabilities().sql_dialect
        if transform is not None or (
            services.catalog is not None and services.catalog.capabilities().supports_lineage
        ):
            tools.append(ImpactAnalysis(LineageService(transform, services.catalog, dialect)))
        tools += [DataSnapshot(services), DataDiffTool(services)]
    if knowledge is not None and len(knowledge):
        tools.append(KnowledgeRead(knowledge))
    return tools
