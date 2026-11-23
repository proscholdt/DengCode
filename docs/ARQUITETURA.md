# Arquitetura — DengCode — IA para Engenharia de Dados

Versão do plano: 2026-11-23

## 1. Visão e princípios

Um assistente de código com IA, no estilo Claude Code / Cursor / Codex,
focado 100% em engenharia de dados. O diferencial: o agente enxerga **dados**,
não só código — schemas, estatísticas, lineage, custos e pipelines. Ele roda
localmente; o profissional usa pela CLI ou pela extensão do VS Code.

**Para quem:** engenheiros de dados em qualquer stack (Azure, AWS, Google ou
open source) que querem escolher o LLM.

**Princípios inegociáveis**

1. **Agnóstico de stack.** O núcleo não conhece ferramenta pelo nome; fala em
   conceitos (storage, compute, orquestração, catálogo, streaming). Ferramentas
   entram como adapters.
2. **Agnóstico de LLM.** Formato canônico próprio de mensagens e ferramentas;
   trocar de provedor é uma linha de configuração.
3. **Controle total.** Loop de agente próprio, sem SDK de agente de terceiros;
   arquitetura hexagonal.
4. **Seguro por padrão.** Leitura livre; escrita, DDL e DML com aprovação e
   custo estimado antes. PII nunca vai crua ao LLM.
5. **Verificação orientada a dados.** Mudança em pipeline é validada comparando
   dados antes e depois.
6. **Portátil.** Windows, Linux e macOS; uv, ruff, Docker, Makefile e CI desde o
   primeiro commit.

## 2. Arquitetura geral

```
┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐
│  CLI / TUI   │   │ Extensão VS Code │   │  JetBrains · Zed │
└──────┬───────┘   └────────┬─────────┘   └────────┬─────────┘
       └───────── ACP · JSON-RPC sobre stdio ──────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│ dengcode-engine: o motor (Python)                              │
│  [Servidor ACP]  [Composição da stack]  [Conhecimento]        │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ dengcode-core: domínio puro, sem SDKs de nuvem ou de LLM │  │
│  │ [Loop do agente] [Permissões] [SqlGuard] [Verificador]  │  │
│  │ ─────────────────────────────────────────────────────── │  │
│  │ Portas: LLM · Storage · QueryEngine · Catalog ·         │  │
│  │         Orchestrator · Stream · Secrets · Memory        │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────┬───────────────────────────────────┘
                            │ implementam as portas
┌───────────────────────────▼───────────────────────────────────┐
│ Adapters: plugins, um pacote por ferramenta                   │
│  LLM: Anthropic · OpenAI · Bedrock · Vertex · Ollama          │
│  Storage: ADLS · S3 · GCS · MinIO · disco local               │
│  Compute: Databricks · Synapse · Athena · BigQuery · DuckDB…  │
│  Orquestração: ADF · Airflow (MWAA/Composer) · Dagster        │
│  Catálogo: Unity · Glue · Dataplex · Iceberg REST             │
│  Streaming: Event Hubs · Kinesis · Pub/Sub · Kafka            │
│  Transformação: dbt-core   Segredos/memória: Key Vault, SQLite│
└───────────────────────────────────────────────────────────────┘
```

As interfaces falam com o motor só pelo ACP. O core conhece apenas portas;
cada ferramenta ou provedor entra como adapter. Trocar Azure por AWS, ou um
LLM por outro, não muda uma linha do core.

## 3. Core do agente

O core é o domínio puro: não importa nenhum SDK de nuvem nem de LLM.

**Loop do agente**

1. Monta o contexto: prompt de sistema, índice de fundamentos, índice dos
   pacotes da stack ativa, `PROJETO.md` e memória recuperada.
2. Chama o `LLMPort` em streaming.
3. Cada chamada de ferramenta passa pela `PermissionPolicy`: libera, pede
   aprovação ou bloqueia.
4. Executa as liberadas (em paralelo quando independentes) e devolve
   resultados resumidos.
5. Repete até o modelo encerrar, o usuário interromper ou o orçamento
   (tokens, custo, passos) acabar.

**Componentes**

- `Session`: histórico, workspace, stack ativa, orçamento. Pausar, retomar, exportar.
- `ToolRegistry`: ferramentas genéricas (arquivos, shell, busca no código) +
  as registradas pelos adapters (`query.run`, `catalog.describe`,
  `pipeline.logs`…). Cada ferramenta tem schema JSON, nível de risco e um
  resumidor de saída.
- `PermissionPolicy`: regras por risco (leitura, escrita local, escrita remota,
  custo), por ambiente (dev/prod) e workspace. Modos: `ask`, `auto` (só dev),
  `read_only`.
- `Planner`: para tarefas grandes, gera um plano que o usuário aprova antes.
- `ContextManager`: orçamento de tokens pelas flags do modelo; compactação do
  histórico antigo; saídas grandes vão para Parquet e o LLM recebe só a prévia.
- `Verifier`: após mudar modelo/pipeline, roda testes e a comparação de dados
  (linhas, nulos, chaves duplicadas, distribuição).
