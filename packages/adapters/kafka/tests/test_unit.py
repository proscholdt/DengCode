"""Testes rápidos (sem Docker): configuração, decodificação, erros e Schema Registry."""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

import httpx2
import pytest
from aiokafka.errors import (
    GroupCoordinatorNotAvailableError,
    KafkaConnectionError,
    KafkaError,
    NoBrokersAvailable,
    SaslAuthenticationFailed,
    TopicAuthorizationFailedError,
    UnknownTopicOrPartitionError,
)

from dengcode_adapter_kafka.decode import (
    BASE64_PREFIX,
    MAX_VALUE_BYTES,
    decode_key,
    decode_value,
    iso_timestamp,
)
from dengcode_adapter_kafka.errors import is_retriable, map_error, redact
from dengcode_adapter_kafka.stream import KafkaStream, retention_hours
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    NotFoundError,
    RateLimitError,
    TransientError,
)
from dengcode_core.ports.models import AdapterContext, PortKind
from dengcode_core.ports.stream import StreamPort

REGISTRY = "http://registry.test:8081"


# ------------------------------------------------------------------ configuração
def test_is_a_stream_port_with_info() -> None:
    stream = KafkaStream(bootstrap_servers="localhost:9092")
    assert isinstance(stream, StreamPort)
    assert KafkaStream.info.name == "kafka"
    assert KafkaStream.info.port is PortKind.STREAM
    assert KafkaStream.info.knowledge_pack == "dengcode_adapter_kafka:knowledge/kafka.md"


def test_knowledge_pack_is_packaged() -> None:
    from importlib.resources import files

    module, _, rel = str(KafkaStream.info.knowledge_pack).partition(":")
    text = files(module).joinpath(rel).read_text(encoding="utf-8")
    assert text.startswith("# ")
    assert "consumer group" in text.lower()


def test_capabilities_follow_registry_config() -> None:
    plain = KafkaStream(bootstrap_servers="localhost:9092").capabilities()
    assert plain.supports_consumer_lag
    assert not plain.supports_schema_registry
    with_registry = KafkaStream(bootstrap_servers="b:9092", schema_registry_url=REGISTRY)
    assert with_registry.capabilities().supports_schema_registry


def test_from_config(caplog: pytest.LogCaptureFixture) -> None:
    ctx = AdapterContext(workspace="teste", logger=logging.getLogger("teste.kafka"))
    with caplog.at_level(logging.DEBUG, logger="teste.kafka"):
        stream = KafkaStream.from_config(
            {
                "bootstrap_servers": "b1:9092, b2:9092",
                "security_protocol": "sasl_ssl",
                "sasl_mechanism": "scram-sha-512",
                "sasl_username": "leitor",
                "sasl_password": "segredo-kafka-123",
                "schema_registry_url": REGISTRY + "/",
                "timeout_s": 5,
            },
            ctx,
        )
    kwargs = stream._client_kwargs()
    assert kwargs["bootstrap_servers"] == ["b1:9092", "b2:9092"]
    assert kwargs["security_protocol"] == "SASL_SSL"
    assert kwargs["sasl_mechanism"] == "SCRAM-SHA-512"
    assert kwargs["ssl_context"] is not None
    assert kwargs["request_timeout_ms"] == 5000
    assert kwargs["client_id"] == "dengcode"
    assert "segredo-kafka-123" not in caplog.text
    assert "segredo-kafka-123" not in repr(stream)


