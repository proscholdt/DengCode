# ADR 0009 — Versão do ACP e implementação própria do JSON-RPC

- Status: aceito
- Data: 2026-11-23
- Complementa: ADR 0003

## Contexto
O ADR 0003 adotou o Agent Client Protocol e deixou a versão para a fase 6.
Em 23/11/2026 o repositório oficial (`agentclientprotocol/agent-client-protocol`)
publica dois esquemas: **v1** (estável, `schema-v1.35.0`) e **v2**
(`schema-v2.0.0-beta.4`, ainda beta). Zed, JetBrains AI Assistant e os SDKs
oficiais falam a v1 (`protocolVersion: 1`).

Existe SDK Python oficial (`agent-client-protocol` 1.0.0 no PyPI, ~130 KB,
depende de pydantic). Ele traz, além dos tipos, sua própria camada de conexão,
uma classe base de agente, helpers de subprocesso e extras HTTP/websocket.

## Decisão
1. **Protocolo v1** (`protocolVersion: 1`), conferido contra
   `schema/v1/schema.json` (1.35.0). Se o cliente pedir outra versão, o
   servidor responde com 1 (regra da negociação do ACP).
2. **JSON-RPC 2.0 próprio**, em `dengcode_engine/acp/jsonrpc.py`: JSON
   delimitado por `\n` sobre stdio, bidirecional, ~200 linhas, só biblioteca
   padrão. **Não usamos o SDK oficial**: ele embute um modelo de agente/conexão
   que se sobrepõe ao nosso loop (princípio 3), traria dependência nova ao
   motor e não economizaria código relevante — as mensagens são dicionários
   simples, montados num único módulo (`acp/server.py` + `acp/mapping.py`).
3. **Superfície implementada** (agente): `initialize`, `session/new`,
   `session/load`, `session/prompt`, `session/cancel`; (cliente):
   `session/update` (`agent_message_chunk`, `user_message_chunk` na
   reprodução, `tool_call`, `tool_call_update`, `plan`, `usage_update` e
   `notice` só quando o cliente anuncia `session.notices`) e
   `session/request_permission` com `allow_once`, `allow_always`
   ("sempre nesta sessão") e `reject_once`.
4. **Sem `fs/*` nem `terminal/*` do cliente**: as ferramentas do DengCode
   escrevem no disco direto (com checkpoint, diff e aprovação próprios). O
   diff vai ao cliente como conteúdo `diff` (`path` absoluto, `oldText`,
   `newText` do arquivo inteiro), calculado antes da escrita.
5. **Sem MCP nesta fase**: `mcpServers` é aceito e ignorado;
   `mcpCapabilities` anuncia `http: false, sse: false`.
6. **Motivo de parada**: `end` → `end_turn`, `max_tokens` → `max_tokens`,
   `max_steps` e `budget` → `max_turn_requests`, cancelamento → `cancelled`.
   Erro do turno (LLM fora, roteiro esgotado...) vira erro JSON-RPC `-32603`
   com `data: {"reason": "error"}`. `refusal` não é emitido (o DengCode não
   tem recusa explícita do modelo). O motivo interno vai em
   `_meta.dengcode.reason`.
7. **stdio portátil**: o stdin é lido por uma thread em todos os SOs — no
   Windows, `loop.connect_read_pipe(sys.stdin)` do Proactor aceita o pipe mas
   nunca entrega dados (testado: a leitura fica parada). O descritor real do
   stdout é duplicado para uso exclusivo do protocolo e o fd 1 passa a apontar
   para o stderr, então `print` perdido ou subprocesso falante não corrompe o
   fluxo.
8. **Plano**: heurístico — se o texto do modelo antes da primeira ferramenta
   tem uma lista numerada (≥ 2 itens), vira `plan` (`pending`), e
   `completed` ao fim de um turno `end_turn`. O loop não tem passos de plano
   estruturados; quando tiver, só o `AcpServer` muda.

## Consequências
- (+) Nenhuma dependência nova; o protocolo inteiro fica em `dengcode_engine/acp/`.
- (+) Atualizar para a v2 = mudar `acp/mapping.py`/`acp/server.py`; o core não muda.
- (−) Acompanhar o esquema oficial à mão (testes de protocolo fixam os nomes).
