# Progresso

> O Claude Code atualiza este arquivo ao fim de cada tarefa.
> Marque `[x]` no que terminou, registre decisões e pendências.

## Estado atual

- Fase atual: **6 — ACP + extensão do VS Code**
- Última atualização: 2026-11-23

## Fases

- [x] Fase 1 — Fundação
- [x] Fase 2 — Core + LLM
- [x] Fase 3 — Dados locais
- [x] Fase 4 — Verificação e transformação
- [x] Fase 5 — Memória, orquestração e streaming (preset open source completo)
- [ ] Fase 6 — ACP + extensão do VS Code
- [ ] Fase 7 — Presets de nuvem (Azure → AWS → Google)
- [ ] Fase 8 — Distribuição e lançamento

## Decisões em aberto (do plano)

- [ ] Produto para outros profissionais ou ferramenta de uso próprio?
- [x] Licença: **Apache-2.0** (já definida no repositório `proscholdt/DengCode`)
- [x] Nome definitivo do produto: **DengCode** (pacotes `dengcode-*`, módulos
      `dengcode_*`, comando `dengcode`, pasta `~/.dengcode/`, entry points
      `dengcode.adapters`, extensões ACP `_dengcode/*`)
- [ ] Executável único: PyInstaller ou Nuitka?
- [x] TUI rica (Textual) já na fase 2 ou CLI simples até a fase 6? → **CLI simples (Rich) até a fase 6**
- [x] Orquestrador open source padrão: Airflow ou Dagster? → **Airflow** (API REST v1/v2)

## Decisões tomadas na implementação

