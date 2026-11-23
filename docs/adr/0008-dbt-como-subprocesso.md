# ADR 0008 — Adapter de transformação roda o dbt como subprocesso

- Status: aceito
- Data: 2026-11-23

## Contexto

A fase 4 (tarefa 1) previa o dbt-core chamado em processo via `dbtRunner`, com
subprocesso como alternativa. O `dbtRunner` mantém estado global (logging,
diretório de trabalho, adapters carregados), não é seguro para chamadas
concorrentes dentro de uma aplicação asyncio e não pode ser cancelado de forma
limpa. Além disso, o dbt-duckdb resolve caminhos relativos (banco em
`profiles.yml`, `read_csv('../x.csv')`) a partir do diretório corrente.

## Decisão

- O adapter `dbt_core` executa `dbt` como subprocesso, com `cwd` = diretório do
  projeto dbt, `--project-dir`/`--profiles-dir` explícitos, timeout e kill no
  cancelamento; invocações são serializadas por projeto.
- Resultados vêm de `target/manifest.json` e `target/run_results.json`.
- Para o DuckDB em arquivo, o adapter de consulta abre uma conexão por operação
  (o DuckDB só permite um processo escritor por arquivo), e aceita
  `file_search_path` para resolver caminhos relativos a partir da pasta do dbt.

## Consequências

- (+) Isolamento total, cancelável, igual em Windows/Linux/macOS.
- (+) Funciona com qualquer versão de dbt instalada no ambiente.
- (−) Cada comando paga a inicialização do dbt (alguns segundos); mitigado com
  cache do manifesto e do SQL compilado enquanto os arquivos não mudam.
