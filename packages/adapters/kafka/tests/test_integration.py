"""Contrato do StreamPort contra um Kafka real (testcontainers).

Um broker Kafka KRaft de nó único (imagem oficial ``apache/kafka``) por módulo,
com heap pequeno e limite de memória; sem Docker os testes são pulados. A
porta do host é a mesma do container para que o endereço anunciado funcione.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from dengcode_adapter_kafka.decode import BASE64_PREFIX
from dengcode_adapter_kafka.stream import KafkaStream
from dengcode_testkit.contracts.stream import StreamContract

pytestmark = pytest.mark.integration

KAFKA_IMAGE = "apache/kafka:3.9.1"

CONTRACT_TOPIC = "dc_pedidos"
LAGGING_GROUP = "dc-grupo-atrasado"
MIXED_TOPIC = "dc_misto"
LONG_TOPIC = "dc_longo"
EMPTY_TOPIC = "dc_vazio"
RETENTION_MS = 86_400_000  # 24 h no tópico do contrato

ORDERS = [
    {"pedido_id": i, "cliente": f"cliente{i}@exemplo.com", "valor": 10.0 * i, "status": "pago"}
    for i in range(1, 10)
]
MIXED_VALUES: list[tuple[bytes | None, bytes | None]] = [
    (b"k-json", b'{"tipo": "json", "n": 1}'),
    (b"k-texto", "olá, texto livre".encode()),
    (None, b"\xff\xfe\x00\x01binario"),
    (b"k-nulo", None),
    (b"k-wire", b"\x00\x00\x00\x00\x07" + b'{"via": "registry"}'),
]


@dataclass(frozen=True)
class KafkaServer:
    bootstrap: str


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _seed(bootstrap: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        await admin.create_topics(
            [
                NewTopic(CONTRACT_TOPIC, 3, 1, topic_configs={"retention.ms": str(RETENTION_MS)}),
                NewTopic(MIXED_TOPIC, 1, 1),
                NewTopic(LONG_TOPIC, 1, 1),
                NewTopic(EMPTY_TOPIC, 2, 1),
            ]
        )
    finally:
        await admin.close()

    producer = AIOKafkaProducer(bootstrap_servers=bootstrap, enable_idempotence=True)
    await producer.start()
    try:
        for i, order in enumerate(ORDERS):
            await producer.send_and_wait(
                CONTRACT_TOPIC,
                json.dumps(order).encode(),
                key=str(order["pedido_id"]).encode(),
                partition=i % 3,
            )
        for key, value in MIXED_VALUES:
            await producer.send_and_wait(MIXED_TOPIC, value, key=key)
        for i in range(10):
            await producer.send_and_wait(LONG_TOPIC, json.dumps({"seq": i}).encode())
    finally:
        await producer.stop()

    # grupo com offset gravado só no começo da partição 0: lag > 0
    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap, group_id=LAGGING_GROUP, enable_auto_commit=False
    )
    await consumer.start()
    try:
        tp = TopicPartition(CONTRACT_TOPIC, 0)
        consumer.assign([tp])
        await consumer.commit({tp: 1})
    finally:
        await consumer.stop()


@pytest.fixture(scope="module")
def kafka_server() -> Iterator[KafkaServer]:
    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.wait_strategies import LogMessageWaitStrategy
    except ImportError as exc:  # pragma: no cover - testcontainers é dependência de dev
        pytest.skip(f"testcontainers indisponível: {exc}")

    port = _free_port()
    container = (
        DockerContainer(KAFKA_IMAGE)
        .with_bind_ports(port, port)
        .with_envs(
            KAFKA_NODE_ID="1",
            KAFKA_PROCESS_ROLES="broker,controller",
            KAFKA_LISTENERS=f"PLAINTEXT://:{port},CONTROLLER://:9093",
            KAFKA_ADVERTISED_LISTENERS=f"PLAINTEXT://localhost:{port}",
            KAFKA_CONTROLLER_LISTENER_NAMES="CONTROLLER",
            KAFKA_LISTENER_SECURITY_PROTOCOL_MAP="CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
            KAFKA_CONTROLLER_QUORUM_VOTERS="1@localhost:9093",
            KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR="1",
            KAFKA_OFFSETS_TOPIC_NUM_PARTITIONS="1",
            KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR="1",
            KAFKA_TRANSACTION_STATE_LOG_MIN_ISR="1",
            KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS="0",
            # ligado de propósito: o adapter não pode criar tópico ao ler
            KAFKA_AUTO_CREATE_TOPICS_ENABLE="true",
            KAFKA_HEAP_OPTS="-Xmx384m -Xms256m",
        )
        .with_kwargs(mem_limit="768m")
        .waiting_for(LogMessageWaitStrategy("Kafka Server started").with_startup_timeout(120))
    )
    try:
        container.start()
    except Exception as exc:  # Docker ausente, parado, sem imagem ou sem rede
        with contextlib.suppress(Exception):
            container.stop()
        pytest.skip(f"Docker/Kafka indisponível para testes de integração: {exc}")
    try:
        bootstrap = f"localhost:{port}"
        asyncio.run(_seed(bootstrap))
        yield KafkaServer(bootstrap=bootstrap)
    finally:
        container.stop()


def _stream(server: KafkaServer, **kwargs: object) -> KafkaStream:
    return KafkaStream(bootstrap_servers=server.bootstrap, timeout_s=20, **kwargs)  # type: ignore[arg-type]


class TestKafkaContract(StreamContract):
    @pytest.fixture
    def stream(self, kafka_server: KafkaServer) -> KafkaStream:
        return _stream(kafka_server)

    @pytest.fixture
    def topic_with_messages(self) -> str:
        return CONTRACT_TOPIC

    @pytest.fixture
    def consumer_group(self) -> str:
        return LAGGING_GROUP


async def test_list_topics_partitions_and_retention(kafka_server: KafkaServer) -> None:
    topics = {t.name: t for t in await _stream(kafka_server).list_topics()}
    assert topics[CONTRACT_TOPIC].partitions == 3
    assert topics[CONTRACT_TOPIC].retention_hours == 24
    assert topics[EMPTY_TOPIC].partitions == 2
    assert "__consumer_offsets" not in topics


async def test_sample_reads_latest_messages_per_partition(kafka_server: KafkaServer) -> None:
    items = await _stream(kafka_server).sample(LONG_TOPIC, max_messages=3)
    assert [i["offset"] for i in items] == [7, 8, 9]
    assert [i["value"] for i in items] == [{"seq": 7}, {"seq": 8}, {"seq": 9}]
    assert all(i["timestamp"] and i["timestamp"].endswith("+00:00") for i in items)


async def test_sample_whole_topic_has_all_orders(kafka_server: KafkaServer) -> None:
    items = await _stream(kafka_server).sample(CONTRACT_TOPIC, max_messages=100)
    assert sorted(i["value"]["pedido_id"] for i in items) == [o["pedido_id"] for o in ORDERS]
    assert {i["partition"] for i in items} == {0, 1, 2}
    assert all(isinstance(i["key"], str) for i in items)


async def test_sample_decodes_text_binary_null_and_wire_format(kafka_server: KafkaServer) -> None:
    by_key = {i["key"]: i for i in await _stream(kafka_server).sample(MIXED_TOPIC)}
    assert by_key["k-json"]["value"] == {"tipo": "json", "n": 1}
    assert by_key["k-texto"]["value"] == "olá, texto livre"
    assert by_key["k-nulo"]["value"] is None
    assert by_key["k-wire"]["value"] == {"via": "registry"}
    assert str(by_key[None]["value"]).startswith(BASE64_PREFIX)


async def test_sample_empty_topic_and_zero_limit(kafka_server: KafkaServer) -> None:
    stream = _stream(kafka_server)
    assert await stream.sample(EMPTY_TOPIC, timeout_s=2) == []
    assert await stream.sample(CONTRACT_TOPIC, max_messages=0) == []


async def test_lag_values(kafka_server: KafkaServer) -> None:
    stream = _stream(kafka_server)
    lag = await stream.consumer_lag(CONTRACT_TOPIC, LAGGING_GROUP)
    # 9 pedidos em 3 partições (3 cada); a partição 0 tem offset 1 gravado
    assert lag == {0: 2, 1: 3, 2: 3}
    # grupo sem offsets gravados: lag é tudo o que está retido
    assert await stream.consumer_lag(CONTRACT_TOPIC, "dc-grupo-novo") == {0: 3, 1: 3, 2: 3}
