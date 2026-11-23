# Fase 6 — ACP + extensão do VS Code

**Objetivo:** usar o DengCode de dentro do VS Code (e de IDEs compatíveis com ACP).

**Leia antes:** `docs/especificacoes/acp.md`, ADR 0003. Leia a especificação
atual em https://agentclientprotocol.com e registre a versão adotada num ADR.

## Tarefas

1. **AcpServer** (`dengcode_engine/acp/`): `dengcode acp` inicia o servidor em
   stdio; mapeia EventBus ↔ ACP conforme a especificação; sessões, prompts,
   tool calls, diffs, pedidos de permissão, planos, cancelamento.
2. **Extensões `_dengcode/*`**: sqlApproval, dataPreview, lineage, stack, workspace.
3. **Testes de protocolo** — cliente ACP de teste (Python) dirigindo o motor
   com `FakeLLM`; testes de ponta a ponta do fluxo da fase 4 via ACP.
4. **CLI como cliente** — flag `--acp` usando o subprocesso (garante paridade).
5. **Extensão VS Code** (`vscode-extension/`, TypeScript, esbuild):
   - gerencia o motor: localiza `dengcode` no PATH, senão baixa o binário da
     release correspondente ao SO/arquitetura para o storage da extensão;
     verifica versão compatível;
   - webview de chat (streaming, tool calls recolhíveis);
   - diffs com a API nativa de diff do VS Code (aceitar/rejeitar);
   - card de aprovação de SQL (`_dengcode/sqlApproval`);
   - grade de prévia de dados (`_dengcode/dataPreview`);
   - visão de lineage (grafo simples em webview);
   - barra de status: stack, ambiente, workspace (clique troca);
   - CodeLens em `.sql`: "Executar com DengCode" e "Explicar";
   - configurações: caminho do motor, stack padrão, modo de permissão.
6. **Testes da extensão** — `@vscode/test-electron` com motor fake.
7. **Empacotamento** — `vsce package` gera `.vsix` no CI (publicação só na fase 8).
8. **JetBrains/Zed** — documentar a configuração do agente externo e testar
   manualmente (registrar resultado no PROGRESSO).

## Critério de pronto

- [ ] O fluxo da fase 4 (adicionar coluna e propagar) funciona inteiro pelo VS Code.
- [ ] O motor abre num IDE JetBrains (ou Zed) via ACP e conversa normalmente.
- [ ] `.vsix` gerado no CI.
- [ ] CI verde.
