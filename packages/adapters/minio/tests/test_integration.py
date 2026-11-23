"""Contrato do StoragePort contra um MinIO real (testcontainers).

Precisa de Docker: sem ele, os testes são pulados.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from dengcode_adapter_minio import storage as storage_module
from dengcode_adapter_minio.storage import MinioStorage
from dengcode_core.errors import AuthError, NotFoundError
from dengcode_testkit.contracts.data import SAMPLE_CSV, StorageContract

pytestmark = pytest.mark.integration

MINIO_IMAGE = "pgsty/minio:RELEASE.2026-08-04T00-00-00Z"
BUCKET = "dengcode-teste"
ACCESS_KEY = "dengcode"
SECRET_KEY = "dengcode-teste-123"


@dataclass(frozen=True)
class MinioServer:
    endpoint: str


def _parquet_bytes() -> bytes:
    sink = io.BytesIO()
    pq.write_table(pa.table({"id": [1, 2, 3], "valor": [10.5, 20.0, 7.25]}), sink)
    return sink.getvalue()


SEED: dict[str, bytes] = {
    "dados/clientes.csv": SAMPLE_CSV,
    "dados/vendas/2024/pedidos.parquet": _parquet_bytes(),
    "dados/eventos.jsonl": b'{"id": 1, "tipo": "clique"}\n{"id": 2, "tipo": "compra"}\n',
    "dados/vazio.txt": b"",
    "leiame.txt": b"ola",
}


@pytest.fixture(scope="module")
def minio_server() -> Iterator[MinioServer]:
    try:
        from testcontainers.community.minio import MinioContainer

        container = MinioContainer(image=MINIO_IMAGE, access_key=ACCESS_KEY, secret_key=SECRET_KEY)
        container.with_env("MINIO_ROOT_USER", ACCESS_KEY)
        container.with_env("MINIO_ROOT_PASSWORD", SECRET_KEY)
        container.start()
    except Exception as exc:  # sem Docker, sem imagem, sem rede...
        pytest.skip(f"Docker/MinIO indisponível: {exc}")
    try:
        client = container.get_client()
        client.make_bucket(BUCKET)
        for key, data in SEED.items():
            client.put_object(BUCKET, key, io.BytesIO(data), length=len(data))
        yield MinioServer(endpoint=f"http://{container.get_config()['endpoint']}")
    finally:
        container.stop()


def _storage(server: MinioServer, **kwargs: object) -> MinioStorage:
    params: dict[str, object] = {"access_key": ACCESS_KEY, "secret_key": SECRET_KEY}
    params.update(kwargs)
    return MinioStorage(endpoint=server.endpoint, max_retries=1, **params)  # type: ignore[arg-type]


class TestMinioContract(StorageContract):
    @pytest.fixture
    def storage(self, minio_server: MinioServer) -> MinioStorage:
        return _storage(minio_server)

    @pytest.fixture
    def base_uri(self) -> str:
        return f"s3://{BUCKET}/dados"

    @pytest.fixture
    def sample_uri(self) -> str:
        return f"s3://{BUCKET}/dados/clientes.csv"

    @pytest.fixture
    def missing_uri(self) -> str:
        return f"s3://{BUCKET}/dados/nao_existe.csv"


async def test_list_shallow_shows_prefixes(minio_server: MinioServer) -> None:
    items = await _storage(minio_server).list(f"s3://{BUCKET}/dados/")
    by_uri = {o.uri: o for o in items}
    assert by_uri[f"s3://{BUCKET}/dados/vendas/"].is_dir
    sample = by_uri[f"s3://{BUCKET}/dados/clientes.csv"]
    assert not sample.is_dir
    assert sample.size_bytes == len(SAMPLE_CSV)
    assert sample.modified_at is not None


async def test_list_bucket_root_and_bare_uri(minio_server: MinioServer) -> None:
    items = await _storage(minio_server).list(BUCKET)
    assert {o.uri for o in items} == {f"s3://{BUCKET}/dados/", f"s3://{BUCKET}/leiame.txt"}
    default = _storage(minio_server, bucket=BUCKET)
    assert {o.uri for o in await default.list("")} == {o.uri for o in items}


async def test_list_recursive_and_limit(minio_server: MinioServer) -> None:
    storage = _storage(minio_server)
    deep = await storage.list(f"s3://{BUCKET}/dados", recursive=True)
    assert f"s3://{BUCKET}/dados/vendas/2024/pedidos.parquet" in {o.uri for o in deep}
    assert all(not o.is_dir for o in deep)
    assert len(await storage.list(f"s3://{BUCKET}/dados", recursive=True, limit=2)) == 2


async def test_list_single_object_and_missing_prefix(minio_server: MinioServer) -> None:
    storage = _storage(minio_server)
    single = await storage.list(f"s3://{BUCKET}/dados/clientes.csv")
    assert [o.uri for o in single] == [f"s3://{BUCKET}/dados/clientes.csv"]
    with pytest.raises(NotFoundError):
        await storage.list(f"s3://{BUCKET}/nao/existe")


async def test_read_bytes_bigger_than_object_and_empty(minio_server: MinioServer) -> None:
    storage = _storage(minio_server)
    assert await storage.read_bytes(f"{BUCKET}/leiame.txt", max_bytes=10_000) == b"ola"
    assert await storage.read_bytes(f"{BUCKET}/dados/vazio.txt") == b""


async def test_parquet_and_json(minio_server: MinioServer) -> None:
    storage = _storage(minio_server)
    uri = f"s3://{BUCKET}/dados/vendas/2024/pedidos.parquet"
    table = await storage.read_sample(uri, rows=2)
    assert table.num_rows == 2
    assert (await storage.infer_schema(uri)).names == ["id", "valor"]
    events = await storage.read_sample(f"s3://{BUCKET}/dados/eventos.jsonl")
    assert events.column("tipo").to_pylist() == ["clique", "compra"]


async def test_prefix_picks_first_tabular_object(minio_server: MinioServer) -> None:
    storage = _storage(minio_server)
    table = await storage.read_sample(f"s3://{BUCKET}/dados/vendas")
    assert table.column_names == ["id", "valor"]
    schema = await storage.infer_schema(f"s3://{BUCKET}/dados", fmt="csv")
    assert schema.names == ["id", "nome", "email"]


async def test_large_objects_are_read_in_bounded_ranges(
    minio_server: MinioServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # teto minúsculo: o CSV é truncado na última linha completa e o Parquet
    # passa a ser lido por faixas (rodapé + primeiro grupo de linhas)
    monkeypatch.setattr(storage_module, "MAX_SAMPLE_BYTES", 40)
    storage = _storage(minio_server)
    table = await storage.read_sample(f"s3://{BUCKET}/dados/clientes.csv")
    assert table.column_names == ["id", "nome", "email"]
    assert table.num_rows == 1
    parquet = await storage.read_sample(f"s3://{BUCKET}/dados/vendas/2024/pedidos.parquet")
    assert parquet.column("id").to_pylist() == [1, 2, 3]


async def test_missing_bucket_is_not_found(minio_server: MinioServer) -> None:
    storage = _storage(minio_server)
    with pytest.raises(NotFoundError):
        await storage.read_bytes("s3://bucket-que-nao-existe/x.csv")
    with pytest.raises(NotFoundError):
        await storage.list("s3://bucket-que-nao-existe/")


async def test_bad_credentials_are_auth_error(minio_server: MinioServer) -> None:
    storage = _storage(minio_server, secret_key="senha-errada-123")
    with pytest.raises(AuthError) as info:
        await storage.read_bytes(f"s3://{BUCKET}/dados/clientes.csv")
    assert "senha-errada-123" not in str(info.value)


async def test_write_when_allowed(minio_server: MinioServer) -> None:
    storage = _storage(minio_server, allow_write=True)
    assert storage.capabilities().supports_write
    uri = f"s3://{BUCKET}/escrita/novo.csv"
    await storage.write_bytes(uri, b"a,b\n1,2\n")
    assert await storage.read_bytes(uri) == b"a,b\n1,2\n"
