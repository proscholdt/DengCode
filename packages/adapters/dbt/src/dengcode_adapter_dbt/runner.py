"""Execução do dbt em subprocesso.

O dbt guarda estado global (logging, adapters, diretório atual) e não é seguro
chamá-lo de forma concorrente dentro de um app assíncrono. Um subprocesso com
``cwd`` no projeto dbt é isolado e pode ser cancelado (``kill``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dengcode_core.errors import AdapterError, ConfigError, TransientError

LOG_TAIL_CHARS = 4000

# o filho herda o ambiente do processo; estas chaves deixam a saída previsível
# (UTF-8 no Windows, sem cores, sem telemetria anônima)
_CHILD_ENV: dict[str, str] = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "NO_COLOR": "1",
    "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    "DO_NOT_TRACK": "1",
}


@dataclass(frozen=True)
class DbtInvocation:
    """Resultado bruto de uma chamada ao dbt."""

    args: tuple[str, ...]
    returncode: int
    output: str  # stdout + stderr intercalados

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, limit: int = LOG_TAIL_CHARS) -> str:
        return tail(self.output, limit)


def tail(text: str, limit: int = LOG_TAIL_CHARS) -> str:
    """Últimos ``limit`` caracteres, marcando o corte."""
    if len(text) <= limit:
        return text
    return "[...]\n" + text[-limit:]


def default_executable_name() -> str:
    return "dbt.exe" if os.name == "nt" else "dbt"


def find_dbt_executable(
    configured: str | None = None,
    *,
    python_executable: str | None = None,
) -> Path:
    """Localiza o executável do dbt.

    Ordem: valor configurado (caminho ou nome no PATH); ``dbt`` ao lado do
    Python em uso (``Scripts`` no Windows, ``bin`` nos demais); ``dbt`` no PATH.
    """
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate.resolve()
        found = shutil.which(configured)
        if found:
            return Path(found)
        raise ConfigError(f"executável do dbt não encontrado: {configured!r}")
    python = Path(python_executable or sys.executable)
    beside = python.parent / default_executable_name()
    if beside.is_file():
        return beside
    found = shutil.which("dbt")
    if found:
        return Path(found)
    raise ConfigError(
        "executável do dbt não encontrado (nem ao lado do Python nem no PATH); "
        "instale dbt-core ou configure 'dbt_executable'"
    )


async def run_dbt(
    executable: Path,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    logger: logging.Logger,
    extra_env: Mapping[str, str] | None = None,
) -> DbtInvocation:
    """Roda ``dbt <args>`` em ``cwd``; mata o processo em timeout ou cancelamento."""
    env = {**os.environ, **_CHILD_ENV, **(extra_env or {})}
    logger.debug("dbt: %s (cwd=%s)", " ".join(args), cwd)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(executable),
            *args,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise ConfigError(f"não foi possível iniciar o dbt ({executable}): {exc}") from exc
    except NotImplementedError as exc:  # laço de eventos sem suporte a subprocesso
        raise AdapterError(
            "o laço de eventos atual não suporta subprocessos (no Windows use o laço padrão)"
        ) from exc
    except OSError as exc:
        raise AdapterError(f"falha ao iniciar o dbt: {exc}") from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        await _kill(proc)
        raise TransientError(f"dbt {args[0] if args else ''} excedeu {timeout_s:g}s") from exc
    except asyncio.CancelledError:
        await _kill(proc)
        raise
    output = stdout.decode("utf-8", errors="replace").replace("\r\n", "\n")
    returncode = proc.returncode if proc.returncode is not None else -1
    logger.debug("dbt %s terminou com código %s", args[0] if args else "", returncode)
    return DbtInvocation(args=tuple(args), returncode=returncode, output=output)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    with contextlib.suppress(TimeoutError, OSError):
        await asyncio.wait_for(proc.wait(), timeout=10)
