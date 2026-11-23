# Especificação — Protocolo de interface (ACP)

O motor expõe o **Agent Client Protocol (ACP)**: JSON-RPC 2.0 sobre stdio,
iniciado pelo cliente como subprocesso (`dengcode acp`). Especificação oficial:
https://agentclientprotocol.com — antes de implementar a fase 6, leia a versão
atual da especificação e o SDK Python oficial, se existir, e registre num ADR a
versão adotada.

## Camadas

```
EventBus (core, interno, estável)
   ↓
AcpServer (engine)  — traduz eventos ↔ mensagens ACP
   ↓ stdio
Clientes: CLI, extensão VS Code, JetBrains, Zed
```

O core nunca depende do ACP. Se o protocolo mudar, só o `AcpServer` muda.

## Mapeamento mínimo

| Conceito do DengCode | ACP |
| --- | --- |
| Criar/retomar sessão | criação e carregamento de sessão |
| Mensagem do usuário | prompt da sessão |
| Texto em streaming | atualizações de sessão (chunks de mensagem) |
| Chamada de ferramenta | atualizações de tool call (início, progresso, fim) |
| Diff de arquivo | conteúdo de diff na tool call |
| Pedido de aprovação | pedido de permissão ao cliente |
| Plano | atualização de plano |
| Cancelar | cancelamento da sessão |

## Extensões próprias (métodos com prefixo `_dengcode/`)

Usadas pela extensão do VS Code; clientes ACP genéricos simplesmente as ignoram
e continuam funcionando com o subconjunto padrão.

| Método / notificação | Conteúdo |
| --- | --- |
| `_dengcode/sqlApproval` | SQL formatado, kind, tabelas afetadas, custo estimado, avisos |
| `_dengcode/dataPreview` | schema + linhas da prévia (já mascaradas) + caminho do Parquet |
| `_dengcode/lineage` | grafo (nós e arestas) para uma tabela |
| `_dengcode/stack.list` / `_dengcode/stack.use` | listar e trocar stack/ambiente |
| `_dengcode/workspace.list` / `_dengcode/workspace.use` | listar e trocar workspace |

## CLI como cliente

A CLI pode rodar o motor in-process (modo padrão, mais simples) ou como cliente
ACP de um subprocesso (`--acp`), que é o modo usado nos testes de protocolo.

## Contrato das extensões `_dengcode/*` (fase 6)

Chaves em `camelCase`. Clientes que não conhecem os métodos simplesmente os
ignoram (notificações) ou não os chamam (requisições).

Capacidade anunciada no `initialize` (resposta do agente):
`agentCapabilities._meta.dengcode = { "version": 1, "extensions": ["sqlApproval", "dataPreview", "lineage", "stack", "workspace"] }`.

Notificações agente → cliente:

| Método | Quando | `params` |
| --- | --- | --- |
| `_dengcode/sqlApproval` | logo **antes** do `session/request_permission` de uma ferramenta de SQL | `{sessionId, toolCallId, sql, kind, engine, statements, tablesRead[], tablesWritten[], cost, warnings[], plan}` |
| `_dengcode/dataPreview` | quando uma ferramenta de dados termina com prévia tabular | `{sessionId, toolCallId, columns:[{name,type}], rows:[{coluna: valor}] (≤ 20, já mascaradas), totalRows, resultPath}` |

Requisições cliente → agente:

| Método | `params` | `result` |
| --- | --- | --- |
| `_dengcode/lineage` | `{sessionId, target, direction: "down"\|"up", depth}` | `{root, nodes:[{id, kind: "table"\|"column"}], edges:[{source, target, via}]}` |
| `_dengcode/stack.list` | `{}` | `{stacks:[{name, origin, description}], active:{stack, env}}` |
| `_dengcode/stack.use` | `{stack, env}` | `{stack, env}` (vale para sessões novas) |
| `_dengcode/workspace.list` | `{}` | `{workspaces:[...], active}` |
| `_dengcode/workspace.use` | `{workspace}` | `{workspace}` (vale para sessões novas) |

O `session/request_permission` padrão continua sendo a única forma de aprovar;
o card de SQL é só apresentação. Opções de permissão: `allow_once`,
`allow_always` (= "sempre nesta sessão") e `reject_once`.
