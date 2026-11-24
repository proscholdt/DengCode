# DengCode para VS Code

Assistente de IA para **engenharia de dados** dentro do VS Code. A extensão é um
cliente do [Agent Client Protocol (ACP)](https://agentclientprotocol.com): ela
inicia o motor do DengCode (`dengcode acp`, JSON-RPC 2.0 sobre stdio) e mostra a
conversa, as ferramentas e as aprovações na interface do editor.

## Recursos

- **Chat** na barra lateral, com respostas em streaming, raciocínio, plano e
  tool calls recolhíveis (status: pendente, executando, concluída, falhou).
- **Aprovação de SQL**: antes de rodar SQL que exige aprovação, o motor envia o
  card `_dengcode/sqlApproval` — SQL formatado, tipo (READ/DDL/DML…), motor,
  tabelas lidas e escritas, custo estimado, avisos e plano. A decisão continua
  sendo o `session/request_permission` padrão do ACP.
- **Diffs nativos**: quando o agente propõe editar arquivos (conteúdo `diff` da
  tool call), a extensão abre o editor de diff do VS Code com o antes e o
  depois; **Aceitar** / **Rejeitar** ficam no título do editor, numa
  notificação e no card do chat.
- **Prévia de dados** (`_dengcode/dataPreview`): grade com até 20 linhas, já
  mascaradas pelo motor, no chat e num painel.
- **Lineage** (`_dengcode/lineage`): grafo upstream/downstream de uma tabela ou
  coluna, desenhado em SVG (comando *DengCode: Mostrar lineage*).
- **Barra de status**: `stack · ambiente` e `workspace`; clique para trocar
  (`_dengcode/stack.*`, `_dengcode/workspace.*`).
- **CodeLens em `.sql`**: *▶ Executar com DengCode* e *Explicar* (usa a seleção,
  se houver, ou o arquivo inteiro).

## Comandos

| Comando | O que faz |
| --- | --- |
| `DengCode: Nova sessão` (`dengcode.novaSessao`) | Começa uma conversa nova |
| `DengCode: Executar SQL com DengCode` (`dengcode.executarSql`) | Envia o SQL do editor para execução (com aprovação) |
| `DengCode: Explicar SQL` (`dengcode.explicarSql`) | Pede a explicação do SQL, sem executar |
| `DengCode: Trocar stack/ambiente` (`dengcode.trocarStack`) | Lista e troca a stack e o ambiente (vale para sessões novas) |
| `DengCode: Trocar workspace` (`dengcode.trocarWorkspace`) | Lista e troca o workspace (vale para sessões novas) |
| `DengCode: Mostrar lineage` (`dengcode.mostrarLineage`) | Grafo de lineage da tabela/coluna sob o cursor |
| `DengCode: Cancelar turno` (`dengcode.cancelar`) | Envia `session/cancel` |
| `DengCode: Aceitar/Rejeitar alteração proposta` | Responde ao pedido de permissão do diff aberto |
| `DengCode: Reiniciar motor` · `Baixar motor` · `Mostrar log` | Manutenção |

## Configurações

| Chave | Padrão | Descrição |
| --- | --- | --- |
| `dengcode.caminhoMotor` | `""` | Caminho do executável `dengcode`. Vazio = procurar (ver abaixo). Aceita também um script `.js`/`.mjs` (útil em desenvolvimento). |
| `dengcode.stackPadrao` | `""` | Stack das sessões novas (ex.: `local`, `opensource`). Vazio = a ativa no motor. |
| `dengcode.ambiente` | `dev` | `dev` ou `prod`. |
| `dengcode.modoPermissao` | `ask` | `ask`, `auto` ou `read_only`. |
| `dengcode.workspace` | `""` | Workspace do DengCode (isola memória, sessões e auditoria). |

As configurações chegam ao motor de duas formas: como variáveis de ambiente do
processo (`DENGCODE_STACK`, `DENGCODE_ENV`, `DENGCODE_MODE`,
`DENGCODE_WORKSPACE`) e, quando o motor anuncia as extensões, por
`_dengcode/stack.use` e `_dengcode/workspace.use` logo após o `initialize`. O
modo de permissão também é aplicado por `session/set_mode` se o motor expuser
modos ACP com esses ids.

## Como o motor é encontrado

1. `dengcode.caminhoMotor`, se preenchido (se o caminho não existir, a extensão
   avisa e não tenta outras opções);
2. `dengcode` no `PATH` (no Windows, respeitando `PATHEXT`: `.exe`, `.cmd`…);
3. `uv run --project <pasta> dengcode`, se uma pasta aberta for o próprio
   repositório do DengCode (tem `packages/dengcode-cli/pyproject.toml`) e o `uv`
   estiver no `PATH`;
4. binário baixado antes pela extensão, em `globalStorage/engine/v<versão>/`.

Sem motor, a extensão oferece **Baixar motor** (binário da release
`https://github.com/proscholdt/DengCode/releases/download/v<versão>/dengcode-<so>-<arq>[.exe]`,
com alvos `win-x64`, `linux-x64`, `linux-arm64`, `macos-x64`, `macos-arm64`),
**Configurar caminho** ou **Como instalar**. As releases com binários só
existem a partir da fase 8; até lá, instale o motor com `uv` (ex.: `uv tool
install` a partir do repositório) ou abra o repositório do DengCode no VS Code.

A versão é checada com `dengcode --version`: motor e extensão precisam ter o
mesmo `major.minor` (o patch pode variar). Versão diferente gera um aviso com a
opção de baixar a versão compatível.

## Decisão: biblioteca oficial do ACP só para tipos

Existe uma biblioteca TypeScript oficial e mantida,
[`@agentclientprotocol/sdk`](https://www.npmjs.com/package/@agentclientprotocol/sdk)
(o antigo `@zed-industries/agent-client-protocol` foi renomeado para ela). A
extensão a usa assim:

- **tipos do protocolo** (`import type`, ACP v1): `SessionUpdate`,
  `ToolCallContent`, `RequestPermissionRequest`… — checados em compilação,
  **zero bytes no bundle**;
- **teste de interoperabilidade**: um agente feito com a biblioteca oficial
  conversa com o nosso cliente (`test/unit/sdkInterop.test.ts`), garantindo que
  enquadramento e formas batem com a implementação de referência.

O transporte em si (NDJSON + JSON-RPC, ~300 linhas em
`src/protocol/jsonrpc.ts` e `acpClient.ts`) é próprio, porque:

1. a biblioteca é ESM-only, com `zod` como dependência de execução e API 2.x
   ainda mudando a cada versão maior; o cliente precisa de pouco dela;
2. as notificações `_dengcode/*` chegam intercaladas com `session/update` e
   `session/request_permission`, e o controle direto do despacho deixa simples
   anexar o card de SQL à tool call certa;
3. sem validação de esquema em tempo de execução, o cliente tolera um servidor
   que evolui em paralelo (campos novos, variantes desconhecidas são ignoradas);
4. a extensão fica sem dependências de execução (o `.vsix` leva só o bundle).

## Arquitetura

```
src/
  extension.ts              ativação, comandos, CodeLens, barra de status
  config.ts                 leitura de dengcode.*
  protocol/                 JSON-RPC (NDJSON) + cliente ACP + tipos _dengcode/*
  engine/                   localizar, baixar, checar versão e iniciar o motor
  session/                  controlador da sessão + pedidos de permissão
  chat/                     modelo de visão (updates → itens), view da webview, mensagens tipadas
  webview/                  script da webview (navegador) e HTML dos itens
  diff/                     documentos virtuais + vscode.diff
  preview/  lineage/        HTML/SVG dos painéis (sem scripts)
  render/                   escape, CSP, markdown seguro, realce de SQL
```

Os módulos de `protocol/`, `engine/` (menos `engineManager.ts`), `chat/viewModel.ts`,
`render/`, `preview/`, `lineage/` e `webview/renderItems.ts` não importam o VS
Code e são testados com `node --test`.

**Segurança das webviews**: CSP `default-src 'none'`, scripts só da extensão
com nonce (o painel de prévia e o de lineage não têm script nenhum), nada
remoto. Todo texto do motor é escapado; o markdown do chat não repassa HTML e
só gera links `http(s)`. As mensagens que a webview envia são validadas
(`parseFromWebview`) antes de chegar ao controlador.

## Desenvolvimento

Requisitos: Node.js 22+ (o CI usa 24) e npm.

```bash
cd vscode-extension
npm ci
npm run build          # dist/ (extensão + webview)
npm run lint           # tsc --noEmit (strict)
npm run test:unit      # node --test via tsx
npm run test:integration   # baixa um VS Code e roda a extensão com o motor fake
npm run package        # gera dengcode-<versão>.vsix
```

No Linux sem interface gráfica, rode os testes de integração com
`xvfb-run -a npm run test:integration`. Para depurar, abra a pasta
`vscode-extension/` no VS Code e use **Executar Extensão** (F5).

### Motor fake

`test/fake-engine/fake-dengcode.mjs` é um motor ACP mínimo, sem dependências,
usado pelos testes. Ele roteiriza respostas pelo texto do prompt:
`editar` (tool call com diff + permissão), `sql` (plano, `sqlApproval`,
permissão e `dataPreview`), `lento`/`lenta` (espera `session/cancel`); outro
texto recebe uma resposta simples. Para experimentar a interface sem o motor
real, aponte `dengcode.caminhoMotor` para esse arquivo.

## Instalar o `.vsix` localmente

```bash
code --install-extension dengcode-0.1.0.vsix
```

ou, no VS Code, *Extensões → … → Instalar do VSIX…*.
