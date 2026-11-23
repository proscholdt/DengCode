# Progresso

> O Claude Code atualiza este arquivo ao fim de cada tarefa.
> Marque `[x]` no que terminou, registre decisões e pendências.

## Estado atual

- Fase atual: **4 — Verificação e transformação**
- Última atualização: 2026-11-23

## Fases

- [x] Fase 1 — Fundação
- [x] Fase 2 — Core + LLM
- [x] Fase 3 — Dados locais
- [ ] Fase 4 — Verificação e transformação
- [ ] Fase 5 — Memória, orquestração e streaming (preset open source completo)
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
- [ ] Orquestrador open source padrão: Airflow ou Dagster?

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
| 1 | Ruff `N818` desligado: nomes de erro seguem a especificação (`CostLimitExceeded`, `UnsupportedOperation`). |
| 1 | Adapter `local-fs` já implementa a `StoragePort` completa (CSV/Parquet/JSON via PyArrow); escrita desligada por padrão. |

## Registro

| Data | Fase/tarefa | O que foi feito | Pendências |
| --- | --- | --- | --- |
| 2026-11-23 | Fase 3 | Portas Storage/QueryEngine/Catalog + contratos e fakes; adapters duckdb, postgres (testcontainers), minio (testcontainers), local-fs completo; SqlGuard (82 casos em 6 dialetos), CostGuard, ResultStore, PiiMasker, AuditLog; ferramentas `query_run`, `query_explain`, `catalog_*`, `storage_*`; `stack validate`/`doctor`, `audit list`; card de aprovação de SQL; compose (Postgres + MinIO + bucket de exemplo); dados sintéticos em `examples/ecommerce/`. Critérios: testes de ponta a ponta (FakeLLM + DuckDB real) provam que nenhum e-mail/CPF chega ao LLM, que DDL/DML só roda com aprovação e que `DROP` em prod é negado sem perguntar; contratos passam para os 4 adapters (42 testes de integração com Docker); `stack doctor opensource` verde com `make up-oss`. | Medir RAM do compose (estimativa ~1 GB). |
| 2026-11-23 | Fase 2 | Formato canônico, LLMPort, retry/backoff/jitter + Retry-After, fallback de provedor; ferramentas de arquivo e shell; PermissionPolicy; checkpoints e `undo`; EventBus; ContextManager (truncamento, marcadores de conteúdo não confiável, compactação com o modelo `fast`); sessões JSONL; loop do agente com leituras em paralelo, orçamento e cancelamento; FakeLLM e `LLMContract`; adapters Anthropic, OpenAI (inclui Azure via `variant: azure` e `base_url`) e Ollama; carregamento de stacks (`extends`, `env://`, `.env`); CLI `chat`, `run --json`, `undo`, `sessions list`, `stack list/show/validate`. Verificado ao vivo com Ollama `qwen2.5:7b`: leu o projeto de exemplo, editou com aprovação, rodou `pytest` (exit 0); `dengcode undo` restaurou; recusa no chat interativo manteve o arquivo. | Anthropic e OpenAI testados só com respostas gravadas (sem chave de API nesta máquina): rodar o critério ao vivo quando houver chave. |
| 2026-11-23 | Fase 1 | Workspace uv (core, engine, cli, testkit, local-fs), ruff, pyright strict no core, import-linter, erros, registro por entry points, CLI `--version`/`adapters list`, Makefile portátil, Dockerfile multi-stage, compose vazio, CI 3 SO × 2 Pythons, higiene (gitignore, editorconfig, gitattributes, pre-commit). `make lint` e `make test` verdes; `import boto3` no core quebra o lint (ruff e import-linter). | CI verde a confirmar no GitHub após o push. |
