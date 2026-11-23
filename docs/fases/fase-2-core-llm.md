# Fase 2 — Core do agente + LLM

**Objetivo:** um agente de código funcional (ainda sem dados) que conversa
por qualquer um de três provedores, usa ferramentas de arquivo e shell e pede
aprovação para mudanças.

**Leia antes:** `docs/especificacoes/llm-formato-canonico.md`,
`docs/especificacoes/sqlguard.md` (seção Arquivos e shell), ADR 0002.

## Tarefas

1. **Formato canônico** (`dengcode_core/llm/`): `Message`, parts, `ToolSpec`,
   `ChatRequest`, eventos de streaming, `ModelProfile`, `LLMPort`.
2. **FakeLLM** no testkit: recebe um roteiro de respostas (texto e tool calls)
   e emite os eventos de streaming. Base de todos os testes do loop.
3. **Adapters de LLM** (um pacote cada):
   - `llm-anthropic` (SDK oficial `anthropic`)
   - `llm-openai` (SDK oficial `openai`; suporta `base_url` e Azure OpenAI)
   - `llm-ollama` (HTTP local; formato de tools compatível)
   - Cada um: tradução de mensagens, streaming de tool calls, `model_profiles.yaml`,
     retry/backoff/jitter (só erros temporários, respeitando `Retry-After`).
   - Testes com respostas HTTP gravadas (ex.: `respx`/`vcrpy`), sem chamar a rede no CI.
   - Suíte de contrato `LLMContract` no testkit.
4. **Ferramentas** (`dengcode_core/tools/`): `ToolRegistry`, `Tool`
   (spec + handler + `risk: Risk` + resumidor). Ferramentas genéricas:
   `file_read`, `file_list`, `file_search` (ripgrep se existir, senão Python),
   `file_edit` (substituição exata), `file_write`, `shell_run`.
5. **PermissionPolicy** — decide ALLOW/ASK/DENY por risco, modo (`ask`, `auto`,
   `read_only`) e ambiente. Escrita fora do workspace = DENY. Shell com allowlist.
6. **Checkpoints** — antes de cada escrita, copia o arquivo para
   `.dengcode/checkpoints/<sessão>/`; `dengcode undo` restaura o último.
7. **EventBus** — eventos tipados (`AssistantText`, `ToolCallStarted`,
   `ToolCallFinished`, `ApprovalRequested`, `ApprovalResolved`, `UsageUpdated`,
   `ProviderFallback`, `SessionEnded`).
8. **Loop do agente** (`dengcode_core/agent/loop.py`): conforme `ARQUITETURA.md`
   §3; execução paralela de ferramentas independentes de leitura; orçamento
   (máx. passos, tokens, US$); cancelamento.
9. **ContextManager** — contagem de tokens (estimativa aceitável), truncamento
   de saídas de ferramenta (cabeça + cauda + "N linhas omitidas"), compactação
   ao atingir ~75% da janela usando o modelo `fast`.
10. **Session** — criar, salvar (JSONL em `~/.dengcode/workspaces/<ws>/sessions/`),
    retomar, listar.
11. **Carregamento de stack** (`dengcode_engine/config/`): schema pydantic de
    `docs/especificacoes/stacks.md`, `extends`, resolução de credenciais
    `env://` e `.env`. Por enquanto só a parte `llm` precisa funcionar.
12. **CLI interativa** — `dengcode chat` (Rich): streaming de texto, painel de
    tool calls, diff colorido com prompt de aprovação (s/n/sempre nesta sessão),
    `/undo`, `/stack`, `/cost`, Ctrl+C cancela o turno. `dengcode run "<tarefa>" --json`
    para modo headless.
13. **Prompt de sistema** — em arquivo (`dengcode_core/prompts/system.md`),
    com variáveis (stack ativa, workspace, regras). Sem nomes de ferramentas de dados.

## Critério de pronto

- [ ] Com cada um dos 3 provedores (configurados por stack), o agente:
      lê um projeto Python de exemplo, propõe uma alteração, mostra o diff, aplica
      após aprovação e roda `pytest` via `shell_run`.
- [ ] `/undo` restaura o arquivo.
- [ ] Testes do loop 100% com `FakeLLM` (sem rede).
- [ ] Retry testado: 429 → sucesso na 3ª tentativa; 401 → falha imediata.
- [ ] `make lint` / `make test` verdes no CI.
