# ADR 0003 — ACP como protocolo entre motor e interfaces

- Status: aceito (versão do protocolo definida no ADR 0009)
- Data: 2026-11-23

## Contexto
O usuário quer baixar o sistema e usá-lo pela CLI ou pela extensão do VS Code.
Criar um protocolo próprio obrigaria escrever um plugin por editor.

## Decisão
O motor fala o Agent Client Protocol (JSON-RPC 2.0 sobre stdio). Recursos
específicos (aprovação de SQL com custo, prévia de dados, lineage) são
métodos de extensão com prefixo `_dengcode/`. O core usa um EventBus interno
que não depende do ACP.

## Consequências
- (+) VS Code (extensão própria), JetBrains e Zed com o mesmo motor.
- (−) Dependência da evolução do ACP → isolada no `AcpServer`.
