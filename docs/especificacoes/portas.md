# Especificação — Portas

Todas as portas ficam em `dengcode_core.ports` como `typing.Protocol`
assíncronos. Os modelos de dados (DTOs) são `pydantic.BaseModel` imutáveis
(`model_config = ConfigDict(frozen=True)`) em `dengcode_core.ports.models`.

Os nomes e assinaturas abaixo são o **contrato inicial**. O Claude Code pode
ajustá-los durante a implementação se encontrar um problema real, desde que
registre um ADR.

## Regras gerais

- Toda porta tem `capabilities() -> <X>Capabilities`.
- Todo adapter tem `name: str` (ex.: `"duckdb"`) e é construído a partir do
  bloco de config da stack (`from_config(cfg: dict, ctx: AdapterContext)`).
- `AdapterContext` traz: workspace, ambiente (dev/prod), `SecretsPort`,
  logger, diretório de cache.
- Erros: adapters convertem exceções do SDK em erros de
  `dengcode_core.errors` (`AdapterError`, `AuthError`, `NotFoundError`,
  `RateLimitError`, `TransientError`, `PermissionDeniedError`,
  `CostLimitExceeded`, `UnsupportedOperation`).
- Operações potencialmente longas aceitam `timeout_s` e são canceláveis
  (respeitam `asyncio.CancelledError`).
- Resultados tabulares são sempre `pyarrow.Table` (ou caminho para Parquet
  quando grandes).
- Cada adapter pode registrar **ferramentas extras** via
  `extra_tools() -> list[ToolSpecWithHandler]`.

## Registro de adapters

```python
# pyproject.toml do pacote do adapter
[project.entry-points."dengcode.adapters"]
duckdb = "dengcode_adapter_duckdb:DuckDBQueryEngine"
```

```python
class AdapterInfo(BaseModel):
    name: str  # "duckdb"
    port: PortKind  # Enum: LLM, EMBEDDING, STORAGE, QUERY_ENGINE, CATALOG,
    #       ORCHESTRATOR, TRANSFORM, STREAM, STREAM_COMPUTE,
    #       SECRETS, MEMORY_STORE
    version: str
    knowledge_pack: str | None  # caminho do markdown empacotado
```

## LLMPort

Ver `llm-formato-canonico.md`.

```python
class LLMPort(Protocol):
    name: str

    def profile(self, model: str) -> ModelProfile: ...
    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]: ...
    async def count_tokens(self, request: ChatRequest) -> int: ...  # estimativa permitida
```

## EmbeddingPort

```python
class EmbeddingPort(Protocol):
    name: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

## StoragePort

```python
class StorageCapabilities(BaseModel):
    supports_write: bool
    supports_list_recursive: bool
    table_formats: set[str]  # {"parquet","csv","json","delta","iceberg"}


class ObjectInfo(BaseModel):
    uri: str  # ex.: "s3://b/k", "file:///...", "abfss://..."
    size_bytes: int | None
    modified_at: datetime | None
    is_dir: bool


class StoragePort(Protocol):
    name: str

    def capabilities(self) -> StorageCapabilities: ...
    async def list(
        self, uri: str, *, recursive: bool = False, limit: int = 1000
    ) -> list[ObjectInfo]: ...
    async def read_sample(
        self, uri: str, *, rows: int = 100, fmt: str | None = None
    ) -> pa.Table: ...
    async def infer_schema(self, uri: str, *, fmt: str | None = None) -> pa.Schema: ...
    async def read_bytes(self, uri: str, *, max_bytes: int = 1_000_000) -> bytes: ...
    async def write_bytes(self, uri: str, data: bytes) -> None: ...  # risco: escrita remota
```

## QueryEnginePort

```python
class QueryEngineCapabilities(BaseModel):
    sql_dialect: (
        str  # nome de dialeto do sqlglot: "duckdb","spark","tsql","trino","bigquery","postgres"
    )
    supports_dry_run: bool
    supports_explain: bool
    supports_cost_estimate: bool
    supports_transactions: bool
    supports_cancel: bool


class CostEstimate(BaseModel):
    bytes_scanned: int | None = None
    credits: float | None = None
    usd: float | None = None
    confidence: Literal["exact", "estimate", "unknown"]


class QueryResult(BaseModel, arbitrary_types_allowed=True):
    query_id: str | None
    columns: list[ColumnInfo]
    preview: pa.Table  # até N linhas
    total_rows: int | None
    result_path: Path | None  # Parquet local com o resultado completo (limitado)
    elapsed_ms: int
    cost: CostEstimate | None


class QueryEnginePort(Protocol):
    name: str

    def capabilities(self) -> QueryEngineCapabilities: ...
    async def execute(
        self, sql: str, *, max_rows: int = 10_000, timeout_s: int = 300
    ) -> QueryResult: ...
    async def explain(self, sql: str) -> str: ...
    async def dry_run(self, sql: str) -> CostEstimate: ...  # UnsupportedOperation se não suportar
    async def cancel(self, query_id: str) -> None: ...
```

> O `QueryEnginePort` NUNCA é chamado diretamente pelo loop: toda execução
> passa pelo `SqlGuard` (`sqlguard.md`).

## CatalogPort

```python
class CatalogCapabilities(BaseModel):
    supports_lineage: bool
    supports_column_stats: bool
    supports_tags: bool  # ex.: tags de PII


class TableRef(BaseModel):
    catalog: str | None
    schema_: str | None = Field(alias="schema")
    name: str