def test_plaintext_has_no_sasl_or_ssl() -> None:
    kwargs = KafkaStream(bootstrap_servers=["localhost:9092"], client_id="x")._client_kwargs()
    assert "ssl_context" not in kwargs
    assert "sasl_mechanism" not in kwargs
    assert kwargs["client_id"] == "x"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bootstrap_servers": ""},
        {"bootstrap_servers": " , "},
        {"bootstrap_servers": "b:9092", "security_protocol": "TLS"},
        {"bootstrap_servers": "b:9092", "security_protocol": "SASL_SSL"},
        {
            "bootstrap_servers": "b:9092",
            "security_protocol": "SASL_PLAINTEXT",
            "sasl_mechanism": "GSSAPI",
            "sasl_username": "u",
            "sasl_password": "p",
        },
        {"bootstrap_servers": "b:9092", "schema_registry_url": "registry:8081"},
        {"bootstrap_servers": "b:9092", "timeout_s": 0},
    ],
)
def test_invalid_config(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        KafkaStream(**kwargs)


def test_from_config_requires_bootstrap() -> None:
    with pytest.raises(ConfigError):
        KafkaStream.from_config({}, AdapterContext(workspace="teste"))


@pytest.mark.parametrize(
    ("value", "hours"),
    [
        ("604800000", 168),
        ("86400000", 24),
        ("60000", 1),
        ("0", 0),
        ("-1", None),
        (None, None),
        ("x", None),
    ],
)
def test_retention_hours(value: str | None, hours: int | None) -> None:
    assert retention_hours(value) == hours


# ------------------------------------------------------------------ decodificação
def test_decode_value_kinds() -> None:
    assert decode_value(None) is None
    assert decode_value(b'{"a": 1}') == {"a": 1}
    assert decode_value(b"[1, 2]") == [1, 2]
    assert decode_value("olá".encode()) == "olá"
    assert decode_value(b"nao-json {") == "nao-json {"
    assert decode_value(b"") == ""
    assert decode_value(b"\x00\x00\x00\x00\x2a" + b'{"x": true}') == {"x": True}
    binary = decode_value(b"\x00\x00\x00\x00\x2a\x02\x10avro")
    assert isinstance(binary, str)
    assert binary.startswith(BASE64_PREFIX)
    assert decode_value(b"\xff\xfe").startswith(BASE64_PREFIX)


def test_decode_value_large_is_truncated() -> None:
    big = json.dumps({"x": "a" * MAX_VALUE_BYTES}).encode()
    out = decode_value(big)
    assert isinstance(out, str)
    assert "truncado" in out
    assert len(out) < MAX_VALUE_BYTES + 100
    # corte no meio de um caractere multibyte continua sendo texto
    text = decode_value(("é" * MAX_VALUE_BYTES).encode())
    assert not text.startswith(BASE64_PREFIX)
    blob = decode_value(b"\xff" * (MAX_VALUE_BYTES + 10))
    assert blob.startswith(BASE64_PREFIX)
    assert "truncado" in blob


def test_decode_key_stays_text() -> None:
    assert decode_key(None) is None
    assert decode_key(b"123") == "123"
    assert decode_key(b'{"id": 1}') == '{"id": 1}'
    assert str(decode_key(b"\xff")).startswith(BASE64_PREFIX)


def test_iso_timestamp() -> None:
    assert iso_timestamp(0) == "1970-01-01T00:00:00.000+00:00"
    assert iso_timestamp(1_700_000_000_123) == "2023-11-14T22:13:20.123+00:00"
    assert iso_timestamp(-1) is None
    assert iso_timestamp(None) is None


# ------------------------------------------------------------------ erros
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (UnknownTopicOrPartitionError(), NotFoundError),
        (SaslAuthenticationFailed(), AuthError),
        (TopicAuthorizationFailedError(), AuthError),
        (KafkaConnectionError("sem conexão"), TransientError),
        (NoBrokersAvailable(), TransientError),
        (GroupCoordinatorNotAvailableError(), TransientError),
        (TimeoutError(), TransientError),
        (ConnectionRefusedError(), TransientError),
        (OSError("rede inalcançável"), TransientError),
        (KafkaError("outra coisa"), AdapterError),
        (ValueError("x"), AdapterError),
    ],
)
def test_map_error(exc: BaseException, expected: type[Exception]) -> None:
    mapped = map_error(exc, "teste")
    assert type(mapped) is expected


def test_map_error_keeps_domain_errors_and_redacts() -> None:
    original = NotFoundError("x")
    assert map_error(original, "teste") is original
    mapped = map_error(KafkaError("senha=segredo-123 falhou"), "teste", secrets=["segredo-123"])
    assert "segredo-123" not in str(mapped)
    assert redact("a" * 1000, []).endswith("…")


