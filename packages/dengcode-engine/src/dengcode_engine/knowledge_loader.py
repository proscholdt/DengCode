"""Monta a base de conhecimento: embutido < adapters da stack < pessoal < projeto.

- embutido: ``dengcode_engine/knowledge/index.yaml`` (fundamentos neutros);
- adapters: ``AdapterInfo.knowledge_pack`` = ``"modulo:caminho/arquivo.md"``;
- pessoal: ``~/.dengcode/knowledge/*.md``; projeto: ``<repo>/.dengcode/knowledge/*.md``.
Mesmo id: a origem posterior substitui a anterior.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from dengcode_core.knowledge import KnowledgeBase, KnowledgeEntry
from dengcode_core.ports.models import AdapterInfo

log = logging.getLogger(__name__)


def _title_summary(text: str, fallback: str) -> tuple[str, str]:
    title, summary = fallback, ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") and title == fallback:
            title = stripped.lstrip("#").strip()
            continue
        if not stripped.startswith("#"):
            summary = re.sub(r"\s+", " ", stripped)[:120]
            break
    return title, summary


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")


def builtin_entries() -> list[KnowledgeEntry]:
    root = files("dengcode_engine").joinpath("knowledge")
    index = root.joinpath("index.yaml")
    if not index.is_file():
        return []
    data: list[dict[str, Any]] = yaml.safe_load(index.read_text(encoding="utf-8")) or []
    out: list[KnowledgeEntry] = []
    for item in data:
        target = root.joinpath(str(item["path"]))
        out.append(
            KnowledgeEntry(
                id=str(item["id"]),
                title=str(item["title"]),
                summary=str(item.get("summary", "")),
                origin="embutido",
                load=lambda t=target: t.read_text(encoding="utf-8"),
            )
        )
    return out


def adapter_entries(infos: Iterable[AdapterInfo]) -> list[KnowledgeEntry]:
    out: list[KnowledgeEntry] = []
    seen: set[str] = set()
    for info in infos:
        pack = info.knowledge_pack
        if not pack or pack in seen or ":" not in pack:
            continue
        seen.add(pack)
        module, _, rel = pack.partition(":")
        try:
            target = files(module).joinpath(rel)
            if not target.is_file():
                continue
            text = target.read_text(encoding="utf-8")
        except (ModuleNotFoundError, OSError) as exc:
            log.debug("pacote de conhecimento %s indisponível: %s", pack, exc)
            continue
        title, summary = _title_summary(text, info.name)
        out.append(
            KnowledgeEntry(
                id=_slug(Path(rel).stem),
                title=title,
                summary=summary,
                origin=f"adapter:{info.name}",
                load=lambda t=target: t.read_text(encoding="utf-8"),
            )
        )
    return out


def folder_entries(folder: Path, origin: str) -> list[KnowledgeEntry]:
    if not folder.is_dir():
        return []
    out: list[KnowledgeEntry] = []
    for path in sorted(folder.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title, summary = _title_summary(text, path.stem)
        out.append(
            KnowledgeEntry(
                id=_slug(path.stem),
                title=title,
                summary=summary,
                origin=origin,
                load=lambda p=path: p.read_text(encoding="utf-8"),
            )
        )
    return out


def load_knowledge(
    *, adapters: Iterable[AdapterInfo], home: Path, project_dir: Path
) -> KnowledgeBase:
    return KnowledgeBase(
        [
            *builtin_entries(),
            *adapter_entries(adapters),
            *folder_entries(home / "knowledge", "pessoal"),
            *folder_entries(project_dir / ".dengcode" / "knowledge", "projeto"),
        ]
    )
