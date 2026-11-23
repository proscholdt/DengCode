"""StoragePort para o disco local (``file://`` ou caminho comum)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq

from dengcode_core.errors import AdapterError, NotFoundError, UnsupportedOperation
from dengcode_core.ports.models import (
    AdapterContext,
    AdapterInfo,
    ObjectInfo,
    PortKind,
    StorageCapabilities,
)

_FORMATS = {
    ".csv": "csv",
    ".tsv": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}


class LocalFsStorage:
    """Lê arquivos do disco. Caminhos relativos partem de ``root``."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="local_fs", port=PortKind.STORAGE, version="0.1.0"
    )
    name = "local_fs"

    def __init__(self, root: Path, *, allow_write: bool = False) -> None:
        self._root = root.resolve()
        self._allow_write = allow_write

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> LocalFsStorage:
        base = ctx.project_dir or Path.cwd()
        root = Path(cfg.get("root", ".")).expanduser()
        return cls(
            root if root.is_absolute() else base / root,
            allow_write=bool(cfg.get("allow_write", False)),
        )

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            supports_write=self._allow_write,
            supports_list_recursive=True,
            table_formats=frozenset({"csv", "parquet", "json"}),
        )

    # ------------------------------------------------------------------ util
    def _path(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme == "file":
            raw = url2pathname(unquote(parsed.path))
            # file:///C:/x vira "\C:\x" no Windows; remove a barra inicial
            if len(raw) > 2 and raw[0] in "\\/" and raw[2] == ":":
                raw = raw[1:]
            path = Path(raw)
        elif parsed.scheme and len(parsed.scheme) > 1:
            raise AdapterError(f"esquema não suportado pelo local_fs: {parsed.scheme}://")
        else:
            path = Path(uri)
        return (path if path.is_absolute() else self._root / path).resolve()

    @staticmethod
    def _info(path: Path) -> ObjectInfo:
        st = path.stat()
        return ObjectInfo(
            uri=path.as_uri(),
            size_bytes=None if path.is_dir() else st.st_size,
            modified_at=datetime.fromtimestamp(st.st_mtime, tz=UTC),
            is_dir=path.is_dir(),
        )

    def _existing(self, uri: str) -> Path:
        path = self._path(uri)
        if not path.exists():
            raise NotFoundError(f"não encontrado: {uri}")
        return path

    @staticmethod
    def _fmt(path: Path, fmt: str | None) -> str:
        if fmt:
            return fmt
        found = _FORMATS.get(path.suffix.lower())
        if found is None:
            raise UnsupportedOperation(f"formato não reconhecido para {path.name}; informe fmt")
        return found

    # ------------------------------------------------------------------ porta
    async def list(
        self, uri: str, *, recursive: bool = False, limit: int = 1000
    ) -> list[ObjectInfo]:
        def run() -> list[ObjectInfo]:
            path = self._existing(uri)
            if path.is_file():
                return [self._info(path)]
            it = path.rglob("*") if recursive else path.iterdir()
            out: list[ObjectInfo] = []
            for child in sorted(it):
                out.append(self._info(child))
                if len(out) >= limit:
                    break
            return out

        return await asyncio.to_thread(run)

    async def read_bytes(self, uri: str, *, max_bytes: int = 1_000_000) -> bytes:
        def run() -> bytes:
            path = self._existing(uri)
            if path.is_dir():
                raise AdapterError(f"é um diretório: {uri}")
            with path.open("rb") as fh:
                return fh.read(max_bytes)

        return await asyncio.to_thread(run)

    def _read_table(self, path: Path, fmt: str, rows: int | None) -> pa.Table:
        if path.is_dir():
            files = sorted(p for p in path.iterdir() if p.suffix.lower() in _FORMATS)
            if not files:
                raise NotFoundError(f"nenhum arquivo tabular em {path}")
            path = files[0]
        try:
            if fmt == "parquet":
                pf = pq.ParquetFile(path)
                if rows is None:
                    return pf.read()
                batches = pf.iter_batches(batch_size=max(rows, 1))
                first = next(batches, None)
                return (
                    pa.Table.from_batches([first]).slice(0, rows)
                    if first
                    else pf.schema_arrow.empty_table()
                )
            if fmt == "csv":
                delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
                table = pacsv.read_csv(path, parse_options=pacsv.ParseOptions(delimiter=delimiter))
            elif fmt == "json":
                table = pajson.read_json(path)
            else:
                raise UnsupportedOperation(f"formato não suportado: {fmt}")
        except (pa.ArrowInvalid, OSError) as exc:
            raise AdapterError(f"falha ao ler {path.name}: {exc}") from exc
        return table if rows is None else table.slice(0, rows)

    async def read_sample(self, uri: str, *, rows: int = 100, fmt: str | None = None) -> pa.Table:
        def run() -> pa.Table:
            path = self._existing(uri)
            return self._read_table(
                path, self._fmt(path, fmt) if path.is_file() else fmt or "csv", rows
            )

        return await asyncio.to_thread(run)

    async def infer_schema(self, uri: str, *, fmt: str | None = None) -> pa.Schema:
        def run() -> pa.Schema:
            path = self._existing(uri)
            if path.is_file() and self._fmt(path, fmt) == "parquet":
                return pq.read_schema(path)
            return self._read_table(
                path, self._fmt(path, fmt) if path.is_file() else fmt or "csv", 1000
            ).schema

        return await asyncio.to_thread(run)

    async def write_bytes(self, uri: str, data: bytes) -> None:
        if not self._allow_write:
            raise UnsupportedOperation("local_fs configurado sem escrita (allow_write: false)")

        def run() -> None:
            path = self._path(uri)
            if self._root not in path.parents and path != self._root:
                raise AdapterError("escrita fora da raiz do storage")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(run)