class ColumnInfo(BaseModel):
    name: str
    data_type: str
    nullable: bool | None = None
    description: str | None = None
    tags: set[str] = set()  # ex.: {"pii","email"}


class TableInfo(BaseModel):
    ref: TableRef
    columns: list[ColumnInfo]
    table_format: str | None
    location: str | None
    row_count: int | None
    description: str | None
    schema_hash: str  # hash estável das colunas+tipos


class ColumnStats(BaseModel):
    column: str
    null_fraction: float | None
    distinct_count: int | None
    min: str | None
    max: str | None


class LineageEdge(BaseModel):
    source: str  # "cat.schema.tabela.coluna"
    target: str
    via: str | None  # job/modelo que gera


class CatalogPort(Protocol):
    name: str

    def capabilities(self) -> CatalogCapabilities: ...
    async def list_schemas(self, catalog: str | None = None) -> list[str]: ...
    async def list_tables(self, schema: str, *, catalog: str | None = None) -> list[TableRef]: ...
    async def describe(self, ref: TableRef) -> TableInfo: ...
    async def column_stats(
        self, ref: TableRef, columns: list[str] | None = None
    ) -> list[ColumnStats]: ...
    async def lineage(
        self, ref: TableRef, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]: ...
```

Quando o catálogo não suportar lineage, o core calcula lineage pelo SQL
(sqlglot) dos modelos conhecidos.

## OrchestratorPort

```python
class OrchestratorCapabilities(BaseModel):
    supports_trigger: bool
    supports_code_definitions: bool  # DAG em código (Airflow) vs JSON (ADF)
    supports_backfill: bool


class PipelineInfo(BaseModel):
    id: str
    name: str
    schedule: str | None
    paused: bool | None
    definition_path: str | None  # arquivo no repo, quando houver


class RunInfo(BaseModel):
    run_id: str
    pipeline_id: str
    status: Literal["queued", "running", "success", "failed", "cancelled", "unknown"]
    started_at: datetime | None
    ended_at: datetime | None
    failed_tasks: list[str] = []


class OrchestratorPort(Protocol):
    name: str

    def capabilities(self) -> OrchestratorCapabilities: ...
    async def list_pipelines(self) -> list[PipelineInfo]: ...
    async def list_runs(self, pipeline_id: str, *, limit: int = 20) -> list[RunInfo]: ...
    async def get_run(self, run_id: str) -> RunInfo: ...
    async def task_logs(self, run_id: str, task_id: str, *, tail_lines: int = 500) -> str: ...
    async def trigger(
        self, pipeline_id: str, params: dict[str, Any] | None = None
    ) -> RunInfo: ...  # risco: escrita remota
```

## TransformPort (opcional)

```python
class TransformPort(Protocol):
    name: str

    async def list_models(self, selector: str | None = None) -> list[ModelInfo]: ...
    async def compile(self, model: str) -> str: ...  # SQL compilado
    async def run(self, selector: str, *, target: str) -> TransformRunResult: ...  # risco
    async def test(self, selector: str, *, target: str) -> TransformRunResult: ...
    async def manifest_lineage(self) -> list[LineageEdge]: ...
```

## StreamPort

```python
class StreamCapabilities(BaseModel):
    supports_consumer_lag: bool
    supports_schema_registry: bool


class TopicInfo(BaseModel):
    name: str
    partitions: int
    retention_hours: int | None


class StreamPort(Protocol):
    name: str

    def capabilities(self) -> StreamCapabilities: ...
    async def list_topics(self) -> list[TopicInfo]: ...
    async def sample(
        self, topic: str, *, max_messages: int = 20, timeout_s: int = 10
    ) -> list[dict[str, Any]]: ...
    async def consumer_lag(self, topic: str, group: str) -> dict[int, int]: ...  # partição -> lag
    async def schema(self, topic: str) -> dict[str, Any] | None: ...
```

`sample` é leitura, mas o conteúdo passa pelo mascaramento de PII antes de ir
ao LLM.

## StreamComputePort

```python
class StreamJobInfo(BaseModel):
    id: str
    name: str
    status: Literal["running", "failed", "stopped", "unknown"]
    lag_seconds: float | None
    last_checkpoint_at: datetime | None


class StreamComputePort(Protocol):
    name: str

    async def list_jobs(self) -> list[StreamJobInfo]: ...
    async def job_status(self, job_id: str) -> StreamJobInfo: ...
    async def job_logs(self, job_id: str, *, tail_lines: int = 500) -> str: ...
```

## SecretsPort

```python
class SecretsPort(Protocol):
    name: str

    async def get(self, key: str) -> str: ...  # NUNCA logar o valor
```

## MemoryStorePort

Ver `memoria.md`.

## Testes de contrato

`dengcode-testkit` expõe, para cada porta, uma classe base de testes:

```python
class QueryEngineContract:
    @pytest.fixture
    def engine(self) -> QueryEnginePort:
        raise NotImplementedError

    async def test_execute_select_returns_preview(self, engine): ...
    async def test_capabilities_are_consistent(
        self, engine
    ): ...  # ex.: dry_run lança UnsupportedOperation se flag False
    async def test_timeout_is_respected(self, engine): ...

    ...
```

Cada adapter herda a classe e fornece a fixture. Fakes em memória
(`FakeQueryEngine`, `FakeCatalog`, `FakeLLM` com respostas roteirizadas)
vivem no testkit e servem aos testes do core.
