"""StreamPort sobre Apache Kafka via ``aiokafka`` (somente leitura).

Garantias de leitura:

- metadados sempre pedidos para *todos* os tópicos: pedir um tópico específico
  faria o broker criá-lo quando ``auto.create.topics.enable`` está ligado;
- a amostra usa um consumidor sem ``group_id`` e com atribuição manual: não
  entra em grupo, não dispara rebalanceamento e não grava offsets;
- cada operação abre e fecha os próprios clientes (sem conexões penduradas).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, ClassVar, override
from urllib.parse import quote, urlparse

import httpx2
from aiokafka import AIOKafkaConsumer, ConsumerRecord, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
from aiokafka.errors import KafkaError, for_code
from aiokafka.helpers import create_ssl_context

from dengcode_adapter_kafka.decode import decode_key, decode_value, iso_timestamp
from dengcode_adapter_kafka.errors import is_retriable, map_error, redact
from dengcode_core.errors import (
    AdapterError,
    AuthError,
    ConfigError,
    DengCodeError,
    NotFoundError,
    RateLimitError,
    TransientError,
)
from dengcode_core.ports.models import AdapterContext, AdapterInfo, PortKind
from dengcode_core.ports.stream import StreamCapabilities, TopicInfo

log = logging.getLogger(__name__)

SECURITY_PROTOCOLS = frozenset({"PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL"})
SASL_MECHANISMS = frozenset({"PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"})
MAX_SAMPLE_MESSAGES = 1000
"""Teto de mensagens por amostra, qualquer que seja o ``max_messages`` pedido."""
_MAX_ATTEMPTS = 4
_BACKOFF_S = 0.25
_FETCH_BATCH = 500
_MS_PER_HOUR = 3_600_000


def _opt_str(value: object) -> str | None:
    return None if value is None or value == "" else str(value)


def retention_hours(value: str | None) -> int | None:
    """``retention.ms`` em horas (arredondado; ``-1``/ausente = sem limite = ``None``)."""
    if value is None:
        return None
    try:
        ms = int(value)
    except ValueError:
        return None
    if ms < 0:
        return None
    return max(1, round(ms / _MS_PER_HOUR)) if ms else 0


def _record_dict(record: ConsumerRecord[bytes, bytes]) -> dict[str, Any]:
    return {
        "partition": record.partition,
        "offset": record.offset,
        "timestamp": iso_timestamp(record.timestamp),
        "key": decode_key(record.key),
        "value": decode_value(record.value),
    }


class KafkaStream:
    """Lista tópicos, amostra mensagens recentes, mede lag e lê o Schema Registry."""

    info: ClassVar[AdapterInfo] = AdapterInfo(
        name="kafka",
        port=PortKind.STREAM,
        version="0.1.0",
        knowledge_pack="dengcode_adapter_kafka:knowledge/kafka.md",
    )
    name = "kafka"

    def __init__(
        self,
        *,
        bootstrap_servers: str | Sequence[str],
        security_protocol: str = "PLAINTEXT",
        sasl_mechanism: str | None = None,
        sasl_username: str | None = None,
        sasl_password: str | None = None,
        schema_registry_url: str | None = None,
        schema_registry_username: str | None = None,
        schema_registry_password: str | None = None,
        client_id: str = "dengcode",
        timeout_s: float = 30.0,
        registry_transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        servers = (
            [s.strip() for s in bootstrap_servers.split(",")]
            if isinstance(bootstrap_servers, str)
            else [str(s).strip() for s in bootstrap_servers]
        )
        servers = [s for s in servers if s]
        if not servers:
            raise ConfigError("kafka: 'bootstrap_servers' é obrigatório (ex.: localhost:9092)")
        protocol = security_protocol.upper()
        if protocol not in SECURITY_PROTOCOLS:
            raise ConfigError(
                f"kafka: security_protocol inválido {security_protocol!r} "
                f"(use {', '.join(sorted(SECURITY_PROTOCOLS))})"
            )
        mechanism = (sasl_mechanism or "PLAIN").upper()
        if protocol.startswith("SASL_"):
            if mechanism not in SASL_MECHANISMS:
                raise ConfigError(
                    f"kafka: sasl_mechanism não suportado {sasl_mechanism!r} "
                    f"(use {', '.join(sorted(SASL_MECHANISMS))})"
                )
            if not sasl_username or not sasl_password:
                raise ConfigError("kafka: SASL exige 'sasl_username' e 'sasl_password'")
        if schema_registry_url is not None:
            parsed = urlparse(schema_registry_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigError(
                    "kafka: schema_registry_url inválida (use http(s)://host:porta): "
                    f"{schema_registry_url!r}"
                )
        if timeout_s <= 0:
            raise ConfigError("kafka: 'timeout_s' deve ser positivo")
        self._servers = servers
        self._protocol = protocol
        self._mechanism = mechanism
        self._sasl_username = sasl_username
        self._sasl_password = sasl_password
        self._registry_url = schema_registry_url.rstrip("/") if schema_registry_url else None
        self._registry_auth = (
            httpx2.BasicAuth(schema_registry_username, schema_registry_password or "")
            if schema_registry_username
            else None
        )
        self._registry_password = schema_registry_password
        self._registry_transport = registry_transport
        self._client_id = client_id
        self._timeout_s = float(timeout_s)

    @classmethod
    def from_config(cls, cfg: dict[str, Any], ctx: AdapterContext) -> KafkaStream:
        servers = cfg.get("bootstrap_servers")
        if not servers:
            raise ConfigError("kafka: 'bootstrap_servers' é obrigatório (ex.: localhost:9092)")
        stream = cls(
            bootstrap_servers=servers if isinstance(servers, list) else str(servers),
            security_protocol=str(cfg.get("security_protocol") or "PLAINTEXT"),
            sasl_mechanism=_opt_str(cfg.get("sasl_mechanism")),
            sasl_username=_opt_str(cfg.get("sasl_username")),
            sasl_password=_opt_str(cfg.get("sasl_password")),
            schema_registry_url=_opt_str(cfg.get("schema_registry_url")),
            schema_registry_username=_opt_str(cfg.get("schema_registry_username")),
            schema_registry_password=_opt_str(cfg.get("schema_registry_password")),
            client_id=str(cfg.get("client_id") or "dengcode"),
            timeout_s=float(cfg.get("timeout_s", 30.0)),
        )
        ctx.logger.debug(
            "stream kafka configurado",
            extra={
                "bootstrap_servers": stream._servers,
                "security_protocol": stream._protocol,
                "schema_registry": stream._registry_url is not None,
            },
        )
        return stream

    @override
    def __repr__(self) -> str:
        return f"KafkaStream(bootstrap_servers={self._servers!r}, protocol={self._protocol!r})"

    def capabilities(self) -> StreamCapabilities:
        return StreamCapabilities(
            supports_consumer_lag=True,
            supports_schema_registry=self._registry_url is not None,
        )

    # ------------------------------------------------------------------ util
    @property
    def _secrets(self) -> tuple[str | None, ...]:
        return (self._sasl_password, self._registry_password)

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "bootstrap_servers": self._servers,
            "client_id": self._client_id,
            "request_timeout_ms": int(self._timeout_s * 1000),
            "security_protocol": self._protocol,
        }
        if self._protocol in ("SSL", "SASL_SSL"):
            kwargs["ssl_context"] = create_ssl_context()
        if self._protocol.startswith("SASL_"):
            kwargs["sasl_mechanism"] = self._mechanism
            kwargs["sasl_plain_username"] = self._sasl_username
            kwargs["sasl_plain_password"] = self._sasl_password
        return kwargs

    @asynccontextmanager
    async def _admin(self) -> AsyncIterator[AIOKafkaAdminClient]:
        admin = AIOKafkaAdminClient(**self._client_kwargs())
        try:
            await admin.start()
            yield admin
        finally:
            await admin.close()

    @asynccontextmanager
    async def _consumer(self) -> AsyncIterator[AIOKafkaConsumer]:
        # sem group_id: nada de grupo, rebalanceamento ou commit de offsets
        consumer = AIOKafkaConsumer(
            **self._client_kwargs(),
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_partition_fetch_bytes=1024 * 1024,
        )
        try:
            await consumer.start()
            yield consumer
        finally:
            await consumer.stop()

    async def _call[T](self, what: str, op: Callable[[], Awaitable[T]], *, extra_s: float = 0) -> T:
        """Executa ``op`` com prazo total, novas tentativas em erros passageiros e
        tradução de exceções."""
        try:
            async with asyncio.timeout(self._timeout_s + extra_s):
                attempt = 1
                while True:
                    try:
                        return await op()
                    except DengCodeError:
                        raise
                    except Exception as exc:
                        if attempt >= _MAX_ATTEMPTS or not is_retriable(exc):
                            raise
                        log.debug(
                            "kafka: tentativa %d de %s falhou (%s); tentando de novo",
                            attempt,
                            what,
                            type(exc).__name__,
                        )
                        await asyncio.sleep(_BACKOFF_S * 2 ** (attempt - 1))
                        attempt += 1
        except DengCodeError:
            raise
        except TimeoutError:
            raise TransientError(
                f"tempo esgotado ({self._timeout_s + extra_s:.0f} s) em {what}"
            ) from None
        except Exception as exc:
            raise map_error(exc, what, secrets=self._secrets) from None

    @staticmethod
    async def _topics_metadata(admin: AIOKafkaAdminClient) -> list[dict[str, Any]]:
        # topics=None pede TODOS os tópicos: não aciona a criação automática
        return list(await admin.describe_topics(None))

    async def _partitions(self, admin: AIOKafkaAdminClient, topic: str) -> list[TopicPartition]:
        for meta in await self._topics_metadata(admin):
            if meta["topic"] != topic:
                continue
            code = int(meta.get("error_code") or 0)
            if code:
                raise for_code(code)(f"metadados do tópico {topic}")
            parts = sorted(int(p["partition"]) for p in meta.get("partitions") or [])
            if not parts:
                break
            return [TopicPartition(topic, p) for p in parts]
        raise NotFoundError(f"tópico inexistente no Kafka: {topic}")

    @staticmethod
    async def _retention(admin: AIOKafkaAdminClient, names: list[str]) -> dict[str, int | None]:
        """``retention.ms`` por tópico; sem permissão de DescribeConfigs, fica vazio."""
        if not names:
            return {}
        resources = [
            ConfigResource(ConfigResourceType.TOPIC, name, {"retention.ms": None}) for name in names
        ]
        try:
            responses = await admin.describe_configs(resources)
        except KafkaError as exc:
            log.debug("kafka: retenção indisponível (%s)", type(exc).__name__)
            return {}
        out: dict[str, int | None] = {}
        for response in responses:
            for res in response.to_object().get("resources", []):
                if res.get("error_code"):
                    continue
                for entry in res.get("config_entries", []):
                    if entry.get("config_names", entry.get("config_name")) == "retention.ms":
                        out[str(res["resource_name"])] = retention_hours(entry.get("config_value"))
        return out

    # ------------------------------------------------------------------ porta
    async def list_topics(self) -> list[TopicInfo]:
        async def run() -> list[TopicInfo]:
            async with self._admin() as admin:
                metadata = [
                    m
                    for m in await self._topics_metadata(admin)
                    if not m.get("is_internal") and not str(m["topic"]).startswith("__")
                ]
                names = sorted(str(m["topic"]) for m in metadata)
                retention = await self._retention(admin, names)
            partitions = {str(m["topic"]): len(m.get("partitions") or []) for m in metadata}
            return [
                TopicInfo(name=n, partitions=partitions[n], retention_hours=retention.get(n))
                for n in names
            ]

        return await self._call("list_topics", run)

    async def sample(
        self, topic: str, *, max_messages: int = 20, timeout_s: int = 10
    ) -> list[dict[str, Any]]:
        limit = max(0, min(int(max_messages), MAX_SAMPLE_MESSAGES))
        wait_s = max(0.0, float(timeout_s))

        async def run() -> list[dict[str, Any]]:
            async with self._admin() as admin:
                tps = await self._partitions(admin, topic)
            if limit == 0:
                return []
            async with self._consumer() as consumer:
                consumer.assign(tps)
                begin = await consumer.beginning_offsets(tps)
                end = await consumer.end_offsets(tps)
                pending: dict[TopicPartition, int] = {}
                for tp in tps:
                    # as N últimas de cada partição: o recorte global vem depois
                    start = max(begin[tp], end[tp] - limit)
                    if start < end[tp]:
                        consumer.seek(tp, start)
                        pending[tp] = end[tp]
                    else:
                        consumer.pause(tp)
                records = await self._drain(consumer, pending, wait_s)
            records.sort(key=lambda r: (r.timestamp, r.partition, r.offset))
            return [_record_dict(r) for r in records[-limit:]]

        return await self._call(f"sample({topic})", run, extra_s=wait_s)

    @staticmethod
    async def _drain(
        consumer: AIOKafkaConsumer, pending: dict[TopicPartition, int], wait_s: float
    ) -> list[ConsumerRecord[bytes, bytes]]:
        """Lê até o offset final de cada partição ou até o prazo (o que vier antes)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        out: list[ConsumerRecord[bytes, bytes]] = []
        while pending:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            batch = await consumer.getmany(
                *pending, timeout_ms=int(min(remaining, 1.0) * 1000), max_records=_FETCH_BATCH
            )
            for tp, messages in batch.items():
                stop = pending.get(tp)
                if stop is not None:
                    out.extend(m for m in messages if m.offset < stop)
            for tp in list(pending):
                if await consumer.position(tp) >= pending[tp]:
                    consumer.pause(tp)
                    del pending[tp]
        return out

    async def consumer_lag(self, topic: str, group: str) -> dict[int, int]:
        if not group.strip():
            raise AdapterError("informe o grupo de consumidores (group)")

        async def run() -> dict[int, int]:
            async with self._admin() as admin:
                tps = await self._partitions(admin, topic)
                committed = await admin.list_consumer_group_offsets(group, partitions=tps)
            async with self._consumer() as consumer:
                begin = await consumer.beginning_offsets(tps)
                end = await consumer.end_offsets(tps)
            lag: dict[int, int] = {}
            for tp in tps:
                meta = committed.get(tp)
                offset = meta.offset if meta is not None else -1
                # sem offset gravado o grupo ainda leria tudo o que está retido
                base = offset if offset >= 0 else begin[tp]
                lag[tp.partition] = max(0, end[tp] - base)
            return lag

        return await self._call(f"consumer_lag({topic}, {group})", run)

    async def schema(self, topic: str) -> dict[str, Any] | None:
        if self._registry_url is None:
            return None
        subject = f"{topic}-value"
        url = f"{self._registry_url}/subjects/{quote(subject, safe='')}/versions/latest"
        what = f"schema registry ({subject})"
        try:
            async with httpx2.AsyncClient(
                timeout=self._timeout_s, transport=self._registry_transport
            ) as client:
                response = await client.get(
                    url,
                    auth=self._registry_auth,
                    headers={"Accept": "application/vnd.schemaregistry.v1+json, application/json"},
                )
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            detail = redact(str(exc) or type(exc).__name__, self._secrets)
            raise TransientError(f"Schema Registry indisponível em {what}: {detail}") from None
        except httpx2.HTTPError as exc:
            detail = redact(str(exc) or type(exc).__name__, self._secrets)
            raise AdapterError(f"falha ao consultar o {what}: {detail}") from None
        return self._schema_from_response(response, subject, what)

    def _schema_from_response(
        self, response: httpx2.Response, subject: str, what: str
    ) -> dict[str, Any] | None:
        status = response.status_code
        if status == 404:
            return None  # tópico sem schema registrado
        if status >= 400:
            detail = redact(response.text, self._secrets)
            if status in (401, 403):
                raise AuthError(f"credencial inválida ou sem permissão no {what}: {detail}")
            if status == 429:
                raise RateLimitError(f"limite de requisições do {what}: {detail}")
            if status >= 500:
                raise TransientError(f"erro do servidor no {what} ({status}): {detail}")
            raise AdapterError(f"resposta inesperada do {what} ({status}): {detail}")
        try:
            data: dict[str, Any] = response.json()
        except ValueError:
            raise AdapterError(f"resposta não é JSON no {what}") from None
        schema_type = str(data.get("schemaType") or "AVRO").upper()
        raw = data.get("schema")
        parsed: Any = raw
        if isinstance(raw, str) and schema_type in ("AVRO", "JSON"):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = raw
        return {
            "subject": str(data.get("subject") or subject),
            "version": data.get("version"),
            "id": data.get("id"),
            "schema_type": schema_type,
            "schema": parsed,
            "references": data.get("references") or [],
        }
