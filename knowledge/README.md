# Conhecimento do DengCode

O conhecimento embutido **não fica nesta pasta**. Ele é Markdown empacotado
dentro do motor, como os presets de stack, para que quem instala (PyPI,
executável ou extensão) receba tudo — fonte única:

- **Fundamentos** (neutros, sem nome de ferramenta):
  `packages/dengcode-engine/src/dengcode_engine/knowledge/fundamentos/<id>.md`,
  listados em `packages/dengcode-engine/src/dengcode_engine/knowledge/index.yaml`
  (`id`, `title`, `summary`, `path`).
- **Pacotes por ferramenta**: cada adapter traz o seu, em
  `packages/adapters/<adapter>/src/dengcode_adapter_<adapter>/knowledge/<ferramenta>.md`
  (ex.: `duckdb.md`, `postgres.md`, `dbt.md`), declarado no `AdapterInfo`
  (`knowledge_pack`).

O índice (título + resumo) vai no prompt do agente; o conteúdo é lido sob
demanda pela ferramenta `knowledge_read`.

## Conhecimento do projeto e pessoal

Times e usuários podem acrescentar Markdown próprio:

| Origem | Pasta | Uso típico |
| --- | --- | --- |
| projeto | `<projeto>/.dengcode/knowledge/` | convenções do time, glossário, regras de negócio (versionado no git do projeto) |
| pessoal | `~/.dengcode/knowledge/` | anotações e preferências do usuário |
| embutido | pacote do motor e dos adapters | fundamentos e ferramentas |

Precedência: **projeto > pessoal > embutido**. Um arquivo com o mesmo `id`
(nome do arquivo sem `.md`) substitui o de menor precedência — por exemplo,
`<projeto>/.dengcode/knowledge/scd.md` passa a valer no lugar do fundamento
`scd` embutido.

Nunca coloque segredos ou dados pessoais nesses arquivos: o conteúdo vai para o
contexto do LLM.
