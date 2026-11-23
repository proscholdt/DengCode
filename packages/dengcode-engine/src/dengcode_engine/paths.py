"""Caminhos do DengCode no disco (portáveis: sempre ``pathlib``)."""

from __future__ import annotations

import os
import re
from pathlib import Path

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
