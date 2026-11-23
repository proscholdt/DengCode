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
