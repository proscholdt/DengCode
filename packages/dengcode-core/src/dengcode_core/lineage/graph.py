"""Grafo de lineage (tabelas e colunas) e análise de impacto."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from dengcode_core.lineage.columns import normalize
from dengcode_core.ports.models import LineageEdge


@dataclass(frozen=True)
class ImpactItem:
    entity: str
    level: int
    path: tuple[str, ...]
    via: str | None


def _table(entity: str) -> str:
    return entity.rsplit(".", 1)[0]


class LineageGraph:
    """Arestas de coluna (``s.t.c → s.t2.c2``) e de tabela (``s.t → s.t2``).

    Arestas de coluna também geram as de tabela correspondentes.
    """

    def __init__(
        self, column_edges: Iterable[LineageEdge] = (), table_edges: Iterable[LineageEdge] = ()
    ) -> None:
        self._down: dict[str, list[tuple[str, str | None]]] = {}
        self._up: dict[str, list[tuple[str, str | None]]] = {}
        self.columns: set[str] = set()
        self.tables: set[str] = set()
        for e in column_edges:
            src, dst = e.source.lower(), e.target.lower()
            self.columns.update((src, dst))
            self._add(src, dst, e.via)
            self._add_table(_table(src), _table(dst), e.via)
        for e in table_edges:
            self._add_table(normalize(e.source), normalize(e.target), e.via)

    def _add(self, src: str, dst: str, via: str | None) -> None:
        if src == dst:
            return
        if (dst, via) not in self._down.setdefault(src, []):
            self._down[src].append((dst, via))
            self._up.setdefault(dst, []).append((src, via))

    def _add_table(self, src: str, dst: str, via: str | None) -> None:
        self.tables.update((src, dst))
        self._add(src, dst, via)

    def resolve(self, name: str) -> tuple[str, Literal["table", "column"]] | None:
        """Aceita ``tabela``, ``schema.tabela``, ``tabela.coluna`` ou ``schema.tabela.coluna``."""
        key = name.strip().strip('"`').lower()
        for pool, kind in ((self.tables, "table"), (self.columns, "column")):
            exact = [n for n in pool if n == key]
            if exact:
                return exact[0], kind  # type: ignore[return-value]
            suffix = sorted(n for n in pool if n.endswith("." + key))
            if len(suffix) == 1:
                return suffix[0], kind  # type: ignore[return-value]
        return None

    def walk(
        self, entity: str, *, direction: Literal["up", "down"] = "down", depth: int = 5
    ) -> list[ImpactItem]:
        graph = self._down if direction == "down" else self._up
        out: list[ImpactItem] = []
        seen = {entity}
        queue: deque[tuple[str, tuple[str, ...]]] = deque([(entity, (entity,))])
        while queue:
            node, path = queue.popleft()
            if len(path) > depth:
                continue
            for nxt, via in graph.get(node, []):
                if nxt in seen:
                    continue
                seen.add(nxt)
                new_path = (*path, nxt)
                out.append(ImpactItem(nxt, len(new_path) - 1, new_path, via))
                queue.append((nxt, new_path))
        return out

    def edges(
        self, entity: str, *, direction: Literal["up", "down"], depth: int = 2
    ) -> list[LineageEdge]:
        items = self.walk(entity, direction=direction, depth=depth)
        out: list[LineageEdge] = []
        for item in items:
            a, b = item.path[-2], item.path[-1]
            src, dst = (a, b) if direction == "down" else (b, a)
            out.append(LineageEdge(source=src, target=dst, via=item.via))
        return out
