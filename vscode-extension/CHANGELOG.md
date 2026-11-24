# Changelog

Todas as mudanças relevantes da extensão DengCode para VS Code.
O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/) e o
projeto usa [SemVer](https://semver.org/lang/pt-BR/) — `major.minor` da extensão
acompanha o do motor.

## [0.1.0] — 2026-11-23

### Adicionado

- Cliente ACP (v1) do motor `dengcode acp`: `initialize`, `session/new`,
  `session/prompt` com streaming, `session/cancel`, `session/set_mode`,
  `session/update` (mensagens, raciocínio, tool calls, plano) e
  `session/request_permission`.
- Extensões `_dengcode/*`: card de aprovação de SQL (`sqlApproval`), grade de
  prévia de dados (`dataPreview`), grafo de lineage (`lineage`), troca de stack
  e de workspace (`stack.*`, `workspace.*`).
- Chat em webview com tool calls recolhíveis e aprovação inline (CSP estrita,
  sem scripts remotos).
- Diffs propostos no editor nativo de diff, com Aceitar/Rejeitar.
- Barra de status com stack · ambiente e workspace.
- CodeLens em `.sql`: "Executar com DengCode" e "Explicar".
- Gerência do motor: configuração → PATH → `uv run` (repositório do DengCode) →
  binário baixado da release; checagem de versão (`major.minor`).
