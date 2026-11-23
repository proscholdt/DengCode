from __future__ import annotations

from importlib.metadata import EntryPoint
from typing import Any, ClassVar

import pytest

from dengcode_core.errors import ConfigError
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind
from dengcode_engine.registry import AdapterRegistry, RegisteredAdapter


class _Dummy:
    info: ClassVar[AdapterInfo] = AdapterInfo(name="dummy", port=PortKind.STORAGE, version="1")

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> _Dummy:
        return cls(cfg)


def test_discovers_local_fs_from_installed_entry_points() -> None:
    registry = AdapterRegistry.discover()
    assert registry.has(PortKind.STORAGE, "local_fs")


def test_create_instantiates_by_name() -> None:
    registry = AdapterRegistry([RegisteredAdapter("dummy", None, _Dummy.info, _Dummy)])
    obj = registry.create(PortKind.STORAGE, "dummy", {"x": 1}, AdapterContext(workspace="t"))
    assert isinstance(obj, _Dummy)
    assert obj.cfg == {"x": 1}


def test_unknown_adapter_raises_config_error_with_hint() -> None:
    registry = AdapterRegistry([])
    with pytest.raises(ConfigError, match="não está instalado"):
        registry.get(PortKind.QUERY_ENGINE, "nao_existe")


def test_broken_entry_point_is_reported_not_raised() -> None:
    eps = [EntryPoint("quebrado", "modulo_que_nao_existe:X", "dengcode.adapters")]
    registry = AdapterRegistry.discover(lambda: eps)
    [item] = registry.all()
    assert not item.ok
    assert item.error is not None
    assert "ModuleNotFoundError" in item.error
