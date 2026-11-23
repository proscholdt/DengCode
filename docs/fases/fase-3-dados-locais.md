# Fase 3 — Dados locais (preset open source, parte 1)

**Objetivo:** o agente passa a enxergar e consultar dados com segurança,
usando só componentes locais.

**Leia antes:** `docs/especificacoes/portas.md` (Storage, QueryEngine, Catalog),
`docs/especificacoes/sqlguard.md`, ADRs 0004 e 0005.

## Tarefas

1. **DTOs e portas** em `dengcode_core/ports/`: `StoragePort`, `QueryEnginePort`,
   `CatalogPort` e modelos (conforme especificação).
2. **Suítes de contrato** no testkit para as três portas + fakes.
3. **Adapters**:
   - `duckdb` — QueryEngine + Catalog (via `information_schema` e `SUMMARIZE`).
     Dialeto `duckdb`; `supports_explain=True`, `supports_dry_run=False`.
   - `postgres` — QueryEngine + Catalog (`psycopg` 3, async). Testes com testcontainers.
   - `local-fs` — completar StoragePort (`read_sample`, `infer_schema` com PyArrow
     para CSV/Parquet/JSON).
   - `minio` — StoragePort via API S3 (`aioboto3` ou `obstore`); testes com testcontainers.
4. **SqlGuard** (`dengcode_core/sql/`): análise com sqlglot, classificação,
   `dangerous`, injeção de LIMIT, multi-statement, fallback UNKNOWN.
   Testes extensos com casos por dialeto (duckdb, postgres, spark, tsql, trino, bigquery).
5. **CostGuard** — usa capabilities do engine; teto da stack; `metered`.
6. **ResultStore** — salva Parquet em cache do workspace; gera prévia + estatísticas.
7. **PiiMasker** — detectores (e-mail, CPF, CNPJ, telefone, cartão/Luhn) + tags
   do catálogo; máscara que preserva formato. Testes com dados sintéticos.
8. **AuditLog** — `audit.db` SQLite append-only; `dengcode audit list`.
9. **Ferramentas de dados** registradas pelo engine a partir das portas ativas:
   `query_run`, `query_explain`, `catalog_list_tables`, `catalog_describe`,
   `catalog_column_stats`, `storage_list`, `storage_sample`, `storage_schema`.
   Descrições neutras (sem nome de ferramenta).
10. **Stack `opensource`** carregável com DuckDB + local-fs (+ Postgres/MinIO opcionais);
    `dengcode stack validate` e `dengcode stack doctor`.
11. **Compose** `docker/compose.oss.yaml`: Postgres e MinIO (com bucket de exemplo);
    `make up-oss` / `make down-oss`.
12. **Dados de exemplo** em `examples/ecommerce/` (CSVs sintéticos: clientes com
    e-mail/CPF falsos, pedidos, itens) para demonstrações e testes de ponta a ponta.
13. **CLI** — card de aprovação de SQL (SQL formatado, kind, tabelas, custo,
    avisos) e tabela de prévia.

## Critério de pronto

- [ ] Pedido "crie um modelo silver de pedidos a partir dos CSVs em examples/ecommerce,
      deduplicando e tipando as colunas" funciona de ponta a ponta no DuckDB.
- [ ] Nenhuma escrita (DDL/DML) acontece sem aprovação; `DROP` em prod = DENY (teste).
- [ ] E-mails e CPFs nunca aparecem crus no contexto enviado ao LLM (teste que
      inspeciona as mensagens do `FakeLLM`).
- [ ] Contratos de Storage/QueryEngine/Catalog passam para duckdb, postgres,
      local-fs e minio.
- [ ] CI verde (testes com Docker podem rodar só no Linux).
