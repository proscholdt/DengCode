# CLAUDE.md — DengCode — IA para Engenharia de Dados

Você (Claude Code) vai implementar este projeto do zero, fase por fase.
Este arquivo é a regra do projeto. Leia-o inteiro antes de qualquer tarefa.

## O que é o projeto

Um assistente de código com IA (no estilo Claude Code / Cursor / Codex)
focado 100% em engenharia de dados. O agente enxerga **dados** — schemas,
estatísticas, lineage, custos, pipelines — e não só código.

Princípios inegociáveis:

1. **Agnóstico de stack**: o core nunca cita uma ferramenta pelo nome
   (nada de "databricks", "athena", "adf" dentro de `dengcode-core`).
2. **Agnóstico de LLM**: formato canônico próprio; provedores são adapters.
3. **Controle total**: loop de agente próprio. NÃO usar SDKs de agente de
   terceiros (Claude Agent SDK, LangChain, LlamaIndex, CrewAI etc.) nem LiteLLM
   no caminho principal. SDKs HTTP oficiais de cada provedor podem ser usados
   *dentro* dos adapters.
4. **Seguro por padrão**: leitura livre; escrita/DDL/DML com aprovação;
   PII nunca crua para o LLM.
5. **Verificação orientada a dados**: mudança em pipeline é validada com
   comparação de dados antes/depois.
6. **Portátil**: Windows, Linux e macOS. Nada de caminho fixo com `/`,
   use `pathlib`. Nada de comando shell exclusivo de Linux no Makefile sem
   alternativa.

## Documentos de referência (leia conforme a tarefa)

| Documento | Quando ler |
| --- | --- |
| `docs/ARQUITETURA.md` | sempre, antes de qualquer fase |
| `docs/PLANO.md` | visão das fases |
| `docs/fases/fase-N-*.md` | a fase que você está implementando |
| `docs/especificacoes/portas.md` | ao criar ou alterar portas/adapters |
| `docs/especificacoes/llm-formato-canonico.md` | ao mexer em LLM |
| `docs/especificacoes/sqlguard.md` | ao mexer em SQL/segurança |
| `docs/especificacoes/memoria.md` | ao mexer em memória |
| `docs/especificacoes/stacks.md` | ao mexer em stacks/config |
| `docs/especificacoes/acp.md` | fase 6 |
| `docs/adr/` | decisões já tomadas — não reabra sem motivo |
| `PROGRESSO.md` | estado atual; atualize ao fim de cada tarefa |

## Arquitetura (hexagonal) — regras de dependência

```
dengcode-core      → depende só da stdlib + pydantic + sqlglot + pyarrow
dengcode-engine    → depende de core (+ adapters via entry points, nunca import direto)
dengcode-cli       → depende de engine
adapters/*        → dependem SOMENTE de dengcode_core.ports (e do SDK da ferramenta)
dengcode-testkit   → depende de core
```

- `dengcode_core` NUNCA importa `dengcode_engine`, `dengcode_cli` ou adapters.
- Adapters NUNCA importam outros adapters.
- Isso é verificado no CI com `import-linter` (`.importlinter`). Se quebrar, corrija o design, não a regra.
- Adapters são descobertos por **entry points** (`[project.entry-points."dengcode.adapters"]`).

## Stack técnica obrigatória

- Python 3.12+ · **uv** (workspace) · **ruff** (lint + format) · **pyright** strict no core
- pydantic v2 · asyncio (todas as portas são `async`) · sqlglot · DuckDB + PyArrow
- pytest + pytest-asyncio + testcontainers · import-linter
- Typer + Rich (CLI); Textual só quando a fase pedir
- Docker + docker compose · **Makefile** · GitHub Actions (`.github/workflows/ci.yml`)
- Extensão VS Code em TypeScript (fase 6)

## Convenções de código

- Código e identificadores em **inglês**; docs, mensagens ao usuário e
  comentários explicativos em **português**.
- Tipagem completa. `from __future__ import annotations` em todo módulo.
- Erros de domínio em `dengcode_core/errors.py` (hierarquia própria, nunca
  exceções genéricas cruas atravessando portas).
- Sem `print` fora da CLI; use `logging` estruturado.
- Funções pequenas, sem efeitos colaterais escondidos. Injeção de dependência
  por construtor — nada de singletons globais.
- Segredos nunca em código, YAML ou log.

## Comandos (Makefile)

```
make dev        # uv sync com todos os extras de dev
make lint       # ruff check + ruff format --check + pyright + lint-imports
make fmt        # ruff format + ruff check --fix
make test       # pytest (unit + contrato com fakes)
make test-int   # testes de integração (precisa de Docker)
make up-oss     # sobe a stack open source (docker compose)
make down-oss
```

## Como trabalhar em cada fase

1. Leia `PROGRESSO.md` e a fase atual em `docs/fases/`.
2. Planeje: liste os arquivos que vai criar/alterar antes de escrever código.
3. Implemente em fatias pequenas; cada fatia com testes.
4. Rode `make lint` e `make test` até passarem.
5. Verifique o **critério de pronto** da fase literalmente.
6. Atualize `PROGRESSO.md` (marque tarefas, registre decisões e pendências).
7. Se tomar uma decisão de arquitetura nova, crie um ADR em `docs/adr/`.
8. Faça commits pequenos, em português, no formato `tipo(escopo): descrição`
   (ex.: `feat(core): loop do agente com tool calls`).

## Definição de pronto (vale para toda tarefa)

- [ ] `make lint` e `make test` passam
- [ ] testes cobrindo o caminho feliz e os erros principais
- [ ] nenhuma regra de dependência violada
- [ ] funciona em Windows e Linux (CI em matriz)
- [ ] `PROGRESSO.md` atualizado

## O que NÃO fazer

- Não pule fases nem adiante adapters de nuvem antes da fase 7.
- Não crie abstrações "para o futuro" que a fase não pede.
- Não coloque nome de ferramenta/provedor dentro do core.
- Não execute SQL de escrita em testes contra nada além de containers/DuckDB locais.
- Não altere os princípios acima; se algo parecer impossível, pare e pergunte.
