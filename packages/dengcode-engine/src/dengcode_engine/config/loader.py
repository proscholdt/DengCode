"""Localiza, herda (``extends``) e valida stacks.

Resolução por nome: projeto (``<repo>/.dengcode/stacks``) > usuário
(``~/.dengcode/stacks``) > embutidos (pacote ``dengcode_engine.stacks``).
Herança: merge profundo de dicionários; listas são substituídas; ``null``
explícito remove a porta herdada; ciclo = erro.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from dengcode_core.errors import ConfigError
from dengcode_engine.config.schema import StackConfig
from dengcode_engine.paths import dengcode_home, project_state_dir

Origin = str  # "projeto" | "usuário" | "embutido"


@dataclass(frozen=True)
class StackSource:
    name: str
    origin: Origin
    location: str


def _read_yaml(text: str, where: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML inválido em {where}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{where} não contém um mapa YAML")
    return data  # type: ignore[return-value]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if value is None:
            out[key] = None
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)  # type: ignore[arg-type]
        else:
            out[key] = value
    return out


def merge_llm(base: dict[str, Any] | None, override: dict[str, Any]) -> dict[str, Any]:
    """Merge da seção ``llm``: trocar de provedor recomeça a seção (chaves de um
    adapter não valem para outro); ``model`` e ``models`` substituem um ao outro."""
    if not base or override.get("provider", base.get("provider")) != base.get("provider"):
        return dict(override)
    base = dict(base)
    if "model" in override:
        base.pop("models", None)
    if "models" in override:
        base.pop("model", None)
    return deep_merge(base, override)


def merge_layers(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deep_merge(base, {k: v for k, v in override.items() if k != "llm"})
    if "llm" in override:
        llm = override["llm"]
        out["llm"] = merge_llm(base.get("llm"), llm) if isinstance(llm, dict) else llm
    return out


class StackLoader:
    def __init__(self, project_dir: Path | None = None, home: Path | None = None) -> None:
        self.project_dir = project_dir
        self.home = home or dengcode_home()

    def _dirs(self) -> list[tuple[Origin, Path]]:
        dirs: list[tuple[Origin, Path]] = []
        if self.project_dir is not None:
            dirs.append(("projeto", project_state_dir(self.project_dir) / "stacks"))
        dirs.append(("usuário", self.home / "stacks"))
        return dirs

    def _raw(self, name: str) -> tuple[dict[str, Any], StackSource]:
        for origin, folder in self._dirs():
            for suffix in (".yaml", ".yml"):
                path = folder / f"{name}{suffix}"
                if path.is_file():
                    text = path.read_text(encoding="utf-8")
                    return _read_yaml(text, str(path)), StackSource(name, origin, str(path))
        builtin = files("dengcode_engine.stacks").joinpath(f"{name}.yaml")
        if builtin.is_file():
            text = builtin.read_text(encoding="utf-8")
            return _read_yaml(text, f"embutido:{name}"), StackSource(name, "embutido", name)
        raise ConfigError(f"stack não encontrada: {name} (veja `dengcode stack list`)")

    def list(self) -> list[StackSource]:
        seen: dict[str, StackSource] = {}
        for item in files("dengcode_engine.stacks").iterdir():
            if item.name.endswith(".yaml"):
                n = item.name.removesuffix(".yaml")
                seen[n] = StackSource(n, "embutido", n)
        for origin, folder in reversed(self._dirs()):
            if folder.is_dir():
                for path in sorted(folder.glob("*.y*ml")):
                    seen[path.stem] = StackSource(path.stem, origin, str(path))
        return sorted(seen.values(), key=lambda s: s.name)

    def resolve_raw(self, name: str) -> dict[str, Any]:
        chain: list[str] = []
        current: str | None = name
        layers: list[dict[str, Any]] = []
        while current is not None:
            if current in chain:
                cycle = " → ".join([*chain, current])
                raise ConfigError(f"ciclo de extends: {cycle}")
            chain.append(current)
            raw, _ = self._raw(current)
            layers.append(raw)
            parent = raw.get("extends")
            current = str(parent) if parent else None
        merged: dict[str, Any] = {}
        for layer in reversed(layers):
            merged = merge_layers(merged, layer)
        merged["name"] = name
        merged["extends"] = layers[0].get("extends")
        # null explícito remove a porta: o schema usa os padrões no lugar
        return {k: v for k, v in merged.items() if v is not None or k in ("extends",)}

    def load(self, name: str) -> StackConfig:
        raw = self.resolve_raw(name)
        try:
            return StackConfig.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"stack {name} inválida:\n{exc}") from exc

    def source(self, name: str) -> StackSource:
        return self._raw(name)[1]
