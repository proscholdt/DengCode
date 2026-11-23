from __future__ import annotations

from pathlib import Path

import pytest
from dengcode_adapter_local_fs.storage import LocalFsStorage

from dengcode_core.errors import NotFoundError, UnsupportedOperation


@pytest.fixture
def storage(tmp_path: Path) -> LocalFsStorage:
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.csv").write_text("id,nome\n1,Ana\n2,Bruno\n", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("olá", encoding="utf-8")
    return LocalFsStorage(tmp_path)


async def test_list_relative_and_recursive(storage: LocalFsStorage) -> None:
    top = await storage.list(".")
    assert {o.uri.rsplit("/", 1)[-1] for o in top} == {"a.csv", "sub"}
    deep = await storage.list(".", recursive=True)
    assert any(o.uri.endswith("b.txt") for o in deep)


async def test_read_bytes_accepts_file_uri(storage: LocalFsStorage, tmp_path: Path) -> None:
    data = await storage.read_bytes((tmp_path / "sub" / "b.txt").as_uri())
    assert data.decode("utf-8") == "olá"


async def test_read_bytes_respects_max(storage: LocalFsStorage) -> None:
    assert await storage.read_bytes("a.csv", max_bytes=2) == b"id"


async def test_missing_raises_not_found(storage: LocalFsStorage) -> None:
    with pytest.raises(NotFoundError):
        await storage.read_bytes("nada.csv")


async def test_sample_and_schema(storage: LocalFsStorage) -> None:
    table = await storage.read_sample("a.csv", rows=1)
    assert table.num_rows == 1
    schema = await storage.infer_schema("a.csv")
    assert schema.names == ["id", "nome"]


async def test_write_disabled_by_default(storage: LocalFsStorage) -> None:
    with pytest.raises(UnsupportedOperation):
        await storage.write_bytes("x.txt", b"1")
