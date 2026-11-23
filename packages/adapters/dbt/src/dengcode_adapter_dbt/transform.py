"""TransformPort sobre dbt-core (subprocesso).

Cada comando roda ``dbt`` com ``cwd`` no projeto e sempre com ``--project-dir``,
``--profiles-dir`` e ``--no-use-colors``. As chamadas ao mesmo projeto são
serializadas por um ``asyncio.Lock`` (o dbt escreve em ``target/`` e, no DuckDB,
só um processo pode escrever no arquivo do banco).

Caches:
- ``manifest.json`` é regerado (``dbt parse``) quando falta ou quando algum
  arquivo do projeto mudou depois dele;
- o SQL compilado vem de um ``dbt compile`` do projeto inteiro e vale enquanto
  os arquivos do projeto não mudarem.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, ClassVar, cast

from dengcode_adapter_dbt import manifest as mf
from dengcode_adapter_dbt.runner import DbtInvocation, find_dbt_executable, run_dbt, tail
from dengcode_core.errors import AdapterError, ConfigError, NotFoundError
from dengcode_core.ports.models import AdapterContext, AdapterInfo, LineageEdge, PortKind
from dengcode_core.ports.transform import ModelInfo, TransformRunResult

DEFAULT_TIMEOUT_S = 600.0

# pastas que o dbt gera ou que não fazem parte do código do projeto
_IGNORED_DIRS = frozenset({"target", "logs", "dbt_packages", "dbt_modules", "node_modules"})
# arquivos que podem mudar o manifest (modelos, macros, seeds, YAML de configuração)
_WATCHED_SUFFIXES = frozenset({".sql", ".yml", ".yaml", ".csv", ".py", ".md", ".jinja"})

Fingerprint = tuple[tuple[str, int, int], ...]


def project_fingerprint(project_dir: Path) -> Fingerprint:
    """(caminho relativo, mtime_ns, tamanho) dos arquivos que afetam o manifest."""
    entries: list[tuple[str, int, int]] = []
    for path in project_dir.rglob("*"):
        rel = path.relative_to(project_dir)
        if any(part in _IGNORED_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.suffix.lower() not in _WATCHED_SUFFIXES or not path.is_file():
            continue
        stat = path.stat()
        entries.append((rel.as_posix(), stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(entries))


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AdapterError(f"não foi possível ler {path.name}: {exc}") from exc
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


def _check_selector(selector: str) -> str:
    text = selector.strip()
    if not text:
        raise AdapterError("seletor vazio")
    if text.startswith("-"):  # impede injetar flags do dbt pelo seletor
        raise AdapterError(f"seletor inválido: {selector!r}")
    return text


class DbtTransform:
    """Projeto dbt local, executado pelo executável ``dbt`` do ambiente."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="dbt_core",
        port=PortKind.TRANSFORM,
        version="0.1.0",
        knowledge_pack="dengcode_adapter_dbt:knowledge/dbt.md",
    )
    name = "dbt_core"

    def __init__(
        self,
        project_dir: Path,
        *,
        profiles_dir: Path | None = None,
        target: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        dbt_executable: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ConfigError("timeout_s deve ser positivo")
        self.project_dir = project_dir.resolve()
        self.profiles_dir = (profiles_dir or project_dir).resolve()
        self.target = target or None
        self.timeout_s = timeout_s
        self._dbt_executable = dbt_executable
        self._log = logger or logging.getLogger("dengcode.adapter.dbt")
        self._lock = asyncio.Lock()
        self._executable: Path | None = None
        self._manifest: dict[str, Any] | None = None
        self._manifest_fp: Fingerprint | None = None
        self._compiled: dict[str, str] | None = None
        self._compiled_fp: Fingerprint | None = None

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> DbtTransform:
        base = ctx.project_dir or Path.cwd()
        raw_project = cfg.get("project_dir")
        if not raw_project:
            raise ConfigError("dbt_core: 'project_dir' é obrigatório")
        raw_profiles = cfg.get("profiles_dir")
        try:
            timeout_s = float(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"dbt_core: timeout_s inválido: {cfg.get('timeout_s')!r}") from exc
        executable = cfg.get("dbt_executable")
        target = cfg.get("target")
        return cls(
            base / Path(str(raw_project)),
            profiles_dir=base / Path(str(raw_profiles)) if raw_profiles else None,
            target=str(target) if target else None,
            timeout_s=timeout_s,
            dbt_executable=str(executable) if executable else None,
            logger=ctx.logger,
        )

    # ------------------------------------------------------------------ porta
    async def list_models(self, selector: str | None = None) -> list[ModelInfo]:
        async with self._lock:
            if selector is None or not selector.strip():
                return mf.model_infos(await self._fresh_manifest())
            selected = await self._ls(_check_selector(selector))
            manifest = await self._fresh_manifest()
        return [m for m in mf.model_infos(manifest) if m.unique_id in selected]

    async def compile(self, model: str) -> str:
        async with self._lock:
            fp = await asyncio.to_thread(project_fingerprint, self.project_dir)
            if self._compiled is None or self._compiled_fp != fp:
                await self._compile_all(fp)
            compiled = self._compiled or {}
        if model not in compiled:
            raise NotFoundError(f"modelo dbt não encontrado: {model!r}")
        return compiled[model]

    async def run(self, selector: str, *, target: str) -> TransformRunResult:
        return await self._execute("run", selector, target)

    async def test(self, selector: str, *, target: str) -> TransformRunResult:
        return await self._execute("test", selector, target)

    async def manifest_lineage(self) -> list[LineageEdge]:
        async with self._lock:
            manifest = await self._fresh_manifest()
        return mf.lineage_edges(manifest)

    # --------------------------------------------------------------- internos
    def _check_project(self) -> Path:
        if not (self.project_dir / "dbt_project.yml").is_file():
            raise ConfigError(f"projeto dbt não encontrado em {self.project_dir}")
        if self._executable is None:
            self._executable = find_dbt_executable(self._dbt_executable)
        return self._executable

    def _base_args(self, command: str, target: str | None) -> list[str]:
        args = [
            command,
            "--project-dir",
            str(self.project_dir),
            "--profiles-dir",
            str(self.profiles_dir),
            "--no-use-colors",
            "--no-version-check",
        ]
        chosen = target or self.target
        if chosen:
            args += ["--target", chosen]
        return args

    async def _invoke(self, args: list[str]) -> DbtInvocation:
        executable = self._check_project()
        return await run_dbt(
            executable, args, cwd=self.project_dir, timeout_s=self.timeout_s, logger=self._log
        )

    @property
    def _target_dir(self) -> Path:
        return self.project_dir / "target"

    async def _fresh_manifest(self) -> dict[str, Any]:
        """Manifest atual; roda ``dbt parse`` se faltar ou estiver desatualizado."""
        self._check_project()
        fp = await asyncio.to_thread(project_fingerprint, self.project_dir)
        if self._manifest is not None and self._manifest_fp == fp:
            return self._manifest
        path = self._target_dir / "manifest.json"
        # arquivos mudaram desde o último parse nosso, ou o manifest em disco é
        # mais velho que algum arquivo do projeto (ou não existe)
        changed = self._manifest_fp is not None and self._manifest_fp != fp
        if changed or not await asyncio.to_thread(self._manifest_is_fresh, fp):
            inv = await self._invoke(self._base_args("parse", None))
            if not inv.ok:
                raise AdapterError(f"dbt parse falhou:\n{inv.tail()}")
        manifest = await asyncio.to_thread(_load_json, path)
        if manifest is None:
            raise AdapterError("dbt não gerou target/manifest.json")
        self._manifest, self._manifest_fp = manifest, fp
        return manifest

    def _forget_manifest(self) -> None:
        self._manifest = None
        self._manifest_fp = None

    def _manifest_is_fresh(self, fp: Fingerprint) -> bool:
        path = self._target_dir / "manifest.json"
        if not path.is_file():
            return False
        newest = max((mtime for _, mtime, _ in fp), default=0)
        return path.stat().st_mtime_ns >= newest

    async def _ls(self, selector: str) -> set[str]:
        args = [
            *self._base_args("ls", None),
            "--quiet",
            "--select",
            selector,
            "--resource-type",
            "model",
            "--output",
            "json",
            "--output-keys",
            "unique_id",
        ]
        inv = await self._invoke(args)
        if not inv.ok:
            raise AdapterError(f"dbt ls falhou para o seletor {selector!r}:\n{inv.tail()}")
        selected: set[str] = set()
        for line in inv.output.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                uid = cast(dict[str, Any], item).get("unique_id")
                if isinstance(uid, str):
                    selected.add(uid)
        # o ls também reescreve o manifest: recarrega do disco na próxima leitura
        self._forget_manifest()
        return selected

    async def _compile_all(self, fp: Fingerprint) -> None:
        inv = await self._invoke(self._base_args("compile", None))
        if not inv.ok:
            raise AdapterError(f"dbt compile falhou:\n{inv.tail()}")
        manifest = await asyncio.to_thread(_load_json, self._target_dir / "manifest.json")
        if manifest is None:
            raise AdapterError("dbt compile não gerou target/manifest.json")
        self._compiled, self._compiled_fp = mf.compiled_models(manifest), fp
        self._manifest, self._manifest_fp = manifest, fp

    async def _execute(self, command: str, selector: str, target: str) -> TransformRunResult:
        sel = _check_selector(selector)
        chosen = target or self.target
        async with self._lock:
            results_path = self._target_dir / "run_results.json"
            # sem resultado velho: se o dbt falhar antes de executar, não há nós
            await asyncio.to_thread(results_path.unlink, missing_ok=True)
            self._log.info("dbt %s --select %s (target=%s)", command, sel, chosen or "padrão")
            inv = await self._invoke([*self._base_args(command, chosen), "--select", sel])
            run_results = await asyncio.to_thread(_load_json, results_path)
            manifest = await asyncio.to_thread(_load_json, self._target_dir / "manifest.json")
            # o comando reescreve o manifest: recarrega do disco na próxima leitura
            self._forget_manifest()
        results = mf.node_results(run_results, manifest) if run_results is not None else []
        return TransformRunResult(
            success=inv.ok,
            command=command,
            selector=sel,
            target=chosen,
            results=results,
            logs=tail(inv.output),
        )