| Fase | Decisão |
| --- | --- |
| 1 | Presets de stack vivem **só** em `packages/dengcode-engine/src/dengcode_engine/stacks/` (dados empacotados); `stacks/README.md` na raiz aponta para lá. Evita duas cópias divergentes. |
| 1 | Raiz do workspace uv é *virtual* (sem `[project]`); `make dev` = `uv sync --all-packages --all-extras`. |
| 1 | Fronteira do core verificada duas vezes: `import-linter` (contratos em `.importlinter`) e ruff `TID251` (`packages/dengcode-core/ruff.toml`). |
| 1 | Entry point aponta para a **classe** do adapter, que expõe `info: AdapterInfo` (nome + porta) e `from_config(cfg, ctx)`. O registro indexa por (porta, nome), então um pacote pode registrar várias portas com o mesmo nome (ex.: duckdb como QueryEngine e Catalog). |
| 1 | `pyarrow-stubs` como dependência de dev para o pyright strict no core. |
| 2 | Formato canônico ganhou `ProviderStatePart`/`ProviderState` para replay exato do turno do assistente (ADR 0006). |
| 2 | Adapter Anthropic: SDK `anthropic` 2.x (sobre `httpx2`), `messages.stream` beta; pensamento adaptativo, `drop_block` para blocos que não batem após compactação do core, `fallbacks: "default"` em recusas (desligável: `refusal_fallback: false`), esforço `high` (configurável: `effort`). Sem `temperature`. |
| 2 | Testes de LLM com respostas gravadas via `httpx2.MockTransport` (os SDKs atuais usam `httpx2`; `respx` não se aplica). Sem rede no CI. |
| 2 | Prompt de sistema congelado na sessão (`SessionMeta.system_prompt`): retomar não muda o prefixo. |
| 2 | Preset novo `local` (disco + Ollama, sem Docker) é a stack padrão da CLI; `--llm provedor[:modelo]` troca o LLM sem editar YAML (modelos padrão declarados por cada adapter). |
| 2 | Na herança, trocar o `provider` do `llm` recomeça a seção; `model`/`models` substituem um ao outro (documentado em `stacks.md`). |
| 2 | Tool call truncada por `max_tokens` nunca é executada (o loop devolve erro ao modelo). |
| 3 | `AdapterContext.peers`: adapters já construídos da stack; o catálogo DuckDB reaproveita a conexão do motor DuckDB (mesmo banco, inclusive `:memory:`). |
| 3 | SqlGuard só aceita tipos de instrução conhecidos; o resto é UNKNOWN (ex.: `SELEC x` o sqlglot parseia como `Alias`). `EXPLAIN ANALYZE <x>` é classificado pela instrução interna (executa de verdade). |
| 3 | PII mascarada em **toda** saída de ferramenta (inclusive `file_read` e shell), antes de ir ao LLM e às interfaces; telefones exigem formatação para não mascarar ids/valores. Efeito colateral aceito: o agente vê e-mails mascarados em arquivos de código. |
| 3 | `query_run` injeta `LIMIT 1000` em leituras exploratórias sem limite; `max_rows` (10 000) limita o que é trazido; resultado completo em Parquet em `~/.dengcode/workspaces/<ws>/cache/results/`. |
| 3 | Auditoria guarda o SQL já mascarado; triggers SQLite impedem UPDATE/DELETE. Bloqueios (política) e recusas (usuário) também são auditados (`Tool.on_blocked`). |
| 3 | DuckDB: `SET GLOBAL file_search_path` = diretório do projeto, para `read_csv('dados/x.csv')` não depender do cwd. |
| 3 | MinIO via `obstore` (Rust, async, wheels nos 3 SOs). As imagens oficiais `minio/minio` e `minio/mc` não estão mais disponíveis para pull; o compose usa `pgsty/minio` / `pgsty/mc` (troca por `DENGCODE_MINIO_IMAGE`/`DENGCODE_MC_IMAGE`). |
| 3 | Postgres via psycopg 3 assíncrono, uma conexão por operação. No Windows o psycopg não aceita o `ProactorEventLoop` (necessário ao `shell_run`): o adapter roda cada operação num `SelectorEventLoop` em thread auxiliar (`portable.py`), sem mudar a política global. |
| 3 | Preset `opensource` reduzido ao que existe na fase 3 (MinIO + DuckDB + Postgres); cresce nas fases 4 e 5. Preset `local` ganhou DuckDB em arquivo (`.dengcode/local.duckdb`) + catálogo DuckDB. |
| 4 | dbt como subprocesso (ADR 0008), não `dbtRunner`; DuckDB em arquivo abre conexão por operação para o dbt poder escrever no mesmo arquivo. |
| 4 | Lineage de colunas via `sqlglot.lineage` sobre o SQL compilado; `SELECT *` entre modelos expandido com `qualify` usando as colunas de saída já conhecidas; nomes normalizados para `schema.tabela`. |
| 4 | Data diff gera SQL em DuckDB e transpila para o dialeto do motor; toda instrução gerada passa pelo SqlGuard (precisa ser READ). Colunas PII sem min/max/top valores. |
| 4 | `data_snapshot` grava em `dengcode_tmp_<sessão>`; escrita só nesse schema é liberada em dev sem perguntar (nunca em prod/read_only). |
| 4 | Conhecimento embutido em um só lugar (`dengcode_engine/knowledge/`, como os presets); `knowledge/README.md` na raiz aponta para lá. Pacotes por ferramenta ficam em cada adapter (`AdapterInfo.knowledge_pack`). |
| 4 | Exemplo `examples/ecommerce/` é um projeto DengCode (stack `ecommerce` em `.dengcode/stacks/`) com projeto dbt bronze→silver→gold. |
| 5 | `MemoryStorePort` ganhou `list(scope, kinds, limit)` (além da especificação) para o resumo do workspace no prompt e para `dengcode memory list`. |
| 5 | Memória SQLite: uma conexão por store com lock, WAL; FTS5 (`unicode61 remove_diacritics`) + `sqlite-vec` (partição por workspace) + grafo; RRF k=60. Sem extensão de vetor ou sem embedder: só FTS5 com um aviso. Validador rejeita PII e corpo com cara de linhas de dados. |
| 5 | Embeddings locais com `fastembed` (ADR 0007), modelo multilíngue `paraphrase-multilingual-MiniLM-L12-v2`; também Ollama e OpenAI. Preset `local` fica sem embedding (só FTS); `opensource` usa `local`. |
| 5 | `memory_propose` vira `verified` quando o usuário aprova a chamada (o loop marca `approved_by_user` no `ToolContext`); sem aprovação (modo auto) fica `proposed` e se confirma com `dengcode memory confirm`. |
| 5 | Catálogo na memória só com metadados: colunas, linhas, formato e, nas estatísticas, frações de nulos e distintos — sem min/max. `catalog_describe` compara o `schema_hash` com a memória e marca `stale`. |
| 5 | `build_runtime` passou a ser assíncrono (o resumo da memória vai no prompt congelado da sessão). |
| 5 | Airflow 2 e 3 pela API REST (auto-detecção); `run_id` = `"<dag_id>/<dag_run_id>"`. Imagem `apache/airflow:slim-3.3.2` standalone. Kafka com `aiokafka`; `sample` não confirma offsets e nunca cria tópico. Trino pelo protocolo HTTP (`httpx2`) para cancelar de verdade; Iceberg REST direto pela especificação (sem `pyiceberg`). |
| 5 | Compose: arquivo principal (Postgres + MinIO) inclui `docker/oss/{trino,airflow,kafka}.yaml`; portas Trino 8080, Airflow 8081, Iceberg 8181, Kafka 9092. |
| 1 | Ruff `N818` desligado: nomes de erro seguem a especificação (`CostLimitExceeded`, `UnsupportedOperation`). |
| 1 | Adapter `local-fs` já implementa a `StoragePort` completa (CSV/Parquet/JSON via PyArrow); escrita desligada por padrão. |

## Registro

