# Plano do projeto — 8 fases

Fases sequenciais, sem datas fixas. Cada fase só termina quando o critério de
pronto passa no CI. A ordem prova o loop com tudo local (preset open source)
antes de ir para a nuvem.

| # | Fase | Arquivo | Critério de pronto (resumo) |
| --- | --- | --- | --- |
| 1 | Fundação | `fases/fase-1-fundacao.md` | `make lint` e `make test` passam em Windows, Linux e macOS |
| 2 | Core + LLM | `fases/fase-2-core-llm.md` | agente lê e edita um projeto via Anthropic, OpenAI ou Ollama, com diff aprovado na CLI |
| 3 | Dados locais | `fases/fase-3-dados-locais.md` | "crie um modelo silver a partir destes CSVs" funciona sem escrita sem aprovação |
| 4 | Verificação e transformação | `fases/fase-4-verificacao-transformacao.md` | agente altera modelo dbt, roda testes, mostra diff de dados e impacto downstream |
| 5 | Memória, orquestração, streaming | `fases/fase-5-memoria-orquestracao-streaming.md` | "por que a DAG X falhou ontem?" respondida; preset `opensource` passa nos contratos |
| 6 | ACP + VS Code | `fases/fase-6-acp-vscode.md` | fase 4 inteira pelo VS Code; motor abre num IDE JetBrains via ACP |
| 7 | Presets de nuvem | `fases/fase-7-presets-nuvem.md` | cada preset passa nos contratos e no `stack doctor` contra conta de teste |
| 8 | Distribuição | `fases/fase-8-distribuicao.md` | usuário novo instala e roda a 1ª tarefa em < 10 min só com a documentação |
