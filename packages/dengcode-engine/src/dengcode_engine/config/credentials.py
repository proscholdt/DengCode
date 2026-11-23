"""Credenciais: ``env://VAR`` e arquivo ``.env``. Valores nunca são logados."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dengcode_core.errors import ConfigError

ENV_SCHEME = "env://"
SECRET_SCHEME = "secret://"


def parse_dotenv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def load_dotenv(project_dir: Path, environ: dict[str, str] | None = None) -> list[str]:
    """Carrega ``<projeto>/.env`` sem sobrescrever variáveis já definidas.

    Devolve só os *nomes* carregados (nunca os valores).
    """
    target = os.environ if environ is None else environ
    path = project_dir / ".env"
    if not path.is_file():
        return []
    loaded: list[str] = []
    for key, value in parse_dotenv(path.read_text(encoding="utf-8")).items():
        if key not in target and value:
            target[key] = value
            loaded.append(key)
    return loaded


def resolve_value(value: Any, environ: Mapping[str, str] | None = None) -> Any:
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        if value.startswith(ENV_SCHEME):
            var = value.removeprefix(ENV_SCHEME)
            if var not in env or not env[var]:
                raise ConfigError(f"variável de ambiente {var} não definida (use .env ou exporte)")
            return env[var]
        if value.startswith(SECRET_SCHEME):
            raise ConfigError(
                f"referência {value.split('/')[0]}//… exige uma porta de segredos configurada"
            )
        return value
    if isinstance(value, dict):
        return {k: resolve_value(v, env) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [resolve_value(v, env) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return value
