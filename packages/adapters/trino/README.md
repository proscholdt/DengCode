# dengcode-adapter-trino

QueryEngine + Catalog sobre **Trino**, falando direto o protocolo HTTP do
cliente (`/v1/statement`) com `httpx2` assíncrono.

Entry points (grupo `dengcode.adapters`): `trino_query_engine` e `trino_catalog`
(ambos com `info.name = "trino"`). Pacote de conhecimento:
`dengcode_adapter_trino:knowledge/trino.md`.

## Configuração

```yaml
compute:
  - adapter: trino
    host: localhost        # padrão localhost
    port: 8080             # padrão 8080
    http_scheme: http      # http | https
    user: dengcode         # padrão dengcode (vai em X-Trino-User)
    catalog: iceberg       # catálogo da sessão (opcional)
    schema: vendas         # schema da sessão (opcional; só vale com catalog)
    password: env://TRINO_SENHA   # opcional; autenticação básica (use https)
    timeout_s: 60          # tempo máximo de cada requisição HTTP (padrão 60)
catalog:
  adapter: trino           # sem chaves de conexão: reaproveita o motor acima
```

A senha nunca vai para log, `repr` nem mensagem de erro.

## Por que o protocolo HTTP e não o cliente `trino` (DBAPI)

O cliente oficial é síncrono (`requests`): cada consulta ocuparia uma thread e
cancelar a tarefa asyncio não interromperia a thread. O protocolo é pequeno e
estável — `POST /v1/statement`, `GET nextUri` até acabar, `DELETE nextUri` para
cancelar — e o `httpx2` já está no workspace. Resultado: nada de threads, sem
dependências novas e cancelamento real no servidor.

## Comportamento

- **Uma conexão HTTP por operação** (o `AsyncClient` abre e fecha junto), sem
  estado preso a um event loop.
- **Tempo limite**: propriedade de sessão `query_max_run_time = timeout_s` no
  servidor (`EXCEEDED_TIME_LIMIT` → `TransientError`) e, no cliente,
  `asyncio.timeout(timeout_s + 3 s)`, que cancela a consulta no servidor.
- **Cancelamento**: `cancel(query_id)` usa o `nextUri` da consulta em andamento
  (`DELETE`); para um id que esta instância não conhece, usa
  `DELETE /v1/query/{id}`. Cancelar a *tarefa* asyncio também cancela no servidor.
- **`max_rows`**: as páginas param de ser lidas assim que o limite é passado e o
  resto da consulta é cancelado; `total_rows=None` nesse caso.
- `query_id` é o id do Trino (ex.: `20260101_120000_00042_abcde`), o mesmo de
  `system.runtime.queries` e da interface web.
- `cost` do resultado = bytes lidos de verdade (`physicalInputBytes`).
- `explain` = `EXPLAIN <sql>` (não executa). `dry_run` → `UnsupportedOperation`.
- Uma instrução por chamada (o Trino não aceita várias separadas por `;`).
  DDL sem resultado → tabela vazia; `INSERT` devolve a coluna `rows`.
- Cabeçalho `X-Trino-Client-Capabilities: PARAMETRIC_DATETIME`: datas e horas
  chegam com a precisão real.

### Tipos (Trino → Arrow)

`tinyint/smallint/integer/bigint` → `int8/16/32/64`, `real/double` →
`float32/float64` (inclusive `NaN`/`Infinity`), `decimal(p,s)` →
`decimal128(p,s)` (`decimal256` acima de 38 dígitos), `boolean`, `varchar/char` →
`string`, `varbinary` → `binary`, `date` → `date32`, `time(p)` → `time64[us]`,
`timestamp(p)` → `timestamp[us]`, `timestamp(p) with time zone` →
`timestamp[us, UTC]` (fusos por nome via `zoneinfo`), `array(<escalar>)` →
`list<...>`. O resto (`map`, `row` em JSON; `json`, `uuid`, `ipaddress`,
intervalos, `time with time zone`) vira texto. Se a conversão de uma coluna
falhar, ela inteira cai para texto. Precisão acima de microssegundos é truncada.

### Erros

| Situação | Erro de domínio |
| --- | --- |
| `TABLE/SCHEMA/CATALOG/COLUMN/FUNCTION_NOT_FOUND` | `NotFoundError` |
| HTTP 401 | `AuthError` |
| `PERMISSION_DENIED`, HTTP 403 | `PermissionDeniedError` |
| `EXCEEDED_TIME_LIMIT`, cancelamento, falta de recursos, servidor iniciando, HTTP 502/503/504 (após novas tentativas), conexão recusada, timeout | `TransientError` |
| HTTP 429 | `RateLimitError` |
| `SYNTAX_ERROR` e demais | `AdapterError` |

### Catálogo

- `TableRef.catalog` é o catálogo do Trino; sem ele vale o `catalog` da config
  (sem nenhum dos dois → `ConfigError`). O mesmo para o schema.
- `information_schema` do catálogo para schemas (sem `information_schema`),
  tabelas e colunas (tipo, `is_nullable`, `comment`).
- Nomes: se a busca exata não acha nada, tenta em minúsculas (o Trino normaliza
  nomes na maioria dos conectores).
- `describe`: colunas, comentário da tabela (`system.metadata.table_comments`),
  `row_count` por `count(*)`, `table_format` pelo conector do catálogo
  (`iceberg`, `delta`, `hudi`, `hive`; os demais ficam sem formato) e `schema_hash`.
- `column_stats`: uma agregação só — `count`, `approx_distinct` (HyperLogLog,
  erro padrão ~2,3%; barato em tabela grande) e `min`/`max` como texto. Tipos
  sem ordem útil (`json`, `map`, `array`, `row`...) têm `min`/`max` nulos;
  `map/array/row` também sem distintos.
- `lineage` → `UnsupportedOperation` (o core calcula pelo SQL).

## Testes

```
uv run pytest packages/adapters/trino -m "not integration"   # rápidos, Trino de mentira
uv run pytest packages/adapters/trino -m integration         # trinodb/trino:484 (Docker)
```

A integração sobe um `trinodb/trino:484` com heap de 1 GB (limite de 1,8 GB no
container) e usa os catálogos de exemplo da imagem: escrita no `memory`, consulta
lenta no `tpch`. Sem Docker, os testes de integração são pulados.