- `EventBus`: todo evento (token, tool call, pedido de aprovação) é um evento
  tipado; o servidor ACP repassa às interfaces.
- Subagentes: opcionais e limitados, com contexto próprio.

## 4. Portas e adapters

| Porta | Responsabilidade | Azure | AWS | Google | Open source |
| --- | --- | --- | --- | --- | --- |
| `LLMPort` | chat em streaming com ferramentas | Azure OpenAI / Foundry | Bedrock | Vertex AI | Ollama |
| `EmbeddingPort` | vetores para a memória | Azure OpenAI | Bedrock | Vertex AI | modelo local |
| `StoragePort` | listar/ler arquivos, inferir schema | ADLS Gen2 / Blob | S3 | GCS | MinIO / disco local |
| `QueryEnginePort` | executar, explain/dry-run, custo, dialeto | Databricks, Synapse | Athena, EMR Spark | BigQuery, Dataproc | DuckDB, Trino, Spark |
| `CatalogPort` | tabelas, colunas, estatísticas, lineage | Unity Catalog | Glue Catalog | Dataplex | Iceberg REST |
| `OrchestratorPort` | pipelines, execuções, logs, disparo | ADF | Airflow (MWAA) | Airflow (Composer) | Airflow / Dagster |
| `TransformPort` (opcional) | compile, run, test, seleção | dbt | dbt | dbt | dbt-core |
| `StreamPort` | tópicos, partições, offsets, lag, amostras | Event Hubs | Kinesis / MSK | Pub/Sub | Kafka |
| `StreamComputePort` | status de jobs, atraso, checkpoints | Databricks Streaming, Stream Analytics | Managed Flink | Dataflow | Flink / Spark |
| `SecretsPort` | credenciais | Key Vault | Secrets Manager | Secret Manager | `.env` / Vault |
| `MemoryStorePort` | memória e busca híbrida | SQLite (padrão) / Postgres + pgvector | idem | idem | idem |

- **Capability flags**: cada adapter declara `supports_dry_run`,
  `supports_cost_estimate`, `supports_lineage`, `supports_transactions`,
  `sql_dialect`. Sem dry-run → EXPLAIN; sem estimativa → exige aprovação.
- **Ferramentas extras**: recursos exclusivos (clusters Databricks, partition
  projection Athena) viram ferramentas do próprio adapter.
- **Plugins**: cada adapter é um pacote (`dengcode-adapter-athena`) descoberto
  por entry points.
- **Testes de contrato**: cada porta tem uma suíte que todo adapter dela passa.

Contratos detalhados: `docs/especificacoes/portas.md`.

## 5. Stacks e presets

Uma stack é um YAML que liga cada porta a um adapter. Presets de fábrica:
`azure`, `aws`, `google`, `opensource` (em `stacks/`). Stacks próprias herdam
com `extends`. Sem segredos no YAML: perfis de credenciais reaproveitam
`az login`, `aws sso login`, `gcloud auth` ou o `SecretsPort`. Validação por
schema pydantic. Stacks híbridas são permitidas.

Comandos: `dengcode stack new --from azure`, `stack validate`, `stack use`,
`stack doctor`.

Detalhes: `docs/especificacoes/stacks.md`.

## 6. Camada LLM agnóstica

Formato canônico próprio (`Message`, `TextPart`, `ToolCallPart`,
`ToolResultPart`, `ImagePart`, `ToolSpec`, eventos de streaming). Cada
provedor traduz de/para ele. `ModelProfile` com flags (contexto, saída máxima,
tools paralelas, prompt caching, visão, preço). Papéis de modelo: `main`,
`fast`, `embedding`. Retry com backoff exponencial + jitter só em erros
temporários; fallback configurável para outro provedor.

MVP: Anthropic, OpenAI (inclui Azure OpenAI), Ollama. Depois: Bedrock, Vertex, Gemini.

Detalhes: `docs/especificacoes/llm-formato-canonico.md`.

## 7. Segurança, SQL e governança

Todo SQL passa pelo `SqlGuard` (sqlglot): parse e classificação → política →
proteção de custo (LIMIT, dry-run/EXPLAIN, teto) → execução com timeout →
resultado em Parquet com prévia mascarada → auditoria. PII mascarada por tags
do catálogo e detectores (e-mail, CPF, telefone, cartão). Shell com allowlist,
escrita só dentro do workspace com diff e checkpoint. Conteúdo vindo de dados,
logs e arquivos é não confiável e nunca eleva permissões.

Detalhes: `docs/especificacoes/sqlguard.md`.

## 8. Conhecimento

Markdown escrito no repositório do DengCode e **empacotado dentro do motor**:
quem instala (PyPI, executável ou extensão) já recebe tudo.

- **Fundamentos** (neutros): modelagem dimensional, Data Vault, SCD, medallion,
  formatos de tabela, idempotência, incrementais, backfill, CDC, streaming,
  qualidade, LGPD, performance e custo.
