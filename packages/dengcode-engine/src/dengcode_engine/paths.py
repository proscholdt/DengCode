"""Caminhos do DengCode no disco (portáveis: sempre ``pathlib``)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from dengcode_core.errors import ConfigError

_WS_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def dengcode_home() -> Path:
    """``$DENGCODE_HOME`` ou ``~/.dengcode``."""
    raw = os.environ.get("DENGCODE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".dengcode"


def validate_workspace_name(name: str) -> str:
    if not _WS_NAME.match(name):
        raise ConfigError(
            f"nome de workspace inválido: {name!r} (use minúsculas, números, '-' ou '_')"
        )
    return name


def workspace_dir(workspace: str, home: Path | None = None) -> Path:
    return (home or dengcode_home()) / "workspaces" / validate_workspace_name(workspace)


def sessions_dir(workspace: str, home: Path | None = None) -> Path:
    return workspace_dir(workspace, home) / "sessions"


def project_state_dir(project_dir: Path) -> Path:
    return project_dir / ".dengcode"


def _config_path(home: Path | None = None) -> Path:
    return (home or dengcode_home()) / "config.yaml"


def _read_config(home: Path | None) -> dict[str, object]:
    path = _config_path(home)
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(loaded) if isinstance(loaded, dict) else {}  # pyright: ignore[reportUnknownArgumentType]


def current_workspace(home: Path | None = None) -> str:
    """Workspace ativo (``dengcode workspace use``); padrão ``default``."""
    name = _read_config(home).get("workspace")
    return validate_workspace_name(name) if isinstance(name, str) and name else "default"


def set_current_workspace(name: str, home: Path | None = None) -> None:
    validate_workspace_name(name)
    data = _read_config(home)
    data["workspace"] = name
    path = _config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def list_workspaces(home: Path | None = None) -> list[str]:
    base = (home or dengcode_home()) / "workspaces"
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