| Data | Fase/tarefa | O que foi feito | Pendências |
| --- | --- | --- | --- |
| 2026-11-23 | Fase 5 (ao vivo) | Com o Airflow real e o Ollama qwen2.5:7b: o agente listou execuções, leu o detalhe e os logs reais de `validar_pedidos`, achou a causa (12 nulos em `id_cliente` em 40 linhas) e propôs a correção. O `memory_propose` não terminou: na 1ª vez o embedding local baixa ~220 MB e a execução estourou o tempo limite do teste. Mitigação: `stack doctor` agora sonda o embedding (o download acontece ali, não no meio do chat). | Repetir a 2ª sessão ao vivo (citar o incidente) com o modelo já em cache. |
| 2026-11-23 | Fase 5 | Workspaces (`workspace new/list/use`), `init` (PROJETO.md com tabelas protegidas na política), MemoryStore SQLite + embeddings (local/Ollama/OpenAI), `catalog sync` e detecção de `stale`, ferramentas `memory_*`, `pipeline_*`, `stream_*`; OrchestratorPort + Airflow; StreamPort + Kafka; StreamComputePort + fake; Trino (QueryEngine + Catalog) e Iceberg REST; compose completo e preset `opensource`; pacotes de conhecimento airflow, kafka, trino, iceberg e fundamento neutro de streaming. Critérios: `test_fase5_e2e.py` (investigação de `carga_pedidos` com logs mascarados, incidente gravado e citado na 2ª sessão; dois workspaces sem vazamento; sync + stale); contratos passam para todos os adapters open source (testes de integração com Docker); `stack doctor opensource` 9/9 verde com `make up-oss`. | — |
| 2026-11-23 | Fase 4 | TransformPort + adapter `dbt_core` (contrato `TransformContract`, 11 testes com dbt real); lineage de colunas (testado em 6 dialetos) e `impact_analysis`; `DataDiff` (linhas a mais, duplicadas, nulos novos, alteradas); ferramentas `transform_*`, `data_snapshot`, `data_diff`, `knowledge_read`; fluxo "mudar modelo" no prompt; 9 fundamentos + pacotes duckdb/postgres/dbt; projeto dbt de exemplo. Critério: teste de ponta a ponta com dbt e DuckDB reais (`test_fase4_e2e.py`) — impacto lista o gold, edições aprovadas, `run`/`test` verdes, data diff mostra `canal_venda` nova, 300 → 300 linhas no silver e a mudança de grão no gold; o teste de unicidade antigo do gold falhou até ser ajustado ao novo grão (a verificação pegou o problema). | Rodar o cenário ao vivo com um LLM forte (o 7B local se perde em SQL do DuckDB). |
| 2026-11-23 | Fase 3 | Portas Storage/QueryEngine/Catalog + contratos e fakes; adapters duckdb, postgres (testcontainers), minio (testcontainers), local-fs completo; SqlGuard (82 casos em 6 dialetos), CostGuard, ResultStore, PiiMasker, AuditLog; ferramentas `query_run`, `query_explain`, `catalog_*`, `storage_*`; `stack validate`/`doctor`, `audit list`; card de aprovação de SQL; compose (Postgres + MinIO + bucket de exemplo); dados sintéticos em `examples/ecommerce/`. Critérios: testes de ponta a ponta (FakeLLM + DuckDB real) provam que nenhum e-mail/CPF chega ao LLM, que DDL/DML só roda com aprovação e que `DROP` em prod é negado sem perguntar; contratos passam para os 4 adapters (42 testes de integração com Docker); `stack doctor opensource` verde com `make up-oss`. | Medir RAM do compose (estimativa ~1 GB). |
| 2026-11-23 | Fase 2 | Formato canônico, LLMPort, retry/backoff/jitter + Retry-After, fallback de provedor; ferramentas de arquivo e shell; PermissionPolicy; checkpoints e `undo`; EventBus; ContextManager (truncamento, marcadores de conteúdo não confiável, compactação com o modelo `fast`); sessões JSONL; loop do agente com leituras em paralelo, orçamento e cancelamento; FakeLLM e `LLMContract`; adapters Anthropic, OpenAI (inclui Azure via `variant: azure` e `base_url`) e Ollama; carregamento de stacks (`extends`, `env://`, `.env`); CLI `chat`, `run --json`, `undo`, `sessions list`, `stack list/show/validate`. Verificado ao vivo com Ollama `qwen2.5:7b`: leu o projeto de exemplo, editou com aprovação, rodou `pytest` (exit 0); `dengcode undo` restaurou; recusa no chat interativo manteve o arquivo. | Anthropic e OpenAI testados só com respostas gravadas (sem chave de API nesta máquina): rodar o critério ao vivo quando houver chave. |
| 2026-11-23 | Fase 1 | Workspace uv (core, engine, cli, testkit, local-fs), ruff, pyright strict no core, import-linter, erros, registro por entry points, CLI `--version`/`adapters list`, Makefile portátil, Dockerfile multi-stage, compose vazio, CI 3 SO × 2 Pythons, higiene (gitignore, editorconfig, gitattributes, pre-commit). `make lint` e `make test` verdes; `import boto3` no core quebra o lint (ruff e import-linter). | CI verde a confirmar no GitHub após o push. |
