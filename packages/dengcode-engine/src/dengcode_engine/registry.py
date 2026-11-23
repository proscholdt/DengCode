"""Registro de adapters descobertos por entry points (grupo ``dengcode.adapters``).

O engine nunca importa um adapter diretamente: tudo passa por aqui.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any, ClassVar, Protocol, cast

from dengcode_core.errors import ConfigError
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind

ENTRY_POINT_GROUP = "dengcode.adapters"

log = logging.getLogger(__name__)


class AdapterClass(Protocol):
    """Forma mínima que a classe de um adapter expõe."""

    info: ClassVar[AdapterInfo]

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> Any: ...


@dataclass(frozen=True)
class RegisteredAdapter:
    entry_point: str
    distribution: str | None
    info: AdapterInfo | None
    cls: Any  # a classe do adapter; o registro a expõe como AdapterClass
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.cls is not None and self.info is not None


def _load(ep: EntryPoint) -> RegisteredAdapter:
    dist = ep.dist.name if ep.dist is not None else None
    try:
        obj = ep.load()
    except Exception as exc:  # adapter quebrado não pode derrubar o motor
        log.warning("adapter %s não carregou: %s", ep.name, exc)
        return RegisteredAdapter(ep.name, dist, None, None, f"{type(exc).__name__}: {exc}")
    info = getattr(obj, "info", None)
    if not isinstance(info, AdapterInfo) or not hasattr(obj, "from_config"):
        msg = "entry point não expõe `info: AdapterInfo` e `from_config`"
        return RegisteredAdapter(ep.name, dist, None, None, msg)
    return RegisteredAdapter(ep.name, dist, info, obj)


class AdapterRegistry:
    """Índice (porta, nome) → classe de adapter."""

    def __init__(self, adapters: Iterable[RegisteredAdapter]) -> None:
        self._all = list(adapters)
        self._index: dict[tuple[PortKind, str], RegisteredAdapter] = {}
        for item in self._all:
            if item.ok and item.info is not None:
                self._index[(item.info.port, item.info.name)] = item

    @classmethod
    def discover(cls, loader: Callable[[], Iterable[EntryPoint]] | None = None) -> AdapterRegistry:
        eps = loader() if loader else entry_points(group=ENTRY_POINT_GROUP)
        return cls(_load(ep) for ep in eps)

    def all(self) -> list[RegisteredAdapter]:
        return list(self._all)

    def available(self, port: PortKind | None = None) -> list[AdapterInfo]:
        infos = [a.info for a in self._index.values() if a.info is not None]
        return sorted(
            (i for i in infos if port is None or i.port == port),
            key=lambda i: (i.port.value, i.name),
        )

    def has(self, port: PortKind, name: str) -> bool:
        return (port, name) in self._index

    def get(self, port: PortKind, name: str) -> AdapterClass:
        item = self._index.get((port, name))
        if item is None or item.cls is None:
            known = ", ".join(i.name for i in self.available(port)) or "nenhum"
            raise ConfigError(
                f"adapter '{name}' para a porta '{port.value}' não está instalado "
                f"(disponíveis: {known}). Instale o pacote/extra correspondente, "
                f'ex.: uv tool install "dengcode[oss]".'
            )
        return cast(AdapterClass, item.cls)

    def create(self, port: PortKind, name: str, cfg: dict[str, Any], ctx: AdapterContext) -> Any:
        return self.get(port, name).from_config(cfg, ctx)
