"""StoragePort sobre a API S3 (MinIO e endpoints compatíveis), via ``obstore``."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, override
from urllib.parse import urlparse

import obstore as obs
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.json as pajson
import pyarrow.parquet as pq
from obstore.store import S3Store

from dengcode_adapter_minio.errors import is_invalid_range, map_error, redact
from dengcode_adapter_minio.uri import S3Location, parse_uri
from dengcode_core.errors import (
    AdapterError,
    ConfigError,
    DengCodeError,
    NotFoundError,
    UnsupportedOperation,
)
from dengcode_core.ports.models import (
    AdapterContext,
    AdapterInfo,
    ObjectInfo,
    PortKind,
    StorageCapabilities,
)

if TYPE_CHECKING:
    from obstore import ObjectMeta
    from obstore.store import S3Config

_FORMATS = {
    ".csv": "csv",
    ".tsv": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".json": "json",
    ".jsonl": "json",
    ".ndjson": "json",
}
MAX_SAMPLE_BYTES = 32 * 1024 * 1024
"""Teto de bytes baixados para amostrar CSV/JSON (e Parquet pequeno)."""
_SCHEMA_ROWS = 1000
_PREFIX_SCAN = 1000


def _suffix(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _fmt_of(key: str, fmt: str | None) -> str:
    if fmt:
        return fmt.lower()
    found = _FORMATS.get(_suffix(key))
    if found is None:
        raise UnsupportedOperation(f"formato não reconhecido para {key!r}; informe fmt")
    return found


class _RangeFile(io.RawIOBase):
    """Arquivo somente leitura que busca faixas do objeto sob demanda (Parquet grande)."""

    def __init__(self, store: S3Store, key: str, size: int) -> None:
        super().__init__()
        self._store = store
        self._key = key
        self._size = size
        self._pos = 0

    @override
    def readable(self) -> bool:
        return True

    @override
    def seekable(self) -> bool:
        return True

    @override
    def tell(self) -> int:
        return self._pos

    @override
    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    @override
    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        length = min(len(view), self._size - self._pos)
        if length <= 0:
            return 0
        data = obs.get_range(self._store, self._key, start=self._pos, length=length)
        chunk = bytes(data)
        view[: len(chunk)] = chunk
        self._pos += len(chunk)
        return len(chunk)


class MinioStorage:
    """Lê (e, se permitido, grava) objetos num endpoint S3 como o MinIO."""

    info: ClassVar[AdapterInfo] = AdapterInfo(name="minio", port=PortKind.STORAGE, version="0.1.0")
    name = "minio"

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
        bucket: str | None = None,
        allow_write: bool = False,
        timeout_s: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigError(
                f"endpoint inválido para o minio (use http(s)://host:porta): {endpoint!r}"
            )
        self._endpoint = endpoint.rstrip("/")
        self._allow_http = parsed.scheme == "http"
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._bucket = bucket
        self._allow_write = allow_write
        self._timeout = timedelta(seconds=timeout_s)
        self._max_retries = max_retries
        self._stores: dict[str, S3Store] = {}

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> MinioStorage:
        endpoint = cfg.get("endpoint")
        if not endpoint:
            raise ConfigError("minio: 'endpoint' é obrigatório (ex.: http://localhost:9000)")
        storage = cls(
            endpoint=str(endpoint),
            access_key=_opt_str(cfg.get("access_key")),
            secret_key=_opt_str(cfg.get("secret_key")),
            region=str(cfg.get("region") or "us-east-1"),
            bucket=_opt_str(cfg.get("bucket")),
            allow_write=bool(cfg.get("allow_write", False)),
            timeout_s=float(cfg.get("timeout_s", 30.0)),
        )
        ctx.logger.debug(
            "storage minio configurado",
            extra={"endpoint": storage._endpoint, "bucket": storage._bucket},
        )
        return storage

    @override
    def __repr__(self) -> str:
        return f"MinioStorage(endpoint={self._endpoint!r}, bucket={self._bucket!r})"

    def capabilities(self) -> StorageCapabilities:
        return StorageCapabilities(
            supports_write=self._allow_write,
            supports_list_recursive=True,
            table_formats=frozenset({"csv", "parquet", "json"}),
        )

    # ------------------------------------------------------------------ util
    def _loc(self, uri: str) -> S3Location:
        return parse_uri(uri, default_bucket=self._bucket)

    def _store(self, bucket: str) -> S3Store:
        store = self._stores.get(bucket)
        if store is None:
            try:
                store = S3Store(
                    bucket,
                    config=self._s3_config(),
                    client_options={
                        "allow_http": self._allow_http,
                        "timeout": self._timeout,
                        "connect_timeout": min(self._timeout, timedelta(seconds=10)),
                    },
                    retry_config={
                        "max_retries": self._max_retries,
                        "retry_timeout": self._timeout,
                        "backoff": {
                            "init_backoff": timedelta(milliseconds=100),
                            "max_backoff": timedelta(seconds=2),
                            "base": 2,
                        },
                    },
                )
            except Exception as exc:
                raise ConfigError(f"configuração S3 inválida: {self._redact(exc)}") from None
            self._stores[bucket] = store
        return store

    def _s3_config(self) -> S3Config:
        config: S3Config = {
            "endpoint": self._endpoint,
            "region": self._region,
            "virtual_hosted_style_request": False,
        }
        if self._access_key and self._secret_key:
            config["access_key_id"] = self._access_key
            config["secret_access_key"] = self._secret_key
        else:
            # sem credenciais: acesso anônimo (bucket público), sem buscar em metadados
            config["skip_signature"] = True
        return config

    def _redact(self, exc: BaseException) -> str:
        return redact(str(exc), (self._access_key, self._secret_key))

    async def _call[T](self, uri: str, op: Callable[[], Awaitable[T]]) -> T:
        try:
            return await op()
        except DengCodeError:
            raise
        except Exception as exc:
            raise map_error(exc, uri, secrets=(self._access_key, self._secret_key)) from None

    @staticmethod
    def _object_info(bucket: str, meta: ObjectMeta) -> ObjectInfo:
        return ObjectInfo(
            uri=S3Location(bucket, meta["path"]).uri(),
            size_bytes=meta["size"],
            modified_at=meta["last_modified"],
            is_dir=False,
        )

    async def _head(self, loc: S3Location) -> ObjectMeta | None:
        if not loc.key:
            return None
        try:
            return await obs.head_async(self._store(loc.bucket), loc.key)
        except FileNotFoundError:
            return None

    async def _first_tabular(self, loc: S3Location, fmt: str | None) -> ObjectMeta:
        """Primeiro objeto tabular (ordem lexicográfica) sob o prefixo."""
        wanted = fmt.lower() if fmt else None
        scanned = 0
        stream = obs.list(self._store(loc.bucket), loc.key or None, chunk_size=200)
        async for chunk in stream:
            for meta in sorted(chunk, key=lambda m: m["path"]):
                found = _FORMATS.get(_suffix(meta["path"]))
                if found and (wanted is None or found == wanted) and meta["size"] > 0:
                    return meta
                scanned += 1
            if scanned >= _PREFIX_SCAN:
                break
        raise NotFoundError(f"nenhum arquivo tabular em {loc.uri(is_dir=True)}")

    async def _resolve_table(self, uri: str, fmt: str | None) -> tuple[S3Location, ObjectMeta]:
        loc = self._loc(uri)
        meta = await self._head(loc)
        if meta is None:
            meta = await self._first_tabular(loc, fmt)
        return S3Location(loc.bucket, meta["path"]), meta

    async def _load_table(self, uri: str, fmt: str | None, rows: int | None) -> pa.Table:
        loc, meta = await self._resolve_table(uri, fmt)
        kind = _fmt_of(loc.key, fmt)
        store = self._store(loc.bucket)
        size = meta["size"]
        if kind == "parquet" and size > MAX_SAMPLE_BYTES:
            # Parquet grande: lê só o rodapé e o primeiro grupo de linhas por faixas
            return await asyncio.to_thread(
                _parse_parquet, pa.PythonFile(_RangeFile(store, loc.key, size), mode="r"), rows
            )
        limit = min(size, MAX_SAMPLE_BYTES)
        data = (
            bytes(await obs.get_range_async(store, loc.key, start=0, length=limit))
            if limit
            else b""
        )
        truncated = size > limit
        return await asyncio.to_thread(_parse, data, kind, _suffix(loc.key), rows, truncated)

    # ------------------------------------------------------------------ porta
    async def list(
        self, uri: str, *, recursive: bool = False, limit: int = 1000
    ) -> list[ObjectInfo]:
        loc = self._loc(uri)

        async def run() -> list[ObjectInfo]:
            store = self._store(loc.bucket)
            out: list[ObjectInfo] = []
            if recursive:
                chunk_size = min(max(limit, 1), 1000)
                async for chunk in obs.list(store, loc.key or None, chunk_size=chunk_size):
                    out.extend(self._object_info(loc.bucket, m) for m in chunk)
                    if len(out) >= limit:
                        break
            else:
                result = await obs.list_with_delimiter_async(store, loc.key or None)
                dirs = [
                    ObjectInfo(
                        uri=S3Location(loc.bucket, p.rstrip("/")).uri(is_dir=True),
                        size_bytes=None,
                        modified_at=None,
                        is_dir=True,
                    )
                    for p in result["common_prefixes"]
                ]
                files = [self._object_info(loc.bucket, m) for m in result["objects"]]
                out = sorted(dirs + files, key=lambda o: o.uri)
            if not out and loc.key:
                meta = await self._head(loc)
                if meta is None:
                    raise NotFoundError(f"não encontrado: {loc.uri()}")
                out = [self._object_info(loc.bucket, meta)]
            return out[:limit]

        return await self._call(uri, run)

    async def read_bytes(self, uri: str, *, max_bytes: int = 1_000_000) -> bytes:
        loc = self._loc(uri)
        if not loc.key:
            raise AdapterError(f"é um bucket, não um objeto: {uri}")

        async def run() -> bytes:
            store = self._store(loc.bucket)
            if max_bytes <= 0:
                await obs.head_async(store, loc.key)
                return b""
            try:
                return bytes(await obs.get_range_async(store, loc.key, start=0, length=max_bytes))
            except Exception as exc:
                if not is_invalid_range(exc):
                    raise
                # faixa recusada (objeto vazio): confirma a existência e devolve vazio
                meta = await obs.head_async(store, loc.key)
                if meta["size"] == 0:
                    return b""
                raise

        return await self._call(uri, run)

    async def read_sample(self, uri: str, *, rows: int = 100, fmt: str | None = None) -> pa.Table:
        return await self._call(uri, lambda: self._load_table(uri, fmt, max(rows, 0)))

    async def infer_schema(self, uri: str, *, fmt: str | None = None) -> pa.Schema:
        async def run() -> pa.Schema:
            table = await self._load_table(uri, fmt, _SCHEMA_ROWS)
            return table.schema

        return await self._call(uri, run)

    async def write_bytes(self, uri: str, data: bytes) -> None:
        if not self._allow_write:
            raise UnsupportedOperation("minio configurado sem escrita (allow_write: false)")
        loc = self._loc(uri)
        if not loc.key:
            raise AdapterError(f"informe a chave do objeto a gravar: {uri}")

        async def run() -> None:
            await obs.put_async(self._store(loc.bucket), loc.key, data)

        await self._call(uri, run)


def _opt_str(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def _parse_parquet(source: Any, rows: int | None) -> pa.Table:
    pf = pq.ParquetFile(source)
    if rows is None:
        return pf.read()
    first = next(pf.iter_batches(batch_size=max(rows, 1)), None)
    if first is None:
        return pf.schema_arrow.empty_table()
    return pa.Table.from_batches([first]).slice(0, rows)


def _parse(data: bytes, fmt: str, suffix: str, rows: int | None, truncated: bool) -> pa.Table:
    """Interpreta bytes já baixados; se truncados, descarta a última linha parcial."""
    try:
        if fmt == "parquet":
            return _parse_parquet(pa.BufferReader(data), rows)
        if fmt not in ("csv", "json"):
            raise UnsupportedOperation(f"formato não suportado: {fmt}")
        if truncated:
            cut = data.rfind(b"\n")
            data = data[: cut + 1] if cut >= 0 else data
        buf = io.BytesIO(data)
        if fmt == "csv":
            delimiter = "\t" if suffix == ".tsv" else ","
            table = pacsv.read_csv(buf, parse_options=pacsv.ParseOptions(delimiter=delimiter))
        else:
            table = pajson.read_json(buf)
    except pa.ArrowException as exc:
        raise AdapterError(f"falha ao interpretar {fmt}: {exc}") from None
    return table if rows is None else table.slice(0, rows)
