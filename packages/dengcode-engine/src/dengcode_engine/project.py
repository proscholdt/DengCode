"""``<repo>/.dengcode/PROJETO.md``: convenções, camadas e tabelas protegidas.

Humano edita; o agente só propõe diff (a escrita passa pela aprovação normal).
"""

from __future__ import annotations

import re
from pathlib import Path

from dengcode_engine.paths import project_state_dir

TEMPLATE = """# PROJETO.md — regras deste repositório para o DengCode

> O agente lê este arquivo no início de cada sessão. Mantenha curto e objetivo.

## Visão geral

Descreva em 2 ou 3 linhas o que este projeto de dados faz.

## Camadas e convenções

- bronze: dados brutos, sem transformação (prefixo `bronze_`)
- silver: limpo, tipado e deduplicado (prefixo `silver_`)
- gold: pronto para consumo, com grão documentado (prefixo `gold_`)
- Nomes em `snake_case`; datas em UTC.

## Proibições

- Nunca apagar ou sobrescrever tabelas gold sem aprovação do time.

## Tabelas protegidas

<!-- Uma por linha. Qualquer escrita nelas é bloqueada pela política. -->
- gold.fato_vendas
"""

_PROTECTED = re.compile(r"^##\s+tabelas\s+protegidas\s*$", re.IGNORECASE | re.MULTILINE)


def project_file(project_dir: Path) -> Path:
    return project_state_dir(project_dir) / "PROJETO.md"


def init_project(project_dir: Path) -> tuple[Path, bool]:
    """Cria ``.dengcode/PROJETO.md`` se não existir. Devolve (caminho, criado?)."""
    path = project_file(project_dir)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path, True


def protected_tables(text: str) -> list[str]:
    match = _PROTECTED.search(text)
    if not match:
        return []
    out: list[str] = []
    for line in text[match.end() :].splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith(("- ", "* ")):
            name = stripped[2:].strip().strip("`")
            if name:
                out.append(name.lower())
    return out


def load_project(project_dir: Path, *, max_chars: int = 6000) -> tuple[str | None, list[str]]:
    path = project_file(project_dir)
    if not path.is_file():
        return None, []
    text = path.read_text(encoding="utf-8")
    body = text if len(text) <= max_chars else text[:max_chars] + "\n… (truncado)"
    return body, protected_tables(text)
