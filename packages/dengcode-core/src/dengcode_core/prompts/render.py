"""Renderiza o prompt de sistema a partir do modelo empacotado."""

from __future__ import annotations

from datetime import date
from importlib.resources import files
from string import Template


def load_template(name: str = "system.md") -> str:
    return files("dengcode_core.prompts").joinpath(name).read_text(encoding="utf-8")


def render_system_prompt(
    *,
    workspace: str,
    project_dir: str,
    stack: str | None,
    env: str,
    mode: str,
    sections: list[tuple[str, str]] | None = None,
    today: date | None = None,
) -> str:
    """``sections``: blocos extras (título, conteúdo) — PROJETO.md, conhecimento, memória."""
    extra = "".join(f"\n## {title}\n\n{body.strip()}\n" for title, body in sections or [] if body)
    return Template(load_template()).safe_substitute(
        date=(today or date.today()).isoformat(),
        workspace=workspace,
        project_dir=project_dir,
        stack=stack or "(nenhuma: só código)",
        env=env,
        mode=mode,
        extra=extra,
    )
