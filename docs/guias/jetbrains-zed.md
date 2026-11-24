# Guia — DengCode como agente externo no Zed e no JetBrains (ACP)

> **Status:** configuração escrita a partir da documentação oficial do Zed e do
> JetBrains AI Assistant (consultada em 23/11/2026) e validada com o cliente ACP
> de teste e com `dengcode chat --acp`. **Ainda não testada dentro das IDEs**
> (o ambiente de desenvolvimento não abre Zed nem JetBrains) — registrar o
> resultado do teste manual no `PROGRESSO.md`.

O DengCode fala o **Agent Client Protocol v1** (ADR 0009): a IDE inicia
`dengcode acp` como subprocesso e conversa por stdin/stdout. Os recursos
padrão (chat em streaming, tool calls, diffs, pedidos de permissão,
cancelamento, retomar sessão) funcionam em qualquer cliente ACP. As extensões
`_dengcode/*` (card de SQL, grade de dados, lineage, troca de stack/workspace)
são usadas pela extensão do VS Code; Zed e JetBrains as ignoram.

## Pré-requisitos

1. `dengcode` instalado e no `PATH` (`dengcode --version`). Se não estiver no
   `PATH`, use o caminho absoluto do executável (no Windows, algo como
   `C:\Users\voce\...\Scripts\dengcode.exe`).
2. Uma stack que funciona no terminal: `dengcode stack validate <stack>` e
   `dengcode stack doctor <stack>`.
3. Credenciais do LLM no ambiente ou no `.env` do projeto (o servidor carrega
   `<projeto>/.env` ao abrir cada sessão, sem sobrescrever o que já existe).

## Escolher stack, ambiente e workspace

O `dengcode acp` aceita as opções da CLI e as mesmas variáveis de ambiente
(úteis na configuração da IDE):

| Opção | Variável | Padrão |
| --- | --- | --- |
| `--stack` | `DENGCODE_STACK` | `local` |
| `--env` | `DENGCODE_ENV` | `dev` |
| `--workspace` | `DENGCODE_WORKSPACE` | o ativo (`dengcode workspace use`) |
| `--mode` | `DENGCODE_MODE` | o da stack (`ask`) |
| `--llm` | `DENGCODE_LLM` | o da stack |
| — | `DENGCODE_HOME` | `~/.dengcode` |

O diretório do projeto é o `cwd` que a IDE manda ao criar a sessão (a raiz do
projeto aberto). Em prod, `auto` vira `ask` e escritas continuam bloqueadas
conforme a stack.

## Zed

Abra as configurações (`zed: open settings`) e adicione em `agent_servers`:

```json
{
  "agent_servers": {
    "DengCode": {
      "type": "custom",
      "command": "dengcode",
      "args": ["acp"],
      "env": {
        "DENGCODE_STACK": "local",
        "DENGCODE_ENV": "dev"
      }
    }
  }
}
```

Ou pela interface: **Agent Settings → External Agents → Add Agent → Add Custom
Agent**. Depois, abra o **Agent Panel** e escolha *DengCode* no seletor de
agentes (ou "new thread" → DengCode). Para depurar a troca de mensagens:
`dev: open acp logs` na paleta de comandos.

## JetBrains (AI Assistant)

No painel **AI Chat**, clique em **⋯ → Add Custom Agent**. A IDE cria e abre o
arquivo `~/.jetbrains/acp.json` (no Windows, `%USERPROFILE%\.jetbrains\acp.json`):

```json
{
  "default_mcp_settings": {},
  "agent_servers": {
    "DengCode": {
      "command": "dengcode",
      "args": ["acp", "--stack", "local"],
      "env": {
        "DENGCODE_ENV": "dev",
        "DENGCODE_WORKSPACE": "default"
      }
    }
  }
}
```

Salve o arquivo; o agente aparece no seletor do AI Chat. Se não aparecer,
confira se o JSON é válido e reinicie a IDE. Não coloque chaves de API nesse
arquivo se ele for versionado — prefira o `.env` do projeto ou variáveis do
sistema.

## O que esperar

- **Aprovações**: escrita de arquivo, SQL de escrita/DDL e comandos pedem
  permissão pelo diálogo da IDE (*Permitir*, *Sempre nesta sessão*,
  *Recusar*). Leituras não pedem.
- **Diffs**: edições chegam como diff do arquivo inteiro; o DengCode grava no
  disco direto (não usa `fs/*` da IDE) e `dengcode undo` desfaz a última escrita.
- **Retomar sessão**: clientes que suportam `session/load` reabrem uma
  conversa salva (`dengcode sessions list` mostra os ids).
- **Servidores MCP** configurados na IDE são ignorados nesta fase.
- **Logs**: tudo vai para o stderr (`dengcode -v acp` para depuração); o
  stdout é exclusivo do protocolo.

## Testar sem a IDE

```
dengcode chat --acp --stack local
```

A CLI sobe `dengcode acp` como subprocesso e se comporta como uma IDE (mesmo
caminho de código): se isso funciona, o problema está na configuração da IDE.
