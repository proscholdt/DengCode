# Fase 1 — Fundação

**Objetivo:** um monorepo vazio, mas com toda a engenharia pronta: build,
lint, tipos, testes, regras de dependência e CI em Windows, Linux e macOS.

**Leia antes:** `CLAUDE.md`, `docs/ARQUITETURA.md` (seção 11), ADR 0001.

## Tarefas

1. **Workspace uv**
   - `pyproject.toml` raiz com `[tool.uv.workspace] members = ["packages/*", "packages/adapters/*"]`.
   - Pacotes iniciais (layout `src/`): `dengcode-core`, `dengcode-engine`,
     `dengcode-cli`, `dengcode-testkit` e um adapter exemplo `packages/adapters/local-fs`.
   - Cada pacote com `pyproject.toml` próprio, `py.typed`, `__init__.py` com `__version__`.
   - Python mínimo 3.12 (`.python-version`).
2. **Ruff** — config na raiz: regras `E,F,W,I,UP,B,SIM,RUF,ASYNC,PT,N`, line-length 100, `ruff format`.
3. **Pyright** — `strict` em `dengcode-core`; `standard` no resto.
4. **import-linter** — `.importlinter` com contratos:
   - `dengcode_core` não importa `dengcode_engine`, `dengcode_cli`, `dengcode_adapter_*`.
   - adapters não importam uns aos outros nem `dengcode_engine`/`dengcode_cli`.
   - `dengcode_core` não importa SDKs de nuvem/LLM (`boto3`, `azure`, `google.cloud`, `anthropic`, `openai`, `databricks`).
5. **Esqueleto do core** — só estrutura, sem lógica:
   `dengcode_core/{errors.py, ports/__init__.py, ports/models.py, llm/__init__.py}`
   com a hierarquia de erros de `docs/especificacoes/portas.md`.
6. **Registro de adapters** — em `dengcode_engine/registry.py`: descobrir
   entry points do grupo `dengcode.adapters`, listar e instanciar por nome.
   O adapter `local-fs` registra-se como exemplo (implementação mínima:
   `list` e `read_bytes` de `StoragePort`).
7. **CLI mínima** — `dengcode --version` e `dengcode adapters list` (Typer + Rich).
8. **Testes** — pytest + pytest-asyncio configurados; testes de: registro
   descobre `local-fs`; CLI `--version`; hierarquia de erros.
9. **Makefile** — alvos `dev, lint, fmt, test, test-int, clean` funcionando em
   Windows (Git Bash ou `make` do Chocolatey/Scoop) e Linux. Evite comandos só
   de Unix; use `uv run python -c ...` quando precisar de lógica portátil.
   Documente no README como instalar `make` no Windows.
10. **Docker** — `docker/Dockerfile` do motor (multi-stage com uv) e
    `docker/compose.oss.yaml` vazio por enquanto (preenchido na fase 3/5).
11. **CI** — `.github/workflows/ci.yml`: matriz `ubuntu-latest, windows-latest,
    macos-latest` × Python `3.12, 3.13`; passos: instalar uv, `make dev`,
    `make lint`, `make test`. Cache do uv.
12. **Higiene** — `.gitignore`, `.editorconfig`, `.gitattributes`
    (`* text=auto eol=lf`, `*.bat eol=crlf`), `pre-commit` com ruff.
13. **Copiar presets** — mover `stacks/*.yaml` para dentro do pacote
    `dengcode-engine` como dados empacotados (mantenha `stacks/` na raiz como
    link/cópia documental, ou referencie de um só lugar — registre a escolha).

## Critério de pronto

- [ ] `make lint` e `make test` passam localmente.
- [ ] CI verde nos 3 sistemas operacionais × 2 versões de Python.
- [ ] `uv run dengcode adapters list` mostra `local-fs`.
- [ ] Um import proibido (teste manual: `import boto3` no core) faz `make lint` falhar.
- [ ] `PROGRESSO.md` atualizado.
