"""StoragePort: listar e ler arquivos/objetos, inferir schema."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pyarrow as pa

from dengcode_core.ports.models import ObjectInfo, StorageCapabilities


@runtime_checkable
class StoragePort(Protocol):
    name: str

    def capabilities(self) -> StorageCapabilities: ...

    async def list(
        self, uri: str, *, recursive: bool = False, limit: int = 1000
    ) -> list[ObjectInfo]: ...

    async def read_sample(
        self, uri: str, *, rows: int = 100, fmt: str | None = None
    ) -> pa.Table: ...

    async def infer_schema(self, uri: str, *, fmt: str | None = None) -> pa.Schema: ...

    async def read_bytes(self, uri: str, *, max_bytes: int = 1_000_000) -> bytes: ...

    async def write_bytes(self, uri: str, data: bytes) -> None: ...
