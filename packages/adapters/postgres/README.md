# dengcode-adapter-postgres

QueryEngine + Catalog sobre PostgreSQL, com **psycopg 3 assíncrono**.

Entry points (grupo `dengcode.adapters`): `postgres_query_engine` e `postgres_catalog`.

## Configuração

```yaml
compute:
  - adapter: postgres
    host: localhost
    port: 5432            # padrão da libpq
    dbname: vendas
    user: analista
    password: env://PG_SENHA
    connect_timeout_s: 10 # padrão 10
catalog:
  adapter: postgres       # sem chaves de conexão: reaproveita o motor acima
```

| Chave | Descrição |
| --- | --- |
| `dsn` | string de conexão já resolvida (`postgresql://...` ou `host=... dbname=...`) |
| `host`, `port`, `dbname`, `user`, `password` | sobrescrevem o que vier no `dsn` |
| `connect_timeout_s` | tempo máximo para conectar (padrão 10) |

Chaves ausentes seguem os padrões da libpq (variáveis `PGHOST`, `PGPASSWORD`...).
Se nada definir `application_name`, a sessão aparece como `dengcode` no
`pg_stat_activity`. O DSN e a senha nunca vão para log nem para mensagens de
erro (o `repr` do motor também não os mostra).

## Comportamento

- **Uma conexão por execução**, em autocommit, fechada no fim. Sem estado
  compartilhado entre consultas; `cancel(query_id)` cancela no servidor
  (`cancel_safe`).
- **Tempo limite**: `statement_timeout` da sessão = `timeout_s`; no cliente,
  `asyncio.timeout(timeout_s + 5 s)` como rede de segurança. Estouro →
  `TransientError`.
- **`max_rows`**: só `max_rows` linhas viram `pyarrow.Table`; se houver mais,
  `total_rows=None`. O cursor é do lado do cliente (a libpq recebe o resultado
  inteiro), então consultas exploratórias dependem do `LIMIT` que o SqlGuard
  injeta.
- Várias instruções no mesmo texto: o resultado devolvido é o da última.
  DDL/DML sem resultado → tabela vazia.
- `explain` = `EXPLAIN <sql>` (sem `ANALYZE`, não executa). `dry_run` não existe
  (`UnsupportedOperation`).

### Tipos (PostgreSQL → Arrow)

`int2/int4/int8` → `int16/int32/int64`, `float4/float8` → `float32/float64`,
`numeric(p,s)` → `decimal128(p,s)` (sem precisão declarada: `decimal128(38, escala
dos valores)`), `bool`, `bytea` → `binary`, `date` → `date32`,
`time` → `time64[us]`, `timestamp` → `timestamp[us]`, `timestamptz` →
`timestamp[us, UTC]`, `interval` → `duration[us]`, arrays de tipos simples →
`list<...>`. O resto (json/jsonb, uuid, inet, timetz, enums...) vira texto; se a
conversão falhar (ex.: `numeric` com `NaN`), a coluna cai para texto.

### Erros

| Situação | Erro de domínio |
| --- | --- |
| tabela/coluna/schema/banco inexistente | `NotFoundError` |
| senha ou autorização inválida | `AuthError` |
| sem privilégio (`42501`) | `PermissionDeniedError` |
| conexão, serialização, deadlock, `statement_timeout`/cancelamento | `TransientError` |
| erro de sintaxe e demais | `AdapterError` |

Nenhuma exceção do psycopg atravessa a porta.

### Catálogo

- `information_schema` para schemas, tabelas e colunas (sem `pg_catalog`,
  `information_schema`, `pg_toast*`, `pg_temp_*`); o tipo da coluna vem de
  `format_type` (ex.: `character varying(50)`, `numeric(10,2)`, `integer[]`).
- `TableRef.catalog` é o banco; só o banco da conexão é visível. Sem `schema`,
  a tabela é resolvida pelo `search_path`.
- `describe`: colunas, comentário da tabela/colunas, `row_count` por
  `count(*)` e `schema_hash`.
- `column_stats`: fração de nulos, distintos, mínimo e máximo por agregações SQL
  (não depende de `ANALYZE`/`pg_stats`). Tipos sem ordem útil (json, arrays,
  geométricos...) têm `min`/`max` nulos.
- `lineage` → `UnsupportedOperation` (o core calcula pelo SQL).
- Views materializadas não aparecem (não estão no `information_schema`).

## Windows: event loop

O psycopg assíncrono (verificado na 3.3.9; a checagem existe desde a 3.0) **não funciona com o `ProactorEventLoop`**, que é o
padrão do asyncio no Windows: `connect` falha na hora. O adapter não muda a
política global de event loop; quem roda o loop escolhe um `SelectorEventLoop`:

```python
import asyncio
import sys

loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
asyncio.run(main(), loop_factory=loop_factory)  # Python 3.12+
```

Se o loop errado for usado, o erro sai como `AdapterError` com essa instrução.
Nos testes deste pacote, o `conftest.py` faz o mesmo pelo hook
`pytest_asyncio_loop_factories` do pytest-asyncio.

## Testes

```
uv run pytest packages/adapters/postgres -m "not integration"   # rápidos, sem Docker
uv run pytest packages/adapters/postgres -m integration         # PostgresContainer (Docker)
```

Sem Docker disponível, os testes de integração são pulados.
