# DengCode

Assistente de código com IA para **engenharia de dados**, no estilo Claude Code /
Cursor / Codex. O agente enxerga **dados** — schemas, estatísticas, lineage,
custos e pipelines — e não só código. Roda localmente, com o LLM que você
escolher, sobre a stack que você já usa (Azure, AWS, Google ou open source).

- **Agnóstico de stack** — o núcleo fala em conceitos (storage, compute,
  catálogo, orquestração, streaming); cada ferramenta é um adapter.
- **Agnóstico de LLM** — formato canônico próprio; trocar de provedor é uma linha.
- **Seguro por padrão** — leitura livre; escrita, DDL e DML só com aprovação;
  PII mascarada antes de chegar ao LLM; auditoria de toda query.
- **Verificação com dados** — mudança em pipeline é provada com data diff.

> Estado: em desenvolvimento, fase por fase. Veja [`PROGRESSO.md`](PROGRESSO.md).

## Pré-requisitos

- Python 3.12+ e [uv](https://docs.astral.sh/uv/)
- `make`
- Docker (a partir da stack open source com serviços)
- Uma chave de LLM (Anthropic, OpenAI) **ou** [Ollama](https://ollama.com) local

### `make` no Windows

Qualquer uma das opções:

```
winget install ezwinports.make
choco install make
scoop install make
```

O Makefile não usa comandos exclusivos de Unix: roda no PowerShell, no cmd e no Git Bash.

## Primeiros passos (desenvolvimento)

```
git clone https://github.com/proscholdt/DengCode
cd DengCode
make dev          # instala o workspace (uv sync)
make lint         # ruff + pyright + import-linter
make test         # testes unitários e de contrato
uv run dengcode --version
uv run dengcode adapters list
```

## Usando

```
# 1) só o projeto + DuckDB + LLM local (Ollama), sem Docker
ollama pull qwen2.5:7b
uv run dengcode chat                       # stack "local" é o padrão

# trocar o LLM sem editar YAML (chave em .env: ANTHROPIC_API_KEY / OPENAI_API_KEY)
uv run dengcode chat --llm anthropic       # padrão: claude-opus-5-5 (rápido: claude-haiku-5-5)
uv run dengcode chat --llm openai:gpt-5

# 2) exemplo de e-commerce com dbt (bronze → silver → gold)
cd examples/ecommerce
uv run --project ../.. dengcode chat --stack ecommerce
#   "adicione a coluna canal_venda em silver_pedidos e propague até gold_vendas_diarias"

# 3) stack open source completa em Docker (MinIO, Postgres, Trino, Iceberg, Airflow, Kafka)
cp .env.example .env
make up-oss
uv run dengcode stack doctor opensource
uv run dengcode chat --stack opensource
#   "por que a DAG carga_pedidos falhou?"
```

Outros comandos: `dengcode run "<tarefa>" --json` (headless/CI), `undo`,
`init` (cria `.dengcode/PROJETO.md`), `workspace new|list|use`, `catalog sync`,
`memory list|confirm|forget`, `audit list --since 7d`, `sessions list`,
`stack list|show|validate|doctor`, `adapters list`.

### Como o DengCode protege os dados

- Leitura livre; escrita de arquivo, DDL/DML, disparo de pipeline e comandos fora
  da lista permitida pedem aprovação (com diff ou card de SQL). Em `prod`, escrita
  é negada. Tabelas protegidas (stack ou `PROJETO.md`) nunca são escritas.
- Todo SQL passa pelo SqlGuard (sqlglot): classificação, avisos (`DELETE` sem
  `WHERE`, `DROP`…), `LIMIT` em consultas exploratórias, custo estimado.
- PII (e-mail, CPF, CNPJ, telefone, cartão) é mascarada em toda saída de
  ferramenta antes de chegar ao LLM; a memória guarda só metadados.
- Toda query (executada, negada ou recusada) vai para a auditoria append-only.

## Estrutura

```
packages/
  dengcode-core/       domínio puro: agente, portas, política, SqlGuard (sem SDKs)
  dengcode-engine/     composição: stacks, registro de adapters, servidor ACP
  dengcode-cli/        CLI (Typer + Rich)
  dengcode-testkit/    suítes de contrato por porta + fakes
  adapters/            um pacote por ferramenta (descobertos por entry points)
docs/                  arquitetura, plano, fases, especificações e ADRs
docker/                Dockerfile do motor e compose da stack open source
```

Regras de dependência (verificadas no CI com `import-linter`):

```
dengcode-core     → stdlib + pydantic + sqlglot + pyarrow
dengcode-engine   → core (+ adapters via entry points)
dengcode-cli      → engine
adapters/*        → só dengcode_core (+ SDK da ferramenta)
dengcode-testkit  → core
```

## Documentação

- [Arquitetura](docs/ARQUITETURA.md) · [Plano](docs/PLANO.md) · [Fases](docs/fases/)
- [Especificações](docs/especificacoes/) · [Decisões (ADRs)](docs/adr/)
- [Regras para agentes de código](CLAUDE.md)

## Licença

[Apache-2.0](LICENSE)
