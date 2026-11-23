"""Base de conhecimento: índice curto no prompt; conteúdo sob demanda.

O motor monta a base (embutido > adapters > pessoal > projeto); o core só
conhece esta interface.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from dengcode_core.errors import NotFoundError


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    title: str
    summary: str
    origin: str  # "embutido" | "adapter:<nome>" | "pessoal" | "projeto"
    load: Callable[[], str]


class KnowledgeBase:
    """Entradas por id; uma entrada posterior com o mesmo id substitui a anterior."""

    def __init__(self, entries: Iterable[KnowledgeEntry] = ()) -> None:
        self._entries: dict[str, KnowledgeEntry] = {}
        for e in entries:
            self._entries[e.id] = e

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[KnowledgeEntry]:
        return sorted(self._entries.values(), key=lambda e: e.id)

    def read(self, entry_id: str) -> str:
        entry = self._entries.get(entry_id.strip().lower())
        if entry is None:
            known = ", ".join(sorted(self._entries)) or "nenhum"
            raise NotFoundError(f"conhecimento '{entry_id}' não existe (há: {known})")
        return entry.load()

    def index_text(self) -> str:
        return "\n".join(f"- `{e.id}` — {e.title}: {e.summary}" for e in self.entries())
