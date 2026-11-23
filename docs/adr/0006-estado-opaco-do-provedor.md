# ADR 0006 — Estado opaco do provedor no formato canônico

- Status: aceito
- Data: 2026-11-23

## Contexto

Modelos recentes de alguns provedores devolvem, no turno do assistente, blocos
que precisam voltar **inalterados** na próxima requisição (ex.: blocos de
raciocínio assinados, ligados ao prefixo exato da conversa; blocos de
fallback do servidor). Traduzir esses blocos para `TextPart`/`ToolCallPart` e
reconstruí-los depois perde informação e a requisição seguinte pode ser
recusada. O formato canônico original (`llm-formato-canonico.md`) não tinha
onde guardar isso.

## Decisão

- Novo part canônico `ProviderStatePart(provider, data)` e novo evento de
  streaming `ProviderState(provider, data)`, emitido pelo adapter antes de `Stop`.
- O loop anexa o `ProviderStatePart` à mensagem do assistente, junto com o texto
  e as tool calls canônicas (que continuam sendo a fonte da verdade para o core).
- Na tradução de volta, o adapter que reconhece o próprio `provider` reutiliza
  `data` byte a byte; qualquer outro adapter ignora o part (troca de provedor
  continua funcionando, só sem o estado opaco).
- O core nunca lê `data`.

Consequências para quem implementa adapters: manter o histórico *append-only*
do lado do provedor e documentar no adapter como ele lida com edições de
histórico feitas pela compactação do core.

## Consequências

- (+) Compatível com provedores que exigem replay exato, sem citar nenhum no core.
- (+) Fallback entre provedores segue possível.
- (−) Sessões salvas ficam maiores (o estado vai junto no JSONL).
- (−) A compactação do core edita o histórico; o adapter precisa degradar com
  elegância (ex.: pedir ao provedor que descarte blocos que não batem).
