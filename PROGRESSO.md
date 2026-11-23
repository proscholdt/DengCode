# Progresso

> O Claude Code atualiza este arquivo ao fim de cada tarefa.
> Marque `[x]` no que terminou, registre decisões e pendências.

## Estado atual

- Fase atual: **2 — Core + LLM**
- Última atualização: 2026-11-23

## Fases

- [x] Fase 1 — Fundação
- [ ] Fase 2 — Core + LLM
- [ ] Fase 3 — Dados locais
- [ ] Fase 4 — Verificação e transformação
- [ ] Fase 5 — Memória, orquestração e streaming (preset open source completo)
- [ ] Fase 6 — ACP + extensão do VS Code
- [ ] Fase 7 — Presets de nuvem (Azure → AWS → Google)
- [ ] Fase 8 — Distribuição e lançamento

## Decisões em aberto (do plano)

- [ ] Produto para outros profissionais ou ferramenta de uso próprio?
- [x] Licença: **Apache-2.0** (já definida no repositório `proscholdt/DengCode`)
- [x] Nome definitivo do produto: **DengCode** (pacotes `dengcode-*`, módulos
      `dengcode_*`, comando `dengcode`, pasta `~/.dengcode/`, entry points
      `dengcode.adapters`, extensões ACP `_dengcode/*`)
- [ ] Executável único: PyInstaller ou Nuitka?
- [ ] TUI rica (Textual) já na fase 2 ou CLI simples até a fase 6?
- [ ] Orquestrador open source padrão: Airflow ou Dagster?

## Decisões tomadas na implementação

| Fase | Decisão |
| --- | --- |
| 1 | Presets de stack vivem **só** em `packages/dengcode-engine/src/dengcode_engine/stacks/` (dados empacotados); `stacks/README.md` na raiz aponta para lá. Evita duas cópias divergentes. |
| 1 | Raiz do workspace uv é *virtual* (sem `[project]`); `make dev` = `uv sync --all-packages --all-extras`. |
| 1 | Fronteira do core verificada duas vezes: `import-linter` (contratos em `.importlinter`) e ruff `TID251` (`packages/dengcode-core/ruff.toml`). |
| 1 | Entry point aponta para a **classe** do adapter, que expõe `info: AdapterInfo` (nome + porta) e `from_config(cfg, ctx)`. O registro indexa por (porta, nome), então um pacote pode registrar várias portas com o mesmo nome (ex.: duckdb como QueryEngine e Catalog). |
| 1 | `pyarrow-stubs` como dependência de dev para o pyright strict no core. |
| 1 | Ruff `N818` desligado: nomes de erro seguem a especificação (`CostLimitExceeded`, `UnsupportedOperation`). |
| 1 | Adapter `local-fs` já implementa a `StoragePort` completa (CSV/Parquet/JSON via PyArrow); escrita desligada por padrão. |

## Registro

| Data | Fase/tarefa | O que foi feito | Pendências |
| --- | --- | --- | --- |
| 2026-11-23 | Fase 1 | Workspace uv (core, engine, cli, testkit, local-fs), ruff, pyright strict no core, import-linter, erros, registro por entry points, CLI `--version`/`adapters list`, Makefile portátil, Dockerfile multi-stage, compose vazio, CI 3 SO × 2 Pythons, higiene (gitignore, editorconfig, gitattributes, pre-commit). `make lint` e `make test` verdes; `import boto3` no core quebra o lint (ruff e import-linter). | CI verde a confirmar no GitHub após o push. |
