"""Testes rápidos (sem Docker): URIs, configuração e mapeamento de erros."""

from __future__ import annotations

import logging

import pytest
from obstore.exceptions import (
    GenericError,
    InvalidPathError,
    PermissionDeniedError,
    UnauthenticatedError,
)

from dengcode_adapter_minio.errors import map_error, redact
from dengcode_adapter_minio.storage import MinioStorage, _parse
from dengcode_adapter_minio.uri import S3Location, parse_uri
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    RateLimitError,
    TransientError,
    UnsupportedOperation,
)
from dengcode_core.ports.models import AdapterContext
from dengcode_core.ports.storage import StoragePort


# ------------------------------------------------------------------ URIs
@pytest.mark.parametrize(
    ("uri", "bucket", "key"),
    [
        ("s3://dados/vendas/2024/pedidos.csv", "dados", "vendas/2024/pedidos.csv"),
        ("S3://dados/x.csv", "dados", "x.csv"),
        ("s3a://dados/x.csv", "dados", "x.csv"),
        ("s3://dados", "dados", ""),
        ("s3://dados/", "dados", ""),
        ("s3://dados/vendas/", "dados", "vendas"),
        ("dados/vendas//pedidos.csv", "dados", "vendas/pedidos.csv"),
        ("dados", "dados", ""),
        ("dados\\vendas\\a.csv", "dados", "vendas/a.csv"),
    ],
)
def test_parse_uri(uri: str, bucket: str, key: str) -> None:
    assert parse_uri(uri) == S3Location(bucket, key)


@pytest.mark.parametrize("uri", ["", ".", "/"])
def test_empty_uri_uses_default_bucket(uri: str) -> None:
    assert parse_uri(uri, default_bucket="padrao") == S3Location("padrao", "")


@pytest.mark.parametrize("uri", ["", "s3://", "gs://b/k", "file:///tmp/x", "s3://b/../x"])
def test_invalid_uris(uri: str) -> None:
    with pytest.raises(AdapterError):
        parse_uri(uri)


def test_canonical_uri() -> None:
    assert S3Location("b", "a/c.csv").uri() == "s3://b/a/c.csv"
    assert S3Location("b", "a").uri(is_dir=True) == "s3://b/a/"
    assert S3Location("b", "").uri() == "s3://b/"


# ------------------------------------------------------------------ erros
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (FileNotFoundError("Object at location x not found"), NotFoundError),
        (GenericError("Client error with status 404 Not Found: NoSuchBucket"), NotFoundError),
        (
            GenericError("GET http://h/b in 5ms - Server returned non-2xx status code: 404 Not"),
            NotFoundError,
        ),
        (
            GenericError("list - Server returned non-2xx status code: 403 Forbidden: <Code>"),
            AuthError,
        ),
        (GenericError("status code: 416 Range Not Satisfiable: InvalidRange"), AdapterError),
        (GenericError("status code: 400 Bad Request: connection header"), AdapterError),
        (UnauthenticatedError("sem credencial"), AuthError),
        (PermissionDeniedError("denied"), AuthError),
        (GenericError("Client error with status 403 Forbidden: InvalidAccessKeyId"), AuthError),
        (GenericError("Server error with status 503: SlowDown"), RateLimitError),
        (GenericError("Server error with status 500 Internal Server Error"), TransientError),
        (GenericError("HTTP error: error sending request for url"), TransientError),
        (ConnectionRefusedError("recusada"), TransientError),
        (TimeoutError("expirou"), TransientError),
        (InvalidPathError("caminho ruim"), AdapterError),
        (GenericError("algo inesperado"), AdapterError),
        (ValueError("valor ruim"), AdapterError),
    ],
)
def test_map_error(exc: Exception, expected: type[Exception]) -> None:
    mapped = map_error(exc, "s3://b/k")
    assert type(mapped) is expected
    assert "s3://b/k" in str(mapped)


def test_transient_errors_are_not_auth_or_not_found() -> None:
    assert not isinstance(map_error(GenericError("status 502 Bad Gateway"), "u"), NotFoundError)


def test_errors_never_leak_secrets() -> None:
    exc = GenericError("Client error with status 403: InvalidAccessKeyId chave-x segredo-y")
    mapped = map_error(exc, "s3://b/k", secrets=("chave-x", "segredo-y", None))
    assert "chave-x" not in str(mapped)
    assert "segredo-y" not in str(mapped)
    assert isinstance(mapped, AuthError)


def test_redact_truncates() -> None:
    assert len(redact("x" * 1000, ())) < 400


# ------------------------------------------------------------------ config
def _ctx() -> AdapterContext:
    return AdapterContext(workspace="t", logger=logging.getLogger("teste.minio"))


def test_from_config_and_capabilities() -> None:
    storage = MinioStorage.from_config(
        {
            "endpoint": "http://localhost:9000",
            "access_key": "chave",
            "secret_key": "segredo-super",
            "bucket": "dados",
        },
        _ctx(),
    )
    assert isinstance(storage, StoragePort)
    caps = storage.capabilities()
    assert caps.supports_write is False
    assert caps.table_formats == frozenset({"csv", "parquet", "json"})
    assert "segredo-super" not in repr(storage)
    assert MinioStorage.info.name == "minio"


@pytest.mark.parametrize("cfg", [{}, {"endpoint": "localhost:9000"}, {"endpoint": "ftp://x"}])
def test_invalid_config(cfg: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        MinioStorage.from_config(cfg, _ctx())


async def test_write_disabled_by_default() -> None:
    storage = MinioStorage(endpoint="http://localhost:9000")
    with pytest.raises(UnsupportedOperation):
        await storage.write_bytes("s3://b/x.txt", b"1")


async def test_unreachable_endpoint_is_transient() -> None:
    storage = MinioStorage(
        endpoint="http://127.0.0.1:9",
        access_key="a",
        secret_key="b",
        timeout_s=3,
        max_retries=0,
    )
    with pytest.raises(TransientError):
        await storage.read_bytes("s3://b/x.csv")


# ------------------------------------------------------------------ parsing
def test_parse_truncated_csv_drops_partial_line() -> None:
    table = _parse(b"id,nome\n1,Ana\n2,Bru", "csv", ".csv", None, truncated=True)
    assert table.num_rows == 1


def test_parse_tsv_and_json() -> None:
    assert _parse(b"a\tb\n1\t2\n", "csv", ".tsv", None, truncated=False).column_names == ["a", "b"]
    assert _parse(b'{"a": 1}\n{"a": 2}\n', "json", ".jsonl", 1, truncated=False).num_rows == 1


def test_parse_invalid_is_adapter_error() -> None:
    with pytest.raises(AdapterError):
        _parse(b"nao e parquet", "parquet", ".parquet", 10, truncated=False)
    with pytest.raises(UnsupportedOperation):
        _parse(b"x", "avro", ".avro", 10, truncated=False)
