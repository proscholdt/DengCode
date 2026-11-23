"""Conjunto de ferramentas genéricas (arquivos e shell)."""

from __future__ import annotations

from typing import Any

from dengcode_core.tools.base import Tool
from dengcode_core.tools.files import FileEdit, FileList, FileRead, FileSearch, FileWrite
from dengcode_core.tools.shell import ShellRun


def builtin_tools() -> list[Tool[Any]]:
    return [FileRead(), FileList(), FileSearch(), FileEdit(), FileWrite(), ShellRun()]