- **Pacotes por ferramenta**: cada adapter traz `knowledge/<ferramenta>.md`.
- Carregamento sob demanda: o índice vai no prompt; conteúdo via ferramenta
  `knowledge.read`.
- Origens extras: projeto (`<projeto>/.dengcode/knowledge/`, git do time) e
  pessoal (`~/.dengcode/knowledge/`). Conflito: projeto > pessoal > embutido.

## 9. Memória

Seis camadas, isoladas por workspace (escopo obrigatório em toda leitura e escrita):

| Camada | Conteúdo | Onde fica | Quem escreve |
| --- | --- | --- | --- |
| Trabalho | conversa, resultados, compactação | RAM + checkpoints | automático |
| Projeto | convenções, camadas, proibições | `<repo>/.dengcode/PROJETO.md` | humano; agente propõe |
| Ambiente de dados | cache de catálogo, estatísticas, lineage, glossário, problemas conhecidos | `~/.dengcode/workspaces/<ws>/memory.db` | automático + agente propõe |
| Episódica | incidentes, causas, soluções, decisões | `memory.db` ou `.dengcode/incidents/*.md` | agente resume, usuário confirma |
| Usuário | preferências | `~/.dengcode/preferences.md` | usuário |
| Auditoria | queries, custos, aprovações | `audit.db` (append-only) | automático; fora do prompt |

Regras: memória é pista, não verdade (fonte, data, hash de schema; conferir ao
vivo); só metadados, nunca valores de linha/PII; busca sob demanda híbrida
(FTS5 + sqlite-vec + grafo de lineage); sem servidor por padrão.

Detalhes: `docs/especificacoes/memoria.md`.

## 10. Interfaces e distribuição

O motor fala **ACP (Agent Client Protocol)**, JSON-RPC 2.0 sobre stdio. CLI,
extensão VS Code, JetBrains e Zed são clientes do mesmo motor. Recursos
próprios (aprovação de SQL com custo, prévia de dados, lineage) viajam como
métodos de extensão do protocolo.

- CLI/TUI (Typer + Textual): primeiro cliente; modo headless `--json` para CI.
- Extensão VS Code (TypeScript): chat, diff nativo, card de aprovação de SQL,
  grade de prévia, lineage, seletor de stack/workspace, CodeLens em SQL.
  Publicada no Marketplace e Open VSX; baixa o motor certo na 1ª execução.

| Forma | Canal | Público |
| --- | --- | --- |
| PyPI | `uv tool install "dengcode[azure]"` | quem tem Python |
| Executável único | GitHub Releases, winget, Homebrew | quem não quer Python |
| Docker | `docker run ghcr.io/<org>/dengcode` | servidor, VPS, CI |
| Extensão | Marketplace / Open VSX | VS Code e derivados |

Executáveis assinados; telemetria só opt-in.

Detalhes: `docs/especificacoes/acp.md`.

## 11. Estrutura do repositório

```text
dengcode/
├─ packages/
│  ├─ dengcode-core/        # domínio: agente, sessão, política, portas (sem SDKs)
│  │  └─ src/dengcode_core/{agent,ports,policy,context,memory,sql,events,tools}/
│  ├─ dengcode-engine/      # composição: carrega stack, registra adapters, servidor ACP
│  ├─ dengcode-cli/         # Typer + Textual
│  ├─ dengcode-testkit/     # suítes de contrato por porta + fakes
│  └─ adapters/
│     ├─ llm-anthropic/  llm-openai/  llm-ollama/ ...
│     ├─ duckdb/  postgres/  databricks/  synapse/  athena/  bigquery/ ...
│     ├─ local-fs/  adls/  s3/  gcs/  minio/
│     ├─ adf/  airflow/  dagster/
│     └─ kafka/  eventhubs/  kinesis/  pubsub/
├─ knowledge/               # fundamentos (pacotes por ferramenta ficam em cada adapter)
├─ stacks/                  # azure.yaml, aws.yaml, google.yaml, opensource.yaml
├─ vscode-extension/        # TypeScript (fase 6)
├─ docker/                  # compose da stack open source
├─ docs/
├─ Makefile
└─ .github/workflows/ci.yml
```

## 12. Riscos principais

| Risco | Mitigação |
| --- | --- |
| Mínimo denominador comum enfraquece as ferramentas | capability flags + ferramentas específicas por adapter |
| Ação destrutiva/cara em produção | SqlGuard, prod read-only, teto de custo, auditoria |
| Vazamento de dados para o LLM ou entre workspaces | mascaramento, memória só com metadados, escopo obrigatório, LLM local |
| Escopo grande | fases; open source primeiro; nuvem só na fase 7 |
| APIs de nuvem mudam | adapters isolados e versionados; contrato no CI noturno |
| Qualidade desigual entre LLMs | flags de modelo, validação de argumentos, suíte de avaliação |
| ACP evolui ou perde tração | protocolo isolado; EventBus interno independente |
| Custo de tokens | conhecimento/memória sob demanda, caching, modelo `fast` |
