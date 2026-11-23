# ADR 0002 — Loop de agente próprio, sem SDK de agente de terceiros

- Status: aceito
- Data: 2026-11-23

## Contexto
O objetivo é controle total sobre o comportamento, custos e segurança, e
independência de qualquer provedor de LLM.

## Decisão
Implementar o loop do agente, o formato canônico de mensagens e a camada de
ferramentas do zero. Não usar Claude Agent SDK, LangChain, LlamaIndex, CrewAI
nem LiteLLM no caminho principal. SDKs HTTP oficiais de cada provedor podem
ser usados dentro dos adapters de LLM.

## Consequências
- (+) Controle total, sem lock-in, comportamento previsível.
- (−) Mais código para escrever e manter (tradução por provedor, retries,
  streaming de tool calls) → mitigado por testes de contrato e respostas gravadas.