def test_is_retriable() -> None:
    assert is_retriable(GroupCoordinatorNotAvailableError())
    assert not is_retriable(SaslAuthenticationFailed())
    assert not is_retriable(ValueError())


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_unreachable_broker_is_transient() -> None:
    stream = KafkaStream(bootstrap_servers=f"127.0.0.1:{_closed_port()}", timeout_s=3)
    with pytest.raises(TransientError):
        await stream.list_topics()


async def test_empty_group_is_rejected() -> None:
    with pytest.raises(AdapterError):
        await KafkaStream(bootstrap_servers="localhost:9092").consumer_lag("t", " ")


# ------------------------------------------------------------------ Schema Registry
class Registry:
    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


def _with_registry(response: httpx2.Response, **kwargs: Any) -> tuple[KafkaStream, Registry]:
    registry = Registry(response)
    stream = KafkaStream(
        bootstrap_servers="localhost:9092",
        schema_registry_url=REGISTRY,
        registry_transport=httpx2.MockTransport(registry),
        **kwargs,
    )
    return stream, registry


AVRO = {"type": "record", "name": "Pedido", "fields": [{"name": "id", "type": "long"}]}


async def test_schema_without_registry_is_none() -> None:
    assert await KafkaStream(bootstrap_servers="localhost:9092").schema("pedidos") is None


async def test_schema_avro_latest() -> None:
    body = {"subject": "pedidos-value", "version": 3, "id": 42, "schema": json.dumps(AVRO)}
    stream, registry = _with_registry(
        httpx2.Response(200, json=body),
        schema_registry_username="leitor",
        schema_registry_password="segredo-registry",
    )
    found = await stream.schema("pedidos")
    assert found == {
        "subject": "pedidos-value",
        "version": 3,
        "id": 42,
        "schema_type": "AVRO",
        "schema": AVRO,
        "references": [],
    }
    request = registry.requests[0]
    assert request.method == "GET"
    assert str(request.url) == f"{REGISTRY}/subjects/pedidos-value/versions/latest"
    assert request.headers["authorization"].startswith("Basic ")


async def test_schema_subject_is_url_encoded_and_protobuf_stays_text() -> None:
    body = {"version": 1, "id": 7, "schemaType": "PROTOBUF", "schema": "syntax = 'proto3';"}
    stream, registry = _with_registry(httpx2.Response(200, json=body))
    found = await stream.schema("vendas/eventos")
    assert found is not None
    assert found["schema"] == "syntax = 'proto3';"
    assert found["schema_type"] == "PROTOBUF"
    assert found["subject"] == "vendas/eventos-value"
    assert "/subjects/vendas%2Feventos-value/" in str(registry.requests[0].url)


async def test_schema_missing_subject_is_none() -> None:
    stream, _ = _with_registry(httpx2.Response(404, json={"error_code": 40401}))
    assert await stream.schema("sem_schema") is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, AuthError), (403, AuthError), (429, RateLimitError), (503, TransientError)],
)
async def test_schema_http_errors(status: int, expected: type[Exception]) -> None:
    stream, _ = _with_registry(
        httpx2.Response(status, text="falhou para segredo-registry"),
        schema_registry_username="leitor",
        schema_registry_password="segredo-registry",
    )
    with pytest.raises(expected) as info:
        await stream.schema("pedidos")
    assert "segredo-registry" not in str(info.value)


async def test_schema_bad_json_and_unexpected_status() -> None:
    stream, _ = _with_registry(httpx2.Response(200, text="<html>"))
    with pytest.raises(AdapterError):
        await stream.schema("pedidos")
    stream, _ = _with_registry(httpx2.Response(422, text="sujeito inválido"))
    with pytest.raises(AdapterError):
        await stream.schema("pedidos")


async def test_schema_network_error_is_transient() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("conexão recusada", request=request)

    stream = KafkaStream(
        bootstrap_servers="localhost:9092",
        schema_registry_url=REGISTRY,
        registry_transport=httpx2.MockTransport(fail),
    )
    with pytest.raises(TransientError):
        await stream.schema("pedidos")


def test_bootstrap_and_missing_topic_are_not_retried() -> None:
    assert not is_retriable(KafkaConnectionError())
    assert not is_retriable(NoBrokersAvailable())
    assert not is_retriable(UnknownTopicOrPartitionError())
