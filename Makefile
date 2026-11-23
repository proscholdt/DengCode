# DengCode — alvos portáveis (Windows com make do Chocolatey/Scoop/winget, Linux, macOS).
# Nada de rm/cp/export: lógica de arquivo vai por `uv run python`.

UV ?= uv
COMPOSE ?= docker compose -f docker/compose.oss.yaml

.PHONY: help dev lint fmt test test-int check clean up-oss down-oss

help:
	@$(UV) run --no-sync python -c "print('alvos: dev lint fmt test test-int check clean up-oss down-oss')"

dev:
	$(UV) sync --all-packages --all-extras

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run pyright
	$(UV) run lint-imports

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test:
	$(UV) run pytest -m "not integration"

test-int:
	$(UV) run pytest -m integration

check: lint test

clean:
	$(UV) run python -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for pat in ('**/__pycache__', '**/.pytest_cache', '.ruff_cache', 'dist', 'build') for p in pathlib.Path('.').glob(pat) if '.venv' not in p.parts]"

up-oss:
	$(COMPOSE) up -d

down-oss:
	$(COMPOSE) down
